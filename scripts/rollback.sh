#!/usr/bin/env bash
set -euo pipefail

# Restore HomeLab to a verified backup snapshot (git commit, .env, apps data,
# optional Maintainerr data, and Docker Compose stack without pulling new images).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'EOF'
Usage: rollback.sh [options] [backup_dir]

Restore HomeLab from a verified backup created by backup.sh.

Options:
  --backup <dir>       Backup directory to restore (default: <backup_parent>/latest)
  --repo-dir <dir>     Path to HomeLab git repository (default: auto-detected)
  --yes                Skip interactive confirmation prompt (required in non-TTY mode)
  --no-start           Restore git, .env, and data without starting Docker containers
  --skip-docker        Do not invoke Docker Compose at all
  --dry-run            Verify backup integrity and print planned actions without modifying anything
  -h, --help           Show this help message
EOF
}

CUSTOM_BACKUP_DIR=""
CUSTOM_REPO_DIR=""
ASSUME_YES=false
NO_START=false
SKIP_DOCKER=false
DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backup)
      [[ $# -ge 2 ]] || { echo "Error: --backup requires an argument" >&2; exit 1; }
      CUSTOM_BACKUP_DIR="$2"
      shift 2
      ;;
    --repo-dir)
      [[ $# -ge 2 ]] || { echo "Error: --repo-dir requires an argument" >&2; exit 1; }
      CUSTOM_REPO_DIR="$2"
      shift 2
      ;;
    --yes|-y)
      ASSUME_YES=true
      shift
      ;;
    --no-start)
      NO_START=true
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
    -*)
      echo "Error: unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
    *)
      if [[ -n "${CUSTOM_BACKUP_DIR}" ]]; then
        echo "Error: unexpected positional argument: $1" >&2
        usage >&2
        exit 1
      fi
      CUSTOM_BACKUP_DIR="$1"
      shift
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

BACKUP_PARENT_DEFAULT="${HOMELAB_BACKUP_DIR:-$(dirname "${REPO_DIR}")/homelab-backups}"
if [[ -z "${CUSTOM_BACKUP_DIR}" ]]; then
  CUSTOM_BACKUP_DIR="${BACKUP_PARENT_DEFAULT}/latest"
fi
if [[ ! -e "${CUSTOM_BACKUP_DIR}" ]]; then
  echo "Error: backup directory does not exist: ${CUSTOM_BACKUP_DIR}" >&2
  exit 1
fi
BACKUP_DIR="$(realpath "${CUSTOM_BACKUP_DIR}")"
if [[ ! -d "${BACKUP_DIR}" ]]; then
  echo "Error: backup path is not a directory: ${BACKUP_DIR}" >&2
  exit 1
fi

# Safely parse simple KEY=VALUE lines without source/eval or shell expansion.
read_kv_var() {
  local kv_file="$1"
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
  done < "${kv_file}"

  if [[ "${found}" == true ]]; then
    printf '%s\n' "${result}"
  else
    printf '%s\n' "${default_val}"
  fi
}

for required_file in "${BACKUP_DIR}/metadata.env" "${BACKUP_DIR}/SHA256SUMS" "${BACKUP_DIR}/.env"; do
  if [[ ! -f "${required_file}" ]]; then
    echo "Error: required backup file missing: ${required_file}" >&2
    exit 1
  fi
done

echo "==> Verifying backup checksums in ${BACKUP_DIR}..."
(
  cd "${BACKUP_DIR}"
  sha256sum -c SHA256SUMS >/dev/null
)

BACKUP_VERSION="$(read_kv_var "${BACKUP_DIR}/metadata.env" "BACKUP_VERSION" "")"
GIT_COMMIT="$(read_kv_var "${BACKUP_DIR}/metadata.env" "GIT_COMMIT" "")"
GIT_BRANCH="$(read_kv_var "${BACKUP_DIR}/metadata.env" "GIT_BRANCH" "")"
APPS_MOUNT="$(read_kv_var "${BACKUP_DIR}/metadata.env" "APPS_MOUNT" "")"
STORAGE_MOUNT="$(read_kv_var "${BACKUP_DIR}/metadata.env" "STORAGE_MOUNT" "")"
MAINTAINERR_FOLDER="$(read_kv_var "${BACKUP_DIR}/metadata.env" "MAINTAINERR_FOLDER" "")"
HAD_BETA_RUNNING="$(read_kv_var "${BACKUP_DIR}/metadata.env" "HAD_BETA_RUNNING" "false")"
ENV_SHA256="$(read_kv_var "${BACKUP_DIR}/metadata.env" "ENV_SHA256" "")"
APPS_ARCHIVE="$(read_kv_var "${BACKUP_DIR}/metadata.env" "APPS_ARCHIVE" "")"
MAINTAINERR_ARCHIVE="$(read_kv_var "${BACKUP_DIR}/metadata.env" "MAINTAINERR_ARCHIVE" "")"

