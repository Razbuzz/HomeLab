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

Create a verified HomeLab backup (.env, metadata.env, apps.tar.gz, optional
maintainerr-data.tar.gz, and SHA256SUMS).

Options:
  --backup-dir <dir>   Destination backup directory (must not be inside repo or apps mount)
  --repo-dir <dir>     Path to HomeLab git repository (default: auto-detected)
  --online             Skip stopping/restarting Docker containers during backup
  --no-restart         Stop containers for backup, do not restart them afterward
  --skip-docker        Do not invoke Docker Compose at all (implies --online)
  --dry-run            Show planned backup actions and disk checks without modifying files
  -h, --help           Show this help message
EOF
}

CUSTOM_BACKUP_DIR=""
CUSTOM_REPO_DIR=""
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

dir_size_kb() {
  local dir="$1"
  local output=""
  if output="$(du -sk "${dir}" 2>/dev/null)"; then
    printf '%s\n' "${output%%[[:space:]]*}"
  else
    output="$(run_as_root du -sk "${dir}")"
    printf '%s\n' "${output%%[[:space:]]*}"
  fi
}

# Check available disk space before touching Docker or creating files.
ESTIMATED_KB=1024
apps_kb="$(dir_size_kb "${APPS_MOUNT}")"
ESTIMATED_KB=$((ESTIMATED_KB + apps_kb))
if [[ -n "${MAINTAINERR_DATA_DIR}" && -d "${MAINTAINERR_DATA_DIR}" ]]; then
  maint_kb="$(dir_size_kb "${MAINTAINERR_DATA_DIR}")"
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
  echo "Error: insufficient disk space in ${PROBE_DIR}: available ${AVAIL_KB} KiB, required ${REQUIRED_KB} KiB (source ${ESTIMATED_KB} KiB + 20% headroom)." >&2
  exit 1
fi

if [[ "${DRY_RUN}" == true ]]; then
  echo "[dry-run] Repository:       ${REPO_DIR} (${SHORT_SHA})"
  echo "[dry-run] Apps mount:       ${APPS_MOUNT}"
  if [[ -n "${MAINTAINERR_DATA_DIR}" && -d "${MAINTAINERR_DATA_DIR}" ]]; then
    echo "[dry-run] Maintainerr data: ${MAINTAINERR_DATA_DIR}"
  fi
  echo "[dry-run] Backup directory: ${BACKUP_DIR}"
  echo "[dry-run] Disk check OK:    ${AVAIL_KB} KiB available >= ${REQUIRED_KB} KiB required"
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

cleanup() {
  local rc=$?
  trap - EXIT
  trap '' INT TERM HUP
  set +e
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

if [[ "${SKIP_DOCKER}" != true ]]; then
  if ! command -v docker >/dev/null 2>&1; then
    echo "Error: docker is required (or pass --skip-docker)" >&2
    exit 1
  fi
  detect_beta_running
  if [[ "${ONLINE}" != true ]]; then
    echo "==> Stopping HomeLab stack for crash-consistent backup..."
    STACK_WAS_STOPPED=true
    compose_cmd --profile '*' down
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

APPS_ARCHIVE="apps.tar.gz"
echo "==> Archiving ${APPS_MOUNT} (excluding home-server-dashboard)..."
apps_archive_path="${BACKUP_DIR}/${APPS_ARCHIVE}"
if ! tar --numeric-owner --anchored --exclude='./home-server-dashboard' -C "${APPS_MOUNT}" -czpf "${apps_archive_path}" . 2>/dev/null; then
  echo "    Retrying archive with root privileges for root-owned/mode-restricted files..."
  run_as_root tar --numeric-owner --anchored --exclude='./home-server-dashboard' -C "${APPS_MOUNT}" -czpf "${apps_archive_path}" .
  run_as_root chown "$(id -u):$(id -g)" "${apps_archive_path}"
fi
chmod 600 "${apps_archive_path}"
tar -tzf "${apps_archive_path}" >/dev/null

MAINTAINERR_ARCHIVE=""
if [[ -n "${MAINTAINERR_DATA_DIR}" && -d "${MAINTAINERR_DATA_DIR}" ]]; then
  echo "==> Archiving Maintainerr data (${MAINTAINERR_DATA_DIR})..."
  MAINTAINERR_ARCHIVE="maintainerr-data.tar.gz"
  maint_archive_path="${BACKUP_DIR}/${MAINTAINERR_ARCHIVE}"
  if ! tar --numeric-owner -C "${MAINTAINERR_DATA_DIR}" -czpf "${maint_archive_path}" . 2>/dev/null; then
    run_as_root tar --numeric-owner -C "${MAINTAINERR_DATA_DIR}" -czpf "${maint_archive_path}" .
    run_as_root chown "$(id -u):$(id -g)" "${maint_archive_path}"
  fi
  chmod 600 "${maint_archive_path}"
  tar -tzf "${maint_archive_path}" >/dev/null
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
EOF
chmod 600 "${BACKUP_DIR}/metadata.env"

echo "==> Generating and verifying SHA256SUMS..."
CHECKSUM_FILES=(".env" "metadata.env" "${APPS_ARCHIVE}")
if [[ -n "${MAINTAINERR_ARCHIVE}" ]]; then
  CHECKSUM_FILES+=("${MAINTAINERR_ARCHIVE}")
fi
(
  cd "${BACKUP_DIR}"
  sha256sum "${CHECKSUM_FILES[@]}" > SHA256SUMS
  chmod 600 SHA256SUMS
  sha256sum -c SHA256SUMS >/dev/null
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
