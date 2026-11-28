#!/usr/bin/env bash
set -euo pipefail

# Safely update VLLM_MODEL (and optional VLLM_TOOL_CALL_PARSER) in .env,
# repair bind-mount permissions, and recreate the vllm service so the new
# Hugging Face model is pulled on demand without touching other containers.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'EOF'
Usage: vllm-model.sh [options] [MODEL]

Pull and serve a Hugging Face model on the opt-in vLLM service.

Options:
  --model <org/repo>   Hugging Face model repository ID (e.g. Qwen/Qwen3-8B-AWQ)
  --parser <name>      Tool call parser for --tool-call-parser (e.g. hermes, qwen3_coder, llama3_json)
  --repo-dir <dir>     Path to HomeLab git repository (default: auto-detected)
  --dry-run, -n        Validate arguments and preview changes without modifying .env or Docker
  --no-restart         Update .env only without running fix-permissions.sh or restarting vllm
  -h, --help           Show this help message
EOF
}

MODEL_ARG=""
PARSER_ARG=""
CUSTOM_REPO_DIR=""
DRY_RUN=false
NO_RESTART=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model)
      [[ $# -ge 2 ]] || { echo "Error: --model requires an argument" >&2; exit 1; }
      MODEL_ARG="$2"
      shift 2
      ;;
    --parser)
      [[ $# -ge 2 ]] || { echo "Error: --parser requires an argument" >&2; exit 1; }
      PARSER_ARG="$2"
      shift 2
      ;;
    --repo-dir)
      [[ $# -ge 2 ]] || { echo "Error: --repo-dir requires an argument" >&2; exit 1; }
      CUSTOM_REPO_DIR="$2"
      shift 2
      ;;
    --dry-run|-n)
      DRY_RUN=true
      shift
      ;;
    --no-restart)
      NO_RESTART=true
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    -*)
      echo "Error: unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
    *)
      if [[ -n "${MODEL_ARG}" ]]; then
        echo "Error: unexpected positional argument: $1" >&2
        usage >&2
        exit 1
      fi
      MODEL_ARG="$1"
      shift
      ;;
  esac
done

if [[ -z "${MODEL_ARG}" ]]; then
  echo "Error: MODEL is required (usage: make vllm-model MODEL=org/repo [PARSER=hermes])" >&2
  exit 1
fi

if [[ "${MODEL_ARG}" == *$'\n'* || "${MODEL_ARG}" == *$'\r'* ]]; then
  echo "Error: MODEL must not contain newline characters" >&2
  exit 1
fi