if [[ "${BACKUP_VERSION}" != "1" ]]; then
  echo "Error: unsupported BACKUP_VERSION '${BACKUP_VERSION}' in metadata.env" >&2
  exit 1
fi
if [[ -z "${GIT_COMMIT}" || ! "${GIT_COMMIT}" =~ ^[0-9a-fA-F]{7,40}$ ]]; then
  echo "Error: invalid GIT_COMMIT in metadata.env: '${GIT_COMMIT}'" >&2
  exit 1
fi
if ! git -C "${REPO_DIR}" cat-file -e "${GIT_COMMIT}^{commit}" 2>/dev/null; then
  echo "Error: backup commit ${GIT_COMMIT} does not exist in ${REPO_DIR}" >&2
  exit 1
fi
if [[ -z "${APPS_MOUNT}" ]]; then
  echo "Error: APPS_MOUNT missing from metadata.env" >&2
  exit 1
fi

ACTUAL_BACKUP_ENV_SHA="$(sha256sum "${BACKUP_DIR}/.env" | awk '{print $1}')"
if [[ -z "${ENV_SHA256}" || "${ACTUAL_BACKUP_ENV_SHA}" != "${ENV_SHA256}" ]]; then
  echo "Error: backup .env SHA-256 mismatch against metadata.env" >&2
  exit 1
fi

if [[ -n "${APPS_ARCHIVE}" ]]; then
  if [[ ! -f "${BACKUP_DIR}/${APPS_ARCHIVE}" ]]; then
    echo "Error: missing apps archive: ${BACKUP_DIR}/${APPS_ARCHIVE}" >&2
    exit 1
  fi
  echo "==> Verifying ${APPS_ARCHIVE} readability..."
  tar -tzf "${BACKUP_DIR}/${APPS_ARCHIVE}" >/dev/null
fi

MAINTAINERR_DATA_DIR=""
if [[ -n "${MAINTAINERR_ARCHIVE}" ]]; then
  if [[ ! -f "${BACKUP_DIR}/${MAINTAINERR_ARCHIVE}" ]]; then
    echo "Error: missing Maintainerr archive: ${BACKUP_DIR}/${MAINTAINERR_ARCHIVE}" >&2
    exit 1
  fi
  if [[ -z "${STORAGE_MOUNT}" || -z "${MAINTAINERR_FOLDER}" ]]; then
    echo "Error: STORAGE_MOUNT or MAINTAINERR_FOLDER missing for Maintainerr restore" >&2
    exit 1
  fi
  MAINTAINERR_DATA_DIR="$(realpath -m "${STORAGE_MOUNT}/${MAINTAINERR_FOLDER}/data")"
  echo "==> Verifying ${MAINTAINERR_ARCHIVE} readability..."
  tar -tzf "${BACKUP_DIR}/${MAINTAINERR_ARCHIVE}" >/dev/null
fi

if [[ "${DRY_RUN}" == true ]]; then
  echo "[dry-run] Backup verified: ${BACKUP_DIR}"
  echo "[dry-run] Would restore repository ${REPO_DIR} to commit ${GIT_COMMIT} (branch: ${GIT_BRANCH:-detached})"
  echo "[dry-run] Would restore .env (sha256: ${ENV_SHA256})"
  if [[ -n "${APPS_ARCHIVE}" ]]; then
    echo "[dry-run] Would restore ${APPS_MOUNT} from ${APPS_ARCHIVE} (preserving home-server-dashboard)"
  fi
  if [[ -n "${MAINTAINERR_ARCHIVE}" ]]; then
    echo "[dry-run] Would restore ${MAINTAINERR_DATA_DIR} from ${MAINTAINERR_ARCHIVE}"
  fi
  if [[ "${SKIP_DOCKER}" == true ]]; then
    echo "[dry-run] Docker: skipped (--skip-docker)"
  elif [[ "${NO_START}" == true ]]; then
    echo "[dry-run] Docker: would stop stack and leave stopped (--no-start)"
  else
    echo "[dry-run] Docker: would restart restored stack without pulling (beta=${HAD_BETA_RUNNING})"
  fi
  exit 0
