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

# Safely parse simple KEY=VALUE lines from an env file without executing shell
# code or expanding `$` / `&` / `;` / spaces in Compose secrets.
# Only loads valid identifiers, and preserves variables already set in the
# caller's environment.
load_env_file() {
  local env_file="$1"
  [[ -f "${env_file}" ]] || return 0

  local -A _caller_set=()
  local _var
  for _var in DEFAULT_PUID DEFAULT_GUID DEFAULT_APPS_MOUNT COUCHDB_FOLDER SEERR_FOLDER; do
    if [[ -n "${!_var+x}" ]]; then
      _caller_set["${_var}"]=1
    fi
  done

  local line key val first_char last_char
  while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line%$'\r'}"
    # Skip blank lines and comments
    [[ "${line}" =~ ^[[:space:]]*(#|$) ]] && continue
    # Strip optional leading "export "
    if [[ "${line}" =~ ^[[:space:]]*export[[:space:]]+(.*)$ ]]; then
      line="${BASH_REMATCH[1]}"
    fi
    # Must contain '=' with a valid identifier key
    [[ "${line}" == *=* ]] || continue
    key="${line%%=*}"
    key="${key#"${key%%[![:space:]]*}"}"
    key="${key%"${key##*[![:space:]]}"}"
    [[ "${key}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue

    # Only import keys needed by this script; preserve caller overrides
    case "${key}" in
      DEFAULT_PUID|DEFAULT_GUID|DEFAULT_APPS_MOUNT|COUCHDB_FOLDER|SEERR_FOLDER)
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

ENV_FILE="${ENV_FILE:-${REPO_ROOT}/.env}"
load_env_file "${ENV_FILE}"

DEFAULT_PUID="${DEFAULT_PUID:-1000}"
DEFAULT_GUID="${DEFAULT_GUID:-1000}"
DEFAULT_APPS_MOUNT="${DEFAULT_APPS_MOUNT:-./apps}"
COUCHDB_FOLDER="${COUCHDB_FOLDER:-couch-db}"
SEERR_FOLDER="${SEERR_FOLDER:-seerr}"

if [[ ! "${DEFAULT_PUID}" =~ ^[0-9]+$ ]]; then
  echo "Error: DEFAULT_PUID must be a non-negative integer (got '${DEFAULT_PUID}'). Check ${ENV_FILE}." >&2
  exit 1
fi
if [[ ! "${DEFAULT_GUID}" =~ ^[0-9]+$ ]]; then
  echo "Error: DEFAULT_GUID must be a non-negative integer (got '${DEFAULT_GUID}'). Check ${ENV_FILE}." >&2
  exit 1
fi

# Resolve DEFAULT_APPS_MOUNT cleanly, handling ".", "/", and relative paths
if [[ "${DEFAULT_APPS_MOUNT}" == "/" ]]; then
  APPS_MOUNT="/"
elif [[ "${DEFAULT_APPS_MOUNT}" == /* ]]; then
  APPS_MOUNT="${DEFAULT_APPS_MOUNT%/}"
  [[ -z "${APPS_MOUNT}" ]] && APPS_MOUNT="/"
elif [[ "${DEFAULT_APPS_MOUNT}" == "." || "${DEFAULT_APPS_MOUNT}" == "./" ]]; then
  APPS_MOUNT="${REPO_ROOT}"
else
  rel_mount="${DEFAULT_APPS_MOUNT#./}"
  rel_mount="${rel_mount%/}"
  if [[ -z "${rel_mount}" || "${rel_mount}" == "." ]]; then
    APPS_MOUNT="${REPO_ROOT}"
  else
    APPS_MOUNT="${REPO_ROOT}/${rel_mount}"
  fi
fi

# Service volume ownership map: "<relative-dir-under-apps-mount>|<uid>|<gid>"
# Add new non-root service directories here as needed.
# Note:
#   - Seerr runs rootless as `node` (1000:1000) and ignores PUID/PGID.
OWNERSHIP_MAP=(
  "${COUCHDB_FOLDER}/couchdb-data|${DEFAULT_PUID}|${DEFAULT_GUID}"
  "${COUCHDB_FOLDER}/couchdb-etc|${DEFAULT_PUID}|${DEFAULT_GUID}"
  "${SEERR_FOLDER}/config|1000|1000"
)

require_sudo() {
  local action_desc="$1"
  if ! command -v sudo >/dev/null 2>&1; then
    echo "Error: root privileges or 'sudo' are required to ${action_desc}" >&2
    exit 1
  fi
}

run_chown() {
  local owner="$1"
  local target="$2"
  if [[ "$(id -u)" -eq 0 ]]; then
    chown -hR "${owner}" "${target}"
  else
    require_sudo "update ownership of ${target}"
    sudo chown -hR "${owner}" "${target}"
  fi
}

dir_needs_chown() {
  local target="$1"
  local expected_uid="$2"
  local expected_gid="$3"
  local mismatch=""

  # Check the directory itself and any nested entries on the same filesystem without
  # dereferencing symlinks. If find encounters an unreadable subdirectory (e.g. mode
  # 0700 owned by another user), fall back to running chown so root can inspect/repair it.
  if ! mismatch="$(find -P "${target}" -xdev \( ! -uid "${expected_uid}" -o ! -gid "${expected_gid}" \) -print -quit 2>/dev/null)"; then
    return 0
  fi
  [[ -n "${mismatch}" ]]
}

join_target_path() {
  local base="$1"
  local rel="$2"
  rel="${rel#/}"
  rel="${rel%/}"
  if [[ "${base}" == "/" ]]; then
    printf '/%s\n' "${rel}"
  else
    printf '%s/%s\n' "${base}" "${rel}"
  fi
}

# Validate that neither APPS_MOUNT nor any intermediate path component under it
# (up to target_dir itself) is a symlink before touching directories.
ensure_no_symlink_in_path() {
  local base="$1"
  local rel="$2"
  local current="${base}"

  if [[ -L "${current}" ]]; then
    echo "Error: refusing to operate on symlink ${current}" >&2
    exit 1
  fi

  local part
  local -a parts=()
  IFS='/' read -r -a parts <<< "${rel}"
  for part in "${parts[@]}"; do
    [[ -z "${part}" || "${part}" == "." ]] && continue
    if [[ "${current}" == "/" ]]; then
      current="/${part}"
    else
      current="${current}/${part}"
    fi
    if [[ -L "${current}" ]]; then
      echo "Error: refusing to chown symlink ${current}" >&2
      exit 1
    fi
  done
}

changed=0
for entry in "${OWNERSHIP_MAP[@]}"; do
  IFS='|' read -r rel_dir uid gid <<< "${entry}"
  rel_dir="${rel_dir%/}"
  target_dir="$(join_target_path "${APPS_MOUNT}" "${rel_dir}")"

  ensure_no_symlink_in_path "${APPS_MOUNT}" "${rel_dir}"

  if [[ -e "${target_dir}" && ! -d "${target_dir}" ]]; then
    echo "Error: expected directory at ${target_dir}, found non-directory" >&2
    exit 1
  fi

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
        require_sudo "create directory ${target_dir}"
        sudo mkdir -p "${target_dir}"
      fi
      echo "Created ${target_dir}"
    fi
  fi

  if dir_needs_chown "${target_dir}" "${uid}" "${gid}"; then
    current_owner="$(stat -c '%u:%g' "${target_dir}" 2>/dev/null || echo 'unknown')"
    if [[ "${DRY_RUN}" == true ]]; then
      echo "[dry-run] Would chown -hR ${uid}:${gid} ${target_dir} (top-level currently ${current_owner})"
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
