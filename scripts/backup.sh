#!/usr/bin/env bash
set -euo pipefail

# Create a verified, crash-consistent backup of HomeLab configuration (.env)
# and persistent service data (apps mount + optional Maintainerr data).
#
# Can be executed either from inside the repository or from a temporary
# bootstrap directory (e.g. /tmp/hl-up/backup.sh --repo-dir /mnt/HomeLab).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'EOF'
Usage: backup.sh [options]

Create a verified HomeLab backup (.env, metadata.env, apps archive, optional
maintainerr-data archive, and SHA256SUMS).

Options:
  --backup-dir <dir>   Destination backup directory (must not be inside repo or apps mount)
  --repo-dir <dir>     Path to HomeLab git repository (default: auto-detected)
  --include-heavy      Include heavy regenerable paths (Ollama models, Plex Cache/Media/Logs)
  --no-compress        Write uncompressed .tar archives (zero compression CPU overhead)
  --online             Skip stopping/restarting Docker containers during backup
  --no-restart         Stop containers for backup, do not restart them afterward
  --skip-docker        Do not invoke Docker Compose at all (implies --online)
  --dry-run            Show planned backup actions, exclusions, and disk checks without modifying files
  -h, --help           Show this help message
EOF
}

CUSTOM_BACKUP_DIR=""
CUSTOM_REPO_DIR=""
INCLUDE_HEAVY=false
NO_COMPRESS=false
ONLINE=false
NO_RESTART=false
SKIP_DOCKER=false
DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backup-dir)
      [[ $# -ge 2 ]] || { echo "Error: --backup-dir requires an argument" >&2; exit 1; }
      CUSTOM_BACKUP_DIR="$2"
      shift 2
      ;;
    --repo-dir)
      [[ $# -ge 2 ]] || { echo "Error: --repo-dir requires an argument" >&2; exit 1; }
      CUSTOM_REPO_DIR="$2"
      shift 2
      ;;
    --include-heavy)
      INCLUDE_HEAVY=true
      shift
      ;;
    --no-compress)
      NO_COMPRESS=true
      shift
      ;;
    --online)
      ONLINE=true
      shift
      ;;
    --no-restart)
      NO_RESTART=true
      shift
      ;;
    --skip-docker)
      SKIP_DOCKER=true
      shift
      ;;
    --dry-run|-n)
      DRY_RUN=true
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Error: unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

