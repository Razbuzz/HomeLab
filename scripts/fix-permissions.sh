#!/usr/bin/env bash
set -euo pipefail

# Ensure bind-mounted application directories exist with the ownership
# required by non-root containers (run on every `make up` and during `make setup`).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

DRY_RUN=false
for arg in "$@"; do
  case "${arg}" in
    --dry-run|-n)
      DRY_RUN=true
      ;;
    --help|-h)
      echo "Usage: $(basename "$0") [--dry-run|-n]"
      echo "Ensure application bind-mount directories exist with expected ownership."
      exit 0
      ;;
    *)
      echo "Unknown option: ${arg}" >&2
      exit 1
      ;;
  esac
done

# Allow caller-supplied env vars (or ENV_FILE override) to take precedence over .env
_caller_DEFAULT_PUID="${DEFAULT_PUID-}"
_caller_DEFAULT_GUID="${DEFAULT_GUID-}"
_caller_DEFAULT_APPS_MOUNT="${DEFAULT_APPS_MOUNT-}"
_caller_COUCHDB_FOLDER="${COUCHDB_FOLDER-}"
_caller_MATTERMOST_FOLDER="${MATTERMOST_FOLDER-}"

ENV_FILE="${ENV_FILE:-${REPO_ROOT}/.env}"
if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck source=/dev/null
  . "${ENV_FILE}"
  set +a
fi

DEFAULT_PUID="${_caller_DEFAULT_PUID:-${DEFAULT_PUID:-1000}}"
DEFAULT_GUID="${_caller_DEFAULT_GUID:-${DEFAULT_GUID:-1000}}"
DEFAULT_APPS_MOUNT="${_caller_DEFAULT_APPS_MOUNT:-${DEFAULT_APPS_MOUNT:-./apps}}"
COUCHDB_FOLDER="${_caller_COUCHDB_FOLDER:-${COUCHDB_FOLDER:-couch-db}}"
MATTERMOST_FOLDER="${_caller_MATTERMOST_FOLDER:-${MATTERMOST_FOLDER:-mattermost}}"

# Resolve relative DEFAULT_APPS_MOUNT relative to the repository root.
if [[ "${DEFAULT_APPS_MOUNT}" = /* ]]; then
  APPS_MOUNT="${DEFAULT_APPS_MOUNT}"
else
  APPS_MOUNT="${REPO_ROOT}/${DEFAULT_APPS_MOUNT#./}"
fi
APPS_MOUNT="${APPS_MOUNT%/}"

# Service volume ownership map: "<relative-dir-under-apps-mount>:<uid>:<gid>"
# Add new non-root service directories here as needed.
# Note: Mattermost's `db` directory is intentionally omitted because the
# postgres image manages `/var/lib/postgresql/data` ownership itself.
OWNERSHIP_MAP=(
  "${COUCHDB_FOLDER}/couchdb-data:${DEFAULT_PUID}:${DEFAULT_GUID}"
  "${COUCHDB_FOLDER}/couchdb-etc:${DEFAULT_PUID}:${DEFAULT_GUID}"
  "${MATTERMOST_FOLDER}/data:2000:2000"
  "${MATTERMOST_FOLDER}/logs:2000:2000"
  "${MATTERMOST_FOLDER}/config:2000:2000"
)

run_chown() {
  local owner="$1"
  local target="$2"
  if [[ "$(id -u)" -eq 0 ]]; then
    chown -R "${owner}" "${target}"
  else
    sudo chown -R "${owner}" "${target}"
  fi
}

dir_needs_chown() {
  local target="$1"
  local expected_uid="$2"
  local expected_gid="$3"
  local mismatch=""

  # Check the directory itself and any nested entries with mismatched UID/GID.
  # If find encounters an unreadable subdirectory (e.g. mode 0700 owned by another user),
  # fall back to running chown so root can inspect/repair it.
  if ! mismatch="$(find "${target}" \( ! -uid "${expected_uid}" -o ! -gid "${expected_gid}" \) -print -quit 2>/dev/null)"; then
    return 0
  fi
  [[ -n "${mismatch}" ]]
}

changed=0
for entry in "${OWNERSHIP_MAP[@]}"; do
  IFS=':' read -r rel_dir uid gid <<< "${entry}"
  target_dir="${APPS_MOUNT}/${rel_dir}"

  if [[ ! -d "${target_dir}" ]]; then
    if [[ "${DRY_RUN}" == true ]]; then
      echo "[dry-run] Would create directory ${target_dir} and set ownership to ${uid}:${gid}"
      changed=$((changed + 1))
      continue
    fi
    if mkdir -p "${target_dir}" 2>/dev/null; then
      echo "Created ${target_dir}"
    else
      if [[ "$(id -u)" -eq 0 ]]; then
        mkdir -p "${target_dir}"
      else
        sudo mkdir -p "${target_dir}"
      fi
      echo "Created ${target_dir}"
    fi
  fi

  if dir_needs_chown "${target_dir}" "${uid}" "${gid}"; then
    current_owner="$(stat -c '%u:%g' "${target_dir}" 2>/dev/null || echo 'unknown')"
    if [[ "${DRY_RUN}" == true ]]; then
      echo "[dry-run] Would chown -R ${uid}:${gid} ${target_dir} (top-level currently ${current_owner})"
    else
      run_chown "${uid}:${gid}" "${target_dir}"
      echo "Updated ownership of ${target_dir} -> ${uid}:${gid} (was ${current_owner})"
    fi
    changed=$((changed + 1))
  fi
done

if [[ "${changed}" -eq 0 ]]; then
  echo "All managed app directory permissions already match expected ownership."
fi