fi

if [[ "${ASSUME_YES}" != true ]]; then
  if [[ ! -t 0 ]]; then
    echo "Error: non-interactive rollback requires --yes" >&2
    exit 1
  fi
  echo "About to roll back HomeLab:"
  echo "  Repository: ${REPO_DIR}"
  echo "  Target ref: ${GIT_COMMIT} (${GIT_BRANCH:-detached})"
  echo "  Backup:     ${BACKUP_DIR}"
  printf "Proceed with rollback? [y/N] "
  read -r reply
  if [[ ! "${reply}" =~ ^[Yy]([Ee][Ss])?$ ]]; then
    echo "Rollback cancelled."
    exit 1
  fi
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

mkdir_with_privs() {
  local dir="$1"
  mkdir -p "${dir}" 2>/dev/null || run_as_root mkdir -p "${dir}"
}

mv_with_privs() {
  local src="$1"
  local dst="$2"
  mv "${src}" "${dst}" 2>/dev/null || run_as_root mv "${src}" "${dst}"
}

rm_rf_with_privs() {
  local target="$1"
  rm -rf "${target}" 2>/dev/null || run_as_root rm -rf "${target}"
}

archive_has_foreign_owners() {
  local archive="$1"
  local cur_uid cur_gid
  cur_uid="$(id -u)"
  cur_gid="$(id -g)"
  local owners
  if ! owners="$(tar --numeric-owner -tvzf "${archive}" | awk '{print $2}')"; then
    return 0
  fi
  local pair
  while IFS= read -r pair; do
    [[ -z "${pair}" ]] && continue
    if [[ "${pair}" != "${cur_uid}/${cur_gid}" ]]; then
      return 0
    fi
  done <<< "${owners}"
  return 1
}

tar_extract() {
  local archive="$1"
  local target_dir="$2"
  if [[ "$(id -u)" -ne 0 ]] && archive_has_foreign_owners "${archive}"; then
    run_as_root tar --same-owner --numeric-owner -xzpf "${archive}" -C "${target_dir}"
    return $?
  fi
  if ! tar --same-owner --numeric-owner -xzpf "${archive}" -C "${target_dir}" 2>/dev/null; then
    run_as_root tar --same-owner --numeric-owner -xzpf "${archive}" -C "${target_dir}"
  fi
}