resolve_repo_dir() {
  local candidate=""
  if [[ -n "${CUSTOM_REPO_DIR}" ]]; then
    candidate="${CUSTOM_REPO_DIR}"
  elif [[ -n "${HOMELAB_REPO_DIR:-}" ]]; then
    candidate="${HOMELAB_REPO_DIR}"
  elif candidate="$(git rev-parse --show-toplevel 2>/dev/null)" && [[ -n "${candidate}" ]]; then
    :
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
if ! git -C "${REPO_DIR}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Error: ${REPO_DIR} is not a valid git repository" >&2
  exit 1
fi

ENV_FILE="${REPO_DIR}/.env"
if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Error: .env file not found at ${ENV_FILE}" >&2
  exit 1
fi

# Safely parse simple KEY=VALUE lines without source/eval or shell expansion.
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

resolve_mount_path() {
  local raw_mount="$1"
  local resolved=""
  if [[ -z "${raw_mount}" ]]; then
    resolved=""
  elif [[ "${raw_mount}" == "/" ]]; then
    resolved="/"
  elif [[ "${raw_mount}" == /* ]]; then
    resolved="${raw_mount%/}"
    [[ -z "${resolved}" ]] && resolved="/"
  elif [[ "${raw_mount}" == "." || "${raw_mount}" == "./" ]]; then
    resolved="${REPO_DIR}"
  else
    local rel_mount="${raw_mount#./}"
    rel_mount="${rel_mount%/}"
    if [[ -z "${rel_mount}" || "${rel_mount}" == "." ]]; then
      resolved="${REPO_DIR}"
    else
      resolved="${REPO_DIR}/${rel_mount}"
    fi
  fi
  if [[ -n "${resolved}" ]]; then
    realpath -m "${resolved}"
  fi
}

RAW_APPS_MOUNT="$(read_env_var "${ENV_FILE}" "DEFAULT_APPS_MOUNT" "./apps")"
RAW_STORAGE_MOUNT="$(read_env_var "${ENV_FILE}" "DEFAULT_STORAGE_MOUNT" "")"
MAINTAINERR_FOLDER="$(read_env_var "${ENV_FILE}" "MAINTAINERR_FOLDER" "maintainerr")"
OLLAMA_FOLDER="$(read_env_var "${ENV_FILE}" "OLLAMA_FOLDER" "ollama")"
PLEX_FOLDER="$(read_env_var "${ENV_FILE}" "PLEX_FOLDER" "plex")"
[[ -z "${OLLAMA_FOLDER}" || "${OLLAMA_FOLDER}" == CHANGE_TO_* ]] && OLLAMA_FOLDER="ollama"
[[ -z "${PLEX_FOLDER}" || "${PLEX_FOLDER}" == CHANGE_TO_* ]] && PLEX_FOLDER="plex"
OLLAMA_FOLDER="${OLLAMA_FOLDER#./}"
OLLAMA_FOLDER="${OLLAMA_FOLDER%/}"
PLEX_FOLDER="${PLEX_FOLDER#./}"
PLEX_FOLDER="${PLEX_FOLDER%/}"

APPS_MOUNT="$(resolve_mount_path "${RAW_APPS_MOUNT}")"
STORAGE_MOUNT=""
MAINTAINERR_DATA_DIR=""
if [[ -n "${RAW_STORAGE_MOUNT}" && "${RAW_STORAGE_MOUNT}" != CHANGE_TO_* ]]; then
  STORAGE_MOUNT="$(resolve_mount_path "${RAW_STORAGE_MOUNT}")"
  if [[ -n "${MAINTAINERR_FOLDER}" && "${MAINTAINERR_FOLDER}" != CHANGE_TO_* ]]; then
    MAINTAINERR_DATA_DIR="$(realpath -m "${STORAGE_MOUNT}/${MAINTAINERR_FOLDER}/data")"
  fi
fi

GIT_COMMIT="$(git -C "${REPO_DIR}" rev-parse HEAD)"
SHORT_SHA="$(git -C "${REPO_DIR}" rev-parse --short=8 HEAD)"
GIT_BRANCH="$(git -C "${REPO_DIR}" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
GIT_TAG="$(git -C "${REPO_DIR}" describe --tags --exact-match HEAD 2>/dev/null || true)"
CREATED_AT_UTC="$(date -u +%Y%m%dT%H%M%SZ)"

BACKUP_PARENT_DEFAULT="${HOMELAB_BACKUP_DIR:-$(dirname "${REPO_DIR}")/homelab-backups}"
if [[ -n "${CUSTOM_BACKUP_DIR}" ]]; then
  BACKUP_DIR="$(realpath -m "${CUSTOM_BACKUP_DIR}")"
else
  BACKUP_DIR="$(realpath -m "${BACKUP_PARENT_DEFAULT}/${CREATED_AT_UTC}-${SHORT_SHA}")"
fi
BACKUP_PARENT="$(dirname "${BACKUP_DIR}")"

is_same_or_subpath() {
  local target="$1"
  local parent="$2"
  [[ -n "${parent}" ]] || return 1
  local norm_target norm_parent
  norm_target="$(realpath -m "${target}")"
  norm_parent="$(realpath -m "${parent}")"
  if [[ "${norm_parent}" == "/" ]]; then
    return 0
  fi
  [[ "${norm_target}" == "${norm_parent}" || "${norm_target}" == "${norm_parent}/"* ]]
}

if is_same_or_subpath "${BACKUP_DIR}" "${REPO_DIR}"; then
  echo "Error: backup directory (${BACKUP_DIR}) must NOT be inside repository (${REPO_DIR})" >&2
  exit 1
fi
if is_same_or_subpath "${REPO_DIR}" "${BACKUP_DIR}"; then
  echo "Error: backup directory (${BACKUP_DIR}) must NOT be an ancestor of repository (${REPO_DIR})" >&2
  exit 1
fi
if is_same_or_subpath "${BACKUP_DIR}" "${APPS_MOUNT}"; then
  echo "Error: backup directory (${BACKUP_DIR}) must NOT be inside APPS_MOUNT (${APPS_MOUNT})" >&2
  exit 1
fi
if is_same_or_subpath "${APPS_MOUNT}" "${BACKUP_DIR}"; then
  echo "Error: backup directory (${BACKUP_DIR}) must NOT be an ancestor of APPS_MOUNT (${APPS_MOUNT})" >&2
  exit 1
fi
if [[ -n "${MAINTAINERR_DATA_DIR}" ]] && is_same_or_subpath "${BACKUP_DIR}" "${MAINTAINERR_DATA_DIR}"; then
  echo "Error: backup directory (${BACKUP_DIR}) must NOT be inside Maintainerr data dir (${MAINTAINERR_DATA_DIR})" >&2
  exit 1
fi
if [[ -n "${MAINTAINERR_DATA_DIR}" ]] && is_same_or_subpath "${MAINTAINERR_DATA_DIR}" "${BACKUP_DIR}"; then
  echo "Error: backup directory (${BACKUP_DIR}) must NOT be an ancestor of Maintainerr data dir (${MAINTAINERR_DATA_DIR})" >&2
  exit 1
fi

if [[ -e "${BACKUP_DIR}" ]]; then
  if [[ ! -d "${BACKUP_DIR}" ]]; then
    echo "Error: backup path exists and is not a directory: ${BACKUP_DIR}" >&2
    exit 1
  fi
  if [[ -n "$(ls -A "${BACKUP_DIR}" 2>/dev/null || true)" ]]; then
    echo "Error: backup directory already exists and is not empty: ${BACKUP_DIR}" >&2
    exit 1
  fi
fi

if [[ ! -d "${APPS_MOUNT}" ]]; then
  echo "Error: APPS_MOUNT directory not found: ${APPS_MOUNT}" >&2
  exit 1
fi

APPS_EXCLUDED_PATHS_LIST=("./home-server-dashboard")
if [[ "${INCLUDE_HEAVY}" != true ]]; then
  APPS_EXCLUDED_PATHS_LIST+=(
    "./${OLLAMA_FOLDER}/models"
    "./${PLEX_FOLDER}/config/Library/Application Support/Plex Media Server/Cache"
    "./${PLEX_FOLDER}/config/Library/Application Support/Plex Media Server/Media"
    "./${PLEX_FOLDER}/config/Library/Application Support/Plex Media Server/Crash Reports"
    "./${PLEX_FOLDER}/config/Library/Application Support/Plex Media Server/Diagnostics"
    "./${PLEX_FOLDER}/config/Library/Application Support/Plex Media Server/Logs"
    "./${PLEX_FOLDER}/config/Library/Application Support/Plex Media Server/Updates"
  )
fi

APPS_EXCLUDED_PATHS=""
for rel in "${APPS_EXCLUDED_PATHS_LIST[@]}"; do
  if [[ -z "${APPS_EXCLUDED_PATHS}" ]]; then
    APPS_EXCLUDED_PATHS="${rel}"
  else
    APPS_EXCLUDED_PATHS="${APPS_EXCLUDED_PATHS}|${rel}"
  fi
done

APPS_TAR_EXCLUDE_ARGS=()
APPS_DU_EXCLUDE_ARGS=()
for rel in "${APPS_EXCLUDED_PATHS_LIST[@]}"; do
  APPS_TAR_EXCLUDE_ARGS+=("--exclude=${rel}")
  APPS_DU_EXCLUDE_ARGS+=("--exclude=${rel}")
done

# Compression auto-selection
ARCHIVE_COMPRESSION=""
COMPRESS_LABEL=""
ARCHIVE_EXT=""
TAR_COMPRESS_ARGS=()

if [[ "${NO_COMPRESS}" == true ]]; then
  ARCHIVE_COMPRESSION="none"
  COMPRESS_LABEL="none"
  ARCHIVE_EXT=".tar"
elif command -v zstd >/dev/null 2>&1; then
  ARCHIVE_COMPRESSION="zstd"
  COMPRESS_LABEL="zstd"
  ARCHIVE_EXT=".tar.zst"
  TAR_COMPRESS_ARGS=(-I "zstd -T0 -3")
elif command -v pigz >/dev/null 2>&1; then
  ARCHIVE_COMPRESSION="gzip"
  COMPRESS_LABEL="pigz"
  ARCHIVE_EXT=".tar.gz"
  TAR_COMPRESS_ARGS=(-I "pigz -1")
else
  ARCHIVE_COMPRESSION="gzip"
  COMPRESS_LABEL="gzip -1"
  ARCHIVE_EXT=".tar.gz"
  TAR_COMPRESS_ARGS=(-I "gzip -1")
fi

run_as_root() {
  if [[ "$(id -u)" -eq 0 ]]; then
    "$@"
  else
    if ! command -v sudo >/dev/null 2>&1; then
      echo "Error: root privileges or 'sudo' are required to run: $*" >&2
      return 1
    fi
    sudo "$@"
  fi
}

# Detect unreadable files/directories upfront (pruning excluded heavy paths so
# this completes in milliseconds) so we can pre-authenticate sudo once and run
# du/tar in a single pass instead of retrying after a wasted gzip pass.
path_needs_root() {
  local base_dir="$1"
  shift
  local -a excludes=("$@")
  local find_output=""
  local rc=0

  if [[ "$(id -u)" -eq 0 ]]; then
    return 1
  fi

  local -a find_expr=()
  if [[ "${#excludes[@]}" -gt 0 ]]; then
    find_expr+=("(")
    local first=true
    local ex
    for ex in "${excludes[@]}"; do
      if [[ "${first}" == true ]]; then
        first=false
      else
        find_expr+=("-o")
      fi
      find_expr+=("-path" "${ex}")
    done
    find_expr+=(")" "-prune" "-o")
  fi
  find_expr+=("!" "-type" "l" "!" "-readable" "-print" "-quit")

  find_output="$(
    cd "${base_dir}"
    find . "${find_expr[@]}" 2>&1
  )" || rc=$?

  if [[ -n "${find_output}" || "${rc}" -ne 0 ]]; then
    return 0
  fi
  return 1
}

APPS_NEEDS_ROOT=false
MAINTAINERR_NEEDS_ROOT=false
NEEDS_ROOT=false

if path_needs_root "${APPS_MOUNT}" "${APPS_EXCLUDED_PATHS_LIST[@]}"; then
  APPS_NEEDS_ROOT=true
  NEEDS_ROOT=true
fi
if [[ -n "${MAINTAINERR_DATA_DIR}" && -d "${MAINTAINERR_DATA_DIR}" ]]; then
  if path_needs_root "${MAINTAINERR_DATA_DIR}"; then
    MAINTAINERR_NEEDS_ROOT=true
    NEEDS_ROOT=true
  fi
fi

if [[ "${NEEDS_ROOT}" == true ]]; then
  if ! command -v sudo >/dev/null 2>&1; then
    echo "Error: root-owned or mode-restricted files detected in source data, and 'sudo' is not installed." >&2
    exit 1
  fi
  if [[ "${DRY_RUN}" == true ]]; then
    echo "==> Root privileges required for restricted service directories (needed for size estimation)..."
  fi
  if [[ -t 0 ]]; then
    if [[ "${DRY_RUN}" != true ]]; then
      echo "==> Root privileges required for restricted service directories; verifying sudo credentials..."
    fi
    sudo -v
  fi
fi

dir_size_kb() {
  local dir="$1"
  local use_root="$2"
  shift 2
  local -a exclude_args=("$@")
  local output=""

  if [[ "${use_root}" == true ]]; then
    output="$(
      cd "${dir}"
      run_as_root du -sk "${exclude_args[@]}" .
    )"
  else
    if ! output="$(
      cd "${dir}"
      du -sk "${exclude_args[@]}" . 2>/dev/null
    )"; then
      output="$(
        cd "${dir}"
        run_as_root du -sk "${exclude_args[@]}" .
      )"
    fi
  fi
  printf '%s\n' "${output%%[[:space:]]*}"
}

format_size_kb() {
  local kb="$1"
  awk -v k="${kb}" 'BEGIN {
    if (k >= 1048576) {
      printf "%.2f GiB", k / 1048576
    } else {
      printf "%.1f MiB", k / 1024
    }
  }'
}

# Check available disk space before touching Docker or creating files (single pass).
ESTIMATED_KB=1024
apps_kb="$(dir_size_kb "${APPS_MOUNT}" "${APPS_NEEDS_ROOT}" "${APPS_DU_EXCLUDE_ARGS[@]}")"
ESTIMATED_KB=$((ESTIMATED_KB + apps_kb))
if [[ -n "${MAINTAINERR_DATA_DIR}" && -d "${MAINTAINERR_DATA_DIR}" ]]; then
  maint_kb="$(dir_size_kb "${MAINTAINERR_DATA_DIR}" "${MAINTAINERR_NEEDS_ROOT}")"
  ESTIMATED_KB=$((ESTIMATED_KB + maint_kb))
fi
REQUIRED_KB=$(( (ESTIMATED_KB * 120 + 99) / 100 ))

PROBE_DIR="${BACKUP_PARENT}"
while [[ ! -e "${PROBE_DIR}" && "${PROBE_DIR}" != "/" ]]; do
  PROBE_DIR="$(dirname "${PROBE_DIR}")"
done
AVAIL_KB="$(df -Pk "${PROBE_DIR}" | awk 'NR==2 {print $4}')"
if [[ -z "${AVAIL_KB}" || ! "${AVAIL_KB}" =~ ^[0-9]+$ ]]; then
  echo "Error: unable to determine available disk space on ${PROBE_DIR}" >&2
  exit 1
fi
if (( AVAIL_KB < REQUIRED_KB )); then
  echo "Error: insufficient disk space in ${PROBE_DIR}: available ${AVAIL_KB} KiB ($(format_size_kb "${AVAIL_KB}")), required ${REQUIRED_KB} KiB ($(format_size_kb "${REQUIRED_KB}"))." >&2
  exit 1
fi

if [[ "${DRY_RUN}" == true ]]; then
  echo "[dry-run] Repository:       ${REPO_DIR} (${SHORT_SHA})"
  echo "[dry-run] Apps mount:       ${APPS_MOUNT}"
  if [[ -n "${MAINTAINERR_DATA_DIR}" && -d "${MAINTAINERR_DATA_DIR}" ]]; then
    echo "[dry-run] Maintainerr data: ${MAINTAINERR_DATA_DIR}"
  fi
  echo "[dry-run] Backup directory: ${BACKUP_DIR}"
  echo "[dry-run] Compression:      ${COMPRESS_LABEL} (archive: apps${ARCHIVE_EXT})"
  echo "[dry-run] Needs sudo:       ${NEEDS_ROOT}"
  echo "[dry-run] Estimated size:   $(format_size_kb "${ESTIMATED_KB}") (${ESTIMATED_KB} KiB; required with 20% headroom: $(format_size_kb "${REQUIRED_KB}"))"
  echo "[dry-run] Available space:  $(format_size_kb "${AVAIL_KB}") (${AVAIL_KB} KiB on ${PROBE_DIR})"
  if [[ "${INCLUDE_HEAVY}" == true ]]; then
    echo "[dry-run] Exclusions:       ./home-server-dashboard only (--include-heavy enabled)"
  else
    echo "[dry-run] Exclusions (preserved untouched on rollback; pass --include-heavy to archive):"
    for rel in "${APPS_EXCLUDED_PATHS_LIST[@]}"; do
      rel_clean="${rel#./}"
      ex_path="${APPS_MOUNT}/${rel_clean}"
      if [[ -e "${ex_path}" ]]; then
        ex_kb=""
        if ex_kb_line="$(du -sk "${ex_path}" 2>/dev/null)"; then
          ex_kb="${ex_kb_line%%[[:space:]]*}"
          echo "[dry-run]   - ${rel} ($(format_size_kb "${ex_kb}"))"
        else
          echo "[dry-run]   - ${rel} (present; size requires root)"
        fi
      else
        echo "[dry-run]   - ${rel} (not present)"
      fi
    done
  fi
  if [[ "${SKIP_DOCKER}" == true ]]; then
    echo "[dry-run] Docker:           skipped (--skip-docker)"
  elif [[ "${ONLINE}" == true ]]; then
    echo "[dry-run] Docker:           online backup (stack left running)"
  else
    restart_note="true"
    [[ "${NO_RESTART}" == true ]] && restart_note="false"
    echo "[dry-run] Docker:           would stop stack for crash-consistent backup (restart=${restart_note})"
  fi
  exit 0
fi

HAD_BETA_RUNNING=false
STACK_WAS_STOPPED=false
BACKUP_CREATED_BY_US=false
BACKUP_COMPLETED=false
ACTIVE_HB_PID=""
ACTIVE_CMD_PID=""

compose_cmd() {
  (
    cd "${REPO_DIR}"
    docker compose "$@"
  )
}

detect_beta_running() {
  local cid=""
  cid="$(compose_cmd --profile beta ps -q hermes-agent 2>/dev/null || true)"
  if [[ -n "${cid}" ]]; then
    local running=""
    running="$(docker inspect -f '{{.State.Running}}' "${cid}" 2>/dev/null || echo "false")"
    if [[ "${running}" == "true" ]]; then
      HAD_BETA_RUNNING=true
      return 0
    fi
  fi
  HAD_BETA_RUNNING=false
}

restart_stack() {
  echo "==> Restarting HomeLab stack..."
  if [[ "${HAD_BETA_RUNNING}" == true ]]; then
    compose_cmd --profile beta up -d --remove-orphans
  else
    compose_cmd up -d --remove-orphans
  fi
  STACK_WAS_STOPPED=false
}

collect_descendant_pids() {
  local parent_pid="$1"
  local child_pid=""
  while IFS= read -r child_pid; do
    [[ -n "${child_pid}" ]] || continue
    collect_descendant_pids "${child_pid}"
    printf '%s\n' "${child_pid}"
  done < <(pgrep -P "${parent_pid}" 2>/dev/null || true)
}

signal_pid_with_privs() {
  local sig="$1"
  local pid="$2"
  kill "-${sig}" "${pid}" 2>/dev/null && return 0
  if [[ "$(id -u)" -ne 0 ]] && command -v sudo >/dev/null 2>&1; then
    sudo kill "-${sig}" "${pid}" 2>/dev/null && return 0
  fi
  return 1
}

pid_is_alive() {
  local pid="$1"
  kill -0 "${pid}" 2>/dev/null && return 0
  if [[ "$(id -u)" -ne 0 ]] && command -v sudo >/dev/null 2>&1; then
    sudo kill -0 "${pid}" 2>/dev/null && return 0
  fi
  return 1
}

stop_active_cmd() {
  local pid="${ACTIVE_CMD_PID}"
  [[ -n "${pid}" ]] || return 0
  ACTIVE_CMD_PID=""

  local -a target_pids=()
  local desc_pid=""
  while IFS= read -r desc_pid; do
    [[ -n "${desc_pid}" ]] && target_pids+=("${desc_pid}")
  done < <(collect_descendant_pids "${pid}")
  target_pids+=("${pid}")

  local p=""
  for p in "${target_pids[@]}"; do
    if pid_is_alive "${p}"; then
      signal_pid_with_privs TERM "${p}" || true
    fi
  done

  local _ alive
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    alive=false
    for p in "${target_pids[@]}"; do
      if pid_is_alive "${p}"; then
        alive=true
        break
      fi
    done
    [[ "${alive}" == true ]] || break
    sleep 0.2
  done

  for p in "${target_pids[@]}"; do
    if pid_is_alive "${p}"; then
      signal_pid_with_privs KILL "${p}" || true
    fi
  done
  wait "${pid}" 2>/dev/null || true
}

cleanup() {
  local rc=$?
  trap - EXIT
  trap '' INT TERM HUP
  set +e
  if [[ -n "${ACTIVE_HB_PID}" ]]; then
    kill -TERM "${ACTIVE_HB_PID}" 2>/dev/null || true
    wait "${ACTIVE_HB_PID}" 2>/dev/null || true
    ACTIVE_HB_PID=""
  fi
  stop_active_cmd
  if [[ "${BACKUP_COMPLETED}" != true && "${BACKUP_CREATED_BY_US}" == true && -d "${BACKUP_DIR}" ]]; then
    rm -rf "${BACKUP_DIR}" 2>/dev/null || true
  fi
  if [[ "${SKIP_DOCKER}" != true && "${STACK_WAS_STOPPED}" == true ]] && [[ "${BACKUP_COMPLETED}" != true || "${NO_RESTART}" != true ]]; then
    restart_stack || echo "Warning: failed to restart Docker stack during cleanup" >&2
  fi
  if [[ "${BACKUP_COMPLETED}" != true && "${rc}" -eq 0 ]]; then
    rc=1
  fi
  exit "${rc}"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

run_with_heartbeat() {
  local label="$1"
  local watch_file="$2"
  shift 2

  "$@" &
  local cmd_pid=$!
  ACTIVE_CMD_PID="${cmd_pid}"

  (
    trap '[[ -n "${sleep_pid:-}" ]] && kill "${sleep_pid}" 2>/dev/null || true; exit 0' TERM INT
    local start_ts=${SECONDS}
    local next_report=5
    local next_sudo_refresh=60
    local sleep_pid=""
    while kill -0 "${cmd_pid}" 2>/dev/null; do
      sleep 1 &
      sleep_pid=$!
      wait "${sleep_pid}" 2>/dev/null || true
      sleep_pid=""
      if ! kill -0 "${cmd_pid}" 2>/dev/null; then
        break
      fi
      local elapsed=$((SECONDS - start_ts))
      if (( elapsed >= next_sudo_refresh )); then
        if [[ "$(id -u)" -ne 0 ]] && command -v sudo >/dev/null 2>&1; then
          sudo -n -v 2>/dev/null || true
        fi
        next_sudo_refresh=$((next_sudo_refresh + 60))
      fi
      if (( elapsed >= next_report )); then
        local bytes=0
        if [[ -n "${watch_file}" && -f "${watch_file}" ]]; then
          bytes="$(stat -c '%s' "${watch_file}" 2>/dev/null || echo 0)"
        fi
        local mib=$(( (bytes + 524288) / 1048576 ))
        printf '    ... archiving %s: %s MiB written (%ss elapsed)\n' "${label}" "${mib}" "${elapsed}"
        next_report=$((next_report + 5))
      fi
    done
  ) &
  ACTIVE_HB_PID=$!

  local rc=0
  wait "${cmd_pid}" || rc=$?
  ACTIVE_CMD_PID=""
  kill -TERM "${ACTIVE_HB_PID}" 2>/dev/null || true
  wait "${ACTIVE_HB_PID}" 2>/dev/null || true
  ACTIVE_HB_PID=""
  return "${rc}"
}

verify_archive_stream() {
  local archive_path="$1"
  case "${ARCHIVE_COMPRESSION}" in
    zstd)
      zstd -t -q "${archive_path}"
      ;;
    gzip)
      gzip -t "${archive_path}"
      ;;
    none)
      tar -tf "${archive_path}" >/dev/null
      ;;
    *)
      echo "Error: unsupported ARCHIVE_COMPRESSION '${ARCHIVE_COMPRESSION}'" >&2
      return 1
      ;;
  esac
}

if [[ "${SKIP_DOCKER}" != true ]]; then
  if ! command -v docker >/dev/null 2>&1; then
    echo "Error: docker is required (or pass --skip-docker)" >&2
    exit 1
  fi
  detect_beta_running
  if [[ "${ONLINE}" != true ]]; then
    echo "==> Stopping HomeLab stack for crash-consistent backup..."
    STACK_WAS_STOPPED=true
    compose_cmd --profile '*' down || compose_cmd down
  fi
fi

if [[ ! -d "${BACKUP_DIR}" ]]; then
  mkdir -p "${BACKUP_DIR}"
  BACKUP_CREATED_BY_US=true
fi
chmod 700 "${BACKUP_DIR}"

echo "==> Backing up .env..."
cp -p "${ENV_FILE}" "${BACKUP_DIR}/.env"
chmod 600 "${BACKUP_DIR}/.env"
SRC_ENV_SHA="$(sha256sum "${ENV_FILE}" | awk '{print $1}')"
DST_ENV_SHA="$(sha256sum "${BACKUP_DIR}/.env" | awk '{print $1}')"
if [[ "${SRC_ENV_SHA}" != "${DST_ENV_SHA}" ]]; then
  echo "Error: copied .env SHA-256 mismatch!" >&2
  exit 1
fi

APPS_ARCHIVE="apps${ARCHIVE_EXT}"
if [[ "${INCLUDE_HEAVY}" == true ]]; then
  echo "==> Archiving ${APPS_MOUNT} (${COMPRESS_LABEL}; excluding home-server-dashboard, including heavy paths)..."
else
  echo "==> Archiving ${APPS_MOUNT} (${COMPRESS_LABEL}; excluding home-server-dashboard + regenerable models/caches)..."
fi
apps_archive_path="${BACKUP_DIR}/${APPS_ARCHIVE}"
if [[ "${APPS_NEEDS_ROOT}" == true ]]; then
  run_with_heartbeat "${APPS_ARCHIVE}" "${apps_archive_path}" \
    run_as_root tar --numeric-owner --anchored "${APPS_TAR_EXCLUDE_ARGS[@]}" "${TAR_COMPRESS_ARGS[@]}" -C "${APPS_MOUNT}" -cpf "${apps_archive_path}" .
  run_as_root chown "$(id -u):$(id -g)" "${apps_archive_path}"
else
  run_with_heartbeat "${APPS_ARCHIVE}" "${apps_archive_path}" \
    tar --numeric-owner --anchored "${APPS_TAR_EXCLUDE_ARGS[@]}" "${TAR_COMPRESS_ARGS[@]}" -C "${APPS_MOUNT}" -cpf "${apps_archive_path}" .
fi
chmod 600 "${apps_archive_path}"
echo "==> Verifying ${APPS_ARCHIVE} integrity..."
verify_archive_stream "${apps_archive_path}"

MAINTAINERR_ARCHIVE=""
if [[ -n "${MAINTAINERR_DATA_DIR}" && -d "${MAINTAINERR_DATA_DIR}" ]]; then
  echo "==> Archiving Maintainerr data (${MAINTAINERR_DATA_DIR}, ${COMPRESS_LABEL})..."
  MAINTAINERR_ARCHIVE="maintainerr-data${ARCHIVE_EXT}"
  maint_archive_path="${BACKUP_DIR}/${MAINTAINERR_ARCHIVE}"
  if [[ "${MAINTAINERR_NEEDS_ROOT}" == true ]]; then
    run_with_heartbeat "${MAINTAINERR_ARCHIVE}" "${maint_archive_path}" \
      run_as_root tar --numeric-owner "${TAR_COMPRESS_ARGS[@]}" -C "${MAINTAINERR_DATA_DIR}" -cpf "${maint_archive_path}" .
    run_as_root chown "$(id -u):$(id -g)" "${maint_archive_path}"
  else
    run_with_heartbeat "${MAINTAINERR_ARCHIVE}" "${maint_archive_path}" \
      tar --numeric-owner "${TAR_COMPRESS_ARGS[@]}" -C "${MAINTAINERR_DATA_DIR}" -cpf "${maint_archive_path}" .
  fi
  chmod 600 "${maint_archive_path}"
  echo "==> Verifying ${MAINTAINERR_ARCHIVE} integrity..."
  verify_archive_stream "${maint_archive_path}"
fi

cat > "${BACKUP_DIR}/metadata.env" <<EOF
BACKUP_VERSION=1
CREATED_AT_UTC=${CREATED_AT_UTC}
REPO_DIR=${REPO_DIR}
GIT_COMMIT=${GIT_COMMIT}
GIT_BRANCH=${GIT_BRANCH}
GIT_TAG=${GIT_TAG}
APPS_MOUNT=${APPS_MOUNT}
STORAGE_MOUNT=${STORAGE_MOUNT}
MAINTAINERR_FOLDER=${MAINTAINERR_FOLDER}
HAD_BETA_RUNNING=${HAD_BETA_RUNNING}
ENV_SHA256=${SRC_ENV_SHA}
APPS_ARCHIVE=${APPS_ARCHIVE}
MAINTAINERR_ARCHIVE=${MAINTAINERR_ARCHIVE}
ARCHIVE_COMPRESSION=${ARCHIVE_COMPRESSION}
APPS_EXCLUDED_PATHS=${APPS_EXCLUDED_PATHS}
EOF
chmod 600 "${BACKUP_DIR}/metadata.env"

echo "==> Computing SHA256SUMS..."
CHECKSUM_FILES=(".env" "metadata.env" "${APPS_ARCHIVE}")
if [[ -n "${MAINTAINERR_ARCHIVE}" ]]; then
  CHECKSUM_FILES+=("${MAINTAINERR_ARCHIVE}")
fi
(
  cd "${BACKUP_DIR}"
  sha256sum "${CHECKSUM_FILES[@]}" > SHA256SUMS
  chmod 600 SHA256SUMS
  # Verify small metadata/env entries against SHA256SUMS without re-reading multi-GB archives.
  grep -E '  (\.env|metadata\.env)$' SHA256SUMS | sha256sum -c - >/dev/null
)

BACKUP_COMPLETED=true
if [[ -z "${CUSTOM_BACKUP_DIR}" ]]; then
  ln -sfn "${BACKUP_DIR}" "${BACKUP_PARENT}/latest"
fi

if [[ "${SKIP_DOCKER}" != true && "${STACK_WAS_STOPPED}" == true && "${NO_RESTART}" != true ]]; then
  restart_stack
fi

echo "==> Backup verified successfully."
echo "BACKUP_PATH=${BACKUP_DIR}"
