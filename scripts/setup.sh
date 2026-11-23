#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

run_as_root() {
  if [[ "$(id -u)" -eq 0 ]]; then
    "$@"
  else
    if ! command -v sudo >/dev/null 2>&1; then
      echo "Error: root privileges or 'sudo' are required to run: $*" >&2
      exit 1
    fi
    sudo "$@"
  fi
}

# Safely parse simple KEY=VALUE lines from an env file without executing shell
# code or expanding `$` / `&` / `;` / spaces in Compose secrets.
# Preserves variables already set in the caller's environment.
load_env_file() {
  local env_file="$1"
  [[ -f "${env_file}" ]] || return 0

  local -A _caller_set=()
  local _var
  for _var in DEFAULT_PUID DEFAULT_GUID DEFAULT_STORAGE_MOUNT; do
    if [[ -n "${!_var+x}" ]]; then
      _caller_set["${_var}"]=1
    fi
  done

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
    [[ "${key}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue

    case "${key}" in
      DEFAULT_PUID|DEFAULT_GUID|DEFAULT_STORAGE_MOUNT)
        ;;
      *)
        continue
        ;;
    esac
    if [[ -n "${_caller_set[${key}]+x}" ]]; then
      continue
    fi

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

    printf -v "${key}" '%s' "${val}"
  done < "${env_file}"
}

# 0. Load and validate required configuration from .env before making changes
ENV_FILE="${ENV_FILE:-${REPO_ROOT}/.env}"
load_env_file "${ENV_FILE}"

DEFAULT_PUID="${DEFAULT_PUID:-}"
DEFAULT_GUID="${DEFAULT_GUID:-}"
DEFAULT_STORAGE_MOUNT="${DEFAULT_STORAGE_MOUNT:-}"

if [[ ! "${DEFAULT_PUID}" =~ ^[0-9]+$ ]]; then
  echo "Error: DEFAULT_PUID must be a non-negative integer (got '${DEFAULT_PUID}'). Please configure ${ENV_FILE} before running setup." >&2
  exit 1
fi
if [[ ! "${DEFAULT_GUID}" =~ ^[0-9]+$ ]]; then
  echo "Error: DEFAULT_GUID must be a non-negative integer (got '${DEFAULT_GUID}'). Please configure ${ENV_FILE} before running setup." >&2
  exit 1
fi
if [[ -z "${DEFAULT_STORAGE_MOUNT}" || "${DEFAULT_STORAGE_MOUNT}" == CHANGE_TO_* ]]; then
  echo "Error: DEFAULT_STORAGE_MOUNT is not configured (got '${DEFAULT_STORAGE_MOUNT}'). Please edit ${ENV_FILE} before running setup." >&2
  exit 1
fi
if [[ -L "${DEFAULT_STORAGE_MOUNT}" ]]; then
  echo "Error: refusing to chown symlink DEFAULT_STORAGE_MOUNT (${DEFAULT_STORAGE_MOUNT})." >&2
  exit 1
fi
if [[ ! -d "${DEFAULT_STORAGE_MOUNT}" ]]; then
  echo "Error: DEFAULT_STORAGE_MOUNT directory '${DEFAULT_STORAGE_MOUNT}' does not exist. Create or mount it and update ${ENV_FILE} before running setup." >&2
  exit 1
fi

# 1. Enable IP forwarding persistently and idempotently
run_as_root tee /etc/sysctl.d/99-homelab.conf >/dev/null <<'EOF'
net.ipv4.ip_forward=1
net.ipv6.conf.all.forwarding=1
EOF
run_as_root sysctl -p /etc/sysctl.d/99-homelab.conf

# 2. Ensure bind-mount app directories exist with expected non-root ownership
"${SCRIPT_DIR}/fix-permissions.sh"

# 3. Start containers (Docker reads .env automatically)
(
  cd "${REPO_ROOT}"
  docker compose up -d --pull always --build --remove-orphans
)

# 4. Fix storage mount permissions using DEFAULT_PUID/DEFAULT_GUID
run_as_root chown -hR "${DEFAULT_PUID}:${DEFAULT_GUID}" "${DEFAULT_STORAGE_MOUNT}"

# 5. Restart services to apply storage permission changes, then stop stack
(
  cd "${REPO_ROOT}"
  docker compose restart
  docker compose down
)