safe_restore_dir() {
  local target_dir="$1"
  local archive_path="$2"
  local preserve_dashboard="$3"

  mkdir_with_privs "${target_dir}"

  local parent_dir stage_dir
  parent_dir="$(dirname "${target_dir}")"
  mkdir_with_privs "${parent_dir}"

  local prev_int_trap prev_term_trap prev_hup_trap
  prev_int_trap="$(trap -p INT || true)"
  prev_term_trap="$(trap -p TERM || true)"
  prev_hup_trap="$(trap -p HUP || true)"
  trap '' INT TERM HUP

  restore_signal_traps() {
    if [[ -n "${prev_int_trap}" ]]; then eval "${prev_int_trap}"; else trap - INT; fi
    if [[ -n "${prev_term_trap}" ]]; then eval "${prev_term_trap}"; else trap - TERM; fi
    if [[ -n "${prev_hup_trap}" ]]; then eval "${prev_hup_trap}"; else trap - HUP; fi
  }

  stage_dir="$(mktemp -d "${parent_dir}/.homelab-restore-stage.XXXXXX" 2>/dev/null || run_as_root mktemp -d "${parent_dir}/.homelab-restore-stage.XXXXXX")" || {
    restore_signal_traps
    return 1
  }

  local moved_items=()
  local entry base
  shopt -s dotglob nullglob
  for entry in "${target_dir}"/*; do
    base="$(basename "${entry}")"
    if [[ "${preserve_dashboard}" == "true" && "${base}" == "home-server-dashboard" ]]; then
      continue
    fi
    if ! mv_with_privs "${entry}" "${stage_dir}/${base}"; then
      shopt -u dotglob nullglob
      local item
      for item in "${moved_items[@]}"; do
        if [[ -e "${stage_dir}/${item}" || -L "${stage_dir}/${item}" ]]; then
          mv_with_privs "${stage_dir}/${item}" "${target_dir}/${item}" || true
        fi
      done
      rm_rf_with_privs "${stage_dir}" || true
      restore_signal_traps
      return 1
    fi
    moved_items+=("${base}")
  done
  shopt -u dotglob nullglob

  if ! tar_extract "${archive_path}" "${target_dir}"; then
    echo "Error: archive extraction failed; restoring previous directory contents..." >&2
    local item
    shopt -s dotglob nullglob
    for entry in "${target_dir}"/*; do
      base="$(basename "${entry}")"
      if [[ "${preserve_dashboard}" == "true" && "${base}" == "home-server-dashboard" ]]; then
        continue
      fi
      rm_rf_with_privs "${entry}"
    done
    shopt -u dotglob nullglob
    for item in "${moved_items[@]}"; do
      if [[ -e "${stage_dir}/${item}" || -L "${stage_dir}/${item}" ]]; then
        mv_with_privs "${stage_dir}/${item}" "${target_dir}/${item}"
      fi
    done
    rm_rf_with_privs "${stage_dir}"
    restore_signal_traps
    return 1
  fi

  rm_rf_with_privs "${stage_dir}"
  restore_signal_traps
}

compose_cmd() {
  (
    cd "${REPO_DIR}"
    docker compose "$@"
  )
}

if [[ "${SKIP_DOCKER}" != true ]]; then
  if ! command -v docker >/dev/null 2>&1; then
    echo "Error: docker is required (or pass --skip-docker)" >&2
    exit 1
  fi
  echo "==> Stopping running containers..."
  compose_cmd --profile '*' down --remove-orphans || true
fi

echo "==> Checking out git commit ${GIT_COMMIT}..."
git -C "${REPO_DIR}" checkout HEAD -- .env 2>/dev/null || true
if [[ -n "${GIT_BRANCH}" ]]; then
  git -C "${REPO_DIR}" checkout -B "${GIT_BRANCH}" "${GIT_COMMIT}"
else
  git -C "${REPO_DIR}" checkout --detach "${GIT_COMMIT}"
fi

echo "==> Restoring .env (mode 0600)..."
cp -p "${BACKUP_DIR}/.env" "${REPO_DIR}/.env"
chmod 600 "${REPO_DIR}/.env"
RESTORED_ENV_SHA="$(sha256sum "${REPO_DIR}/.env" | awk '{print $1}')"
if [[ "${RESTORED_ENV_SHA}" != "${ENV_SHA256}" ]]; then
  echo "Error: restored .env SHA-256 verification failed!" >&2
  exit 1
fi

if [[ -n "${APPS_ARCHIVE}" ]]; then
  echo "==> Restoring ${APPS_MOUNT} from ${APPS_ARCHIVE}..."
  safe_restore_dir "${APPS_MOUNT}" "${BACKUP_DIR}/${APPS_ARCHIVE}" "true"
fi

if [[ -n "${MAINTAINERR_ARCHIVE}" ]]; then
  echo "==> Restoring ${MAINTAINERR_DATA_DIR} from ${MAINTAINERR_ARCHIVE}..."
  safe_restore_dir "${MAINTAINERR_DATA_DIR}" "${BACKUP_DIR}/${MAINTAINERR_ARCHIVE}" "false"
fi

if [[ "${SKIP_DOCKER}" != true && "${NO_START}" != true ]]; then
  if [[ -f "${REPO_DIR}/scripts/fix-permissions.sh" ]]; then
    echo "==> Checking application directory permissions..."
    bash "${REPO_DIR}/scripts/fix-permissions.sh"
  fi
  echo "==> Starting restored HomeLab stack (without pulling new images)..."
  if [[ "${HAD_BETA_RUNNING}" == "true" ]]; then
    if compose_cmd --profile beta config --services >/dev/null 2>&1; then
      compose_cmd --profile beta up -d --build --remove-orphans
    else
      compose_cmd up -d --build --remove-orphans
    fi
  else
    compose_cmd up -d --build --remove-orphans
  fi
fi

echo "==> Rollback completed successfully to ${GIT_COMMIT}."