if [[ ! "${MODEL_ARG}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || [[ "${MODEL_ARG}" == *".."* ]]; then
  echo "Error: invalid Hugging Face model ID '${MODEL_ARG}' (expected 'org/repo' using [A-Za-z0-9._-])" >&2
  exit 1
fi

if [[ -n "${PARSER_ARG}" ]]; then
  if [[ "${PARSER_ARG}" == *$'\n'* || "${PARSER_ARG}" == *$'\r'* ]] || [[ ! "${PARSER_ARG}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
    echo "Error: invalid tool call parser '${PARSER_ARG}' (expected [A-Za-z0-9._-]+)" >&2
    exit 1
  fi
fi

resolve_repo_dir() {
  local candidate=""
  if [[ -n "${CUSTOM_REPO_DIR}" ]]; then
    candidate="${CUSTOM_REPO_DIR}"
  elif [[ -n "${HOMELAB_REPO_DIR:-}" ]]; then
    candidate="${HOMELAB_REPO_DIR}"
  else
    candidate="${SCRIPT_DIR}/.."
  fi

  if [[ ! -d "${candidate}" ]]; then
    echo "Error: repository directory does not exist: ${candidate}" >&2
    exit 1
  fi
  realpath "${candidate}"
}

REPO_DIR="$(resolve_repo_dir)"
ENV_FILE="${ENV_FILE:-${REPO_DIR}/.env}"
if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Error: .env file not found at ${ENV_FILE}" >&2
  exit 1
fi

read_env_var() {
  local env_path="$1"
  local target_key="$2"
  local default_val="${3:-}"
  local found=false
  local result="${default_val}"
  local line key val first_char last_char

  while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line%$'\r'}"
    [[ "${line}" =~ ^[[:space:]]*(#|$) ]] && continue
    if [[ "${line}" =~ ^[[:space:]]*export[[:space:]]+(.*)$ ]]; then
      line="${BASH_REMATCH[1]}"
    fi
    [[ "${line}" == *=* ]] || continue
    key="${line%%=*}"
    key="${key#"${key%%[![:space:]]*}"}"
    key="${key%"${key##*[![:space:]]}"}"
    [[ "${key}" == "${target_key}" ]] || continue

    val="${line#*=}"
    val="${val#"${val%%[![:space:]]*}"}"
    val="${val%"${val##*[![:space:]]}"}"
    if [[ "${#val}" -ge 2 ]]; then
      first_char="${val:0:1}"
      last_char="${val: -1}"
      if [[ ( "${first_char}" == '"' && "${last_char}" == '"' ) || ( "${first_char}" == "'" && "${last_char}" == "'" ) ]]; then
        val="${val:1:${#val}-2}"
      fi
    fi
    result="${val}"
    found=true
  done < "${env_path}"

  if [[ "${found}" == true ]]; then
    printf '%s\n' "${result}"
  else
    printf '%s\n' "${default_val}"
  fi
}

CURRENT_MODEL="$(read_env_var "${ENV_FILE}" "VLLM_MODEL" "")"
CURRENT_PARSER="$(read_env_var "${ENV_FILE}" "VLLM_TOOL_CALL_PARSER" "")"
EFFECTIVE_PARSER="${PARSER_ARG:-${CURRENT_PARSER}}"

if [[ "${DRY_RUN}" != true && "${NO_RESTART}" != true ]]; then
  VLLM_API_KEY_VAL="$(read_env_var "${ENV_FILE}" "VLLM_API_KEY" "")"
  if [[ -z "${VLLM_API_KEY_VAL}" || "${VLLM_API_KEY_VAL}" == CHANGE_TO_* ]]; then
    echo "Error: VLLM_API_KEY in ${ENV_FILE} must be set to a real API key (not CHANGE_TO_*) before starting vllm." >&2
    exit 1
  fi
fi

if [[ "${DRY_RUN}" == true ]]; then
  echo "[dry-run] Repository:   ${REPO_DIR}"
  echo "[dry-run] Env file:     ${ENV_FILE}"
  echo "[dry-run] VLLM_MODEL:   ${CURRENT_MODEL:-<unset>} -> ${MODEL_ARG}"
  if [[ -n "${PARSER_ARG}" ]]; then
    echo "[dry-run] VLLM_TOOL_CALL_PARSER: ${CURRENT_PARSER:-<unset>} -> ${PARSER_ARG}"
  elif [[ -n "${EFFECTIVE_PARSER}" ]]; then
    echo "[dry-run] VLLM_TOOL_CALL_PARSER: ${EFFECTIVE_PARSER} (unchanged)"
  fi
  if [[ "${NO_RESTART}" == true ]]; then
    echo "[dry-run] Restart:      skipped (--no-restart)"
  else
    echo "[dry-run] Would run:    bash ${REPO_DIR}/scripts/fix-permissions.sh && docker compose --profile vllm up -d --force-recreate vllm"
  fi
  exit 0
fi

python3 - "${ENV_FILE}" "${MODEL_ARG}" "${PARSER_ARG}" <<'PY'
import os
import re
import sys
import tempfile
from pathlib import Path

env_path = Path(sys.argv[1])
model_val = sys.argv[2]
parser_val = sys.argv[3]

text = env_path.read_text(encoding="utf-8")
lines = text.splitlines()

key_re = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
updated_model = False
updated_parser = False
out_lines: list[str] = []

for line in lines:
    match = key_re.match(line)
    if match:
        key = match.group(1)
        if key == "VLLM_MODEL":
            out_lines.append(f"VLLM_MODEL={model_val}")
            updated_model = True
            continue
        if key == "VLLM_TOOL_CALL_PARSER" and parser_val:
            out_lines.append(f"VLLM_TOOL_CALL_PARSER={parser_val}")
            updated_parser = True
            continue
    out_lines.append(line)

if not updated_model:
    out_lines.append(f"VLLM_MODEL={model_val}")
if parser_val and not updated_parser:
    out_lines.append(f"VLLM_TOOL_CALL_PARSER={parser_val}")

new_content = "\n".join(out_lines) + "\n"

fd, tmp_name = tempfile.mkstemp(prefix=f".{env_path.name}.tmp.", dir=str(env_path.parent))
tmp_path = Path(tmp_name)
try:
    os.chmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(new_content)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, env_path)
except BaseException:
    try:
        tmp_path.unlink(missing_ok=True)
    except OSError:
        pass
    raise
PY

echo "Updated ${ENV_FILE} (mode 0600): VLLM_MODEL=${MODEL_ARG}"
if [[ -n "${PARSER_ARG}" ]]; then
  echo "Updated ${ENV_FILE} (mode 0600): VLLM_TOOL_CALL_PARSER=${PARSER_ARG}"
fi

if [[ "${NO_RESTART}" == true ]]; then
  exit 0
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "Error: docker is required to start vllm (or pass --no-restart)" >&2
  exit 1
fi

echo "==> Repairing vLLM cache permissions..."
ENV_FILE="${ENV_FILE}" bash "${REPO_DIR}/scripts/fix-permissions.sh"

echo "==> Recreating vllm service with model '${MODEL_ARG}'..."
(
  cd "${REPO_DIR}"
  docker compose --env-file "${ENV_FILE}" --profile vllm up -d --force-recreate vllm
)

echo "==> vLLM is starting model '${MODEL_ARG}' (follow download/load logs with: docker logs -f vllm)"
