#!/usr/bin/env bash
set -euo pipefail

# Safely upgrade a HomeLab installation:
#   1. Pre-flight tool & git cleanliness checks (allows modified .env only).
#   2. Fetch remote refs/tags and resolve target ref (default: latest vX.Y.Z tag).
#   3. Create verified crash-consistent backup via backup.sh.
#   4. Check out target ref, merge .env non-destructively via merge-env.py,
#      validate Compose config, repair permissions, start upgraded containers,
#      and wait for healthchecks.
#   5. Automatically roll back on any upgrade/health failure via rollback.sh.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'EOF'
Usage: upgrade.sh [options]

Safely back up, upgrade, validate, and verify HomeLab with automatic rollback
on failure.

Options:
  --target <ref>       Git ref/tag/commit to upgrade to (default: latest v*.*.* tag)
  --repo-dir <dir>     Path to HomeLab git repository (default: auto-detected)
  --backup-dir <dir>   Custom backup directory to store pre-upgrade snapshot
  --beta               Enable beta profile (e.g. Hermes Agent) during checks and startup
  --timeout <sec>      Healthcheck wait timeout in seconds (default: 240)
  --no-auto-rollback   Do not automatically roll back if upgrade fails
  --no-pull            Do not pass --pull always to docker compose up
  --skip-docker        Skip Docker Compose validation, permissions, startup, and health checks
  --skip-fetch         Skip git fetch origin --tags --prune
  --force              Proceed even if HEAD is already at the resolved target commit
  --dry-run            Preview target, disk/backup check, and .env merge without modifying files
  -h, --help           Show this help message
EOF
}

TARGET_ARG=""
CUSTOM_REPO_DIR=""
CUSTOM_BACKUP_DIR=""
BETA=false
TIMEOUT=240
AUTO_ROLLBACK=true
NO_PULL=false
SKIP_DOCKER=false
SKIP_FETCH=false
FORCE=false
DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)
      [[ $# -ge 2 ]] || { echo "Error: --target requires an argument" >&2; exit 1; }
      TARGET_ARG="$2"
      shift 2
      ;;
    --repo-dir)
      [[ $# -ge 2 ]] || { echo "Error: --repo-dir requires an argument" >&2; exit 1; }
      CUSTOM_REPO_DIR="$2"
      shift 2
      ;;
    --backup-dir)
      [[ $# -ge 2 ]] || { echo "Error: --backup-dir requires an argument" >&2; exit 1; }
      CUSTOM_BACKUP_DIR="$2"
      shift 2
      ;;
    --beta)
      BETA=true
      shift
      ;;
    --timeout)
      [[ $# -ge 2 ]] || { echo "Error: --timeout requires an argument" >&2; exit 1; }
      TIMEOUT="$2"
      shift 2
      ;;
    --no-auto-rollback)
      AUTO_ROLLBACK=false
      shift
      ;;
    --no-pull)
      NO_PULL=true
      shift
      ;;
    --skip-docker)
      SKIP_DOCKER=true
      shift
      ;;
    --skip-fetch)
      SKIP_FETCH=true
      shift
      ;;
    --force)
      FORCE=true
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

if [[ ! "${TIMEOUT}" =~ ^[0-9]+$ ]] || [[ "${TIMEOUT}" -le 0 ]]; then
  echo "Error: --timeout must be a positive integer (got '${TIMEOUT}')" >&2
  exit 1
fi

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

if [[ ! -f "${REPO_DIR}/.env" ]]; then
  echo "Error: .env not found in ${REPO_DIR}" >&2
  exit 1
fi

for helper in backup.sh rollback.sh merge-env.py; do
  if [[ ! -f "${SCRIPT_DIR}/${helper}" ]]; then
    echo "Error: required helper script missing alongside upgrade.sh: ${SCRIPT_DIR}/${helper}" >&2
    exit 1
  fi
done

for cmd in git python3 tar sha256sum; do
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    echo "Error: required tool '${cmd}' is not installed or not in PATH" >&2
    exit 1
  fi
done
if [[ "${SKIP_DOCKER}" != true ]]; then
  if ! command -v docker >/dev/null 2>&1; then
    echo "Error: required tool 'docker' is not installed (or pass --skip-docker)" >&2
    exit 1
  fi
fi

# Ensure no tracked files other than .env have uncommitted changes.
check_git_clean_except_env() {
  local dirty_files=()
  local path=""
  while IFS= read -r path; do
    [[ -n "${path}" && "${path}" != ".env" ]] && dirty_files+=("${path}")
  done < <(git -C "${REPO_DIR}" diff --name-only)

  while IFS= read -r path; do
    [[ -n "${path}" && "${path}" != ".env" ]] && dirty_files+=("${path}")
  done < <(git -C "${REPO_DIR}" diff --cached --name-only)

  if [[ "${#dirty_files[@]}" -gt 0 ]]; then
    echo "Error: repository has uncommitted changes in tracked file(s) other than .env:" >&2
    printf '  %s\n' "${dirty_files[@]}" >&2
    echo "Commit, stash, or revert those changes before upgrading." >&2
    exit 1
  fi
}

check_git_clean_except_env

if [[ "${SKIP_FETCH}" != true ]]; then
  echo "==> Fetching remote tags and branches..."
  git -C "${REPO_DIR}" fetch origin --tags --prune
fi

resolve_target_ref() {
  if [[ -n "${TARGET_ARG}" ]]; then
    printf '%s\n' "${TARGET_ARG}"
    return 0
  fi
  local latest_tag=""
  latest_tag="$(git -C "${REPO_DIR}" tag --list 'v*.*.*' --sort=-v:refname | head -n 1 || true)"
  if [[ -z "${latest_tag}" ]]; then
    echo "Error: no semver tag matching 'v*.*.*' found; pass --target <ref>" >&2
    exit 1
  fi
  printf '%s\n' "${latest_tag}"
}

TARGET_REF="$(resolve_target_ref)"

resolve_target_commit() {
  local ref="$1"
  local resolved=""
  if resolved="$(git -C "${REPO_DIR}" rev-parse --verify --quiet "${ref}^{commit}" 2>/dev/null)"; then
    printf '%s\n' "${resolved}"
    return 0
  fi
  if resolved="$(git -C "${REPO_DIR}" rev-parse --verify --quiet "origin/${ref}^{commit}" 2>/dev/null)"; then
    printf '%s\n' "${resolved}"
    return 0
  fi
  return 1
}

if ! TARGET_COMMIT="$(resolve_target_commit "${TARGET_REF}")"; then
  echo "Error: target ref '${TARGET_REF}' does not resolve to a valid commit in ${REPO_DIR}" >&2
  exit 1
fi

CURRENT_COMMIT="$(git -C "${REPO_DIR}" rev-parse HEAD)"
CURRENT_DESC="$(git -C "${REPO_DIR}" describe --tags --always --dirty 2>/dev/null || git -C "${REPO_DIR}" rev-parse --short=8 HEAD)"

if [[ "${CURRENT_COMMIT}" == "${TARGET_COMMIT}" && "${FORCE}" != true ]]; then
  echo "Already at target ref '${TARGET_REF}' (${TARGET_COMMIT:0:8}). Nothing to do (use --force to re-apply)."
  exit 0
fi

if [[ "${DRY_RUN}" == true ]]; then
  echo "[dry-run] Repository:     ${REPO_DIR}"
  echo "[dry-run] Current commit: ${CURRENT_COMMIT:0:8} (${CURRENT_DESC})"
  echo "[dry-run] Target ref:     ${TARGET_REF} (${TARGET_COMMIT:0:8})"

  backup_dry_args=(--repo-dir "${REPO_DIR}" --dry-run)
  if [[ -n "${CUSTOM_BACKUP_DIR}" ]]; then
    backup_dry_args+=(--backup-dir "${CUSTOM_BACKUP_DIR}")
  fi
  if [[ "${SKIP_DOCKER}" == true ]]; then
    backup_dry_args+=(--skip-docker)
  fi
  bash "${SCRIPT_DIR}/backup.sh" "${backup_dry_args[@]}"

  tmp_template="$(mktemp /tmp/homelab-template-env.XXXXXX)"
  trap 'rm -f "${tmp_template}"' EXIT
  git -C "${REPO_DIR}" show "${TARGET_COMMIT}:.env" > "${tmp_template}"

  merge_dry_args=(
    --template "${tmp_template}"
    --current "${REPO_DIR}/.env"
    --output "${REPO_DIR}/.env"
    --dry-run
    --check-placeholders
  )
  if [[ "${BETA}" == true ]]; then
    merge_dry_args+=(--beta)
  fi
  python3 "${SCRIPT_DIR}/merge-env.py" "${merge_dry_args[@]}"
  echo "[dry-run] Dry-run complete; no files or containers were modified."
  exit 0
fi

echo "==> Creating pre-upgrade backup..."
backup_args=(--repo-dir "${REPO_DIR}" --no-restart)
if [[ -n "${CUSTOM_BACKUP_DIR}" ]]; then
  backup_args+=(--backup-dir "${CUSTOM_BACKUP_DIR}")
fi
if [[ "${SKIP_DOCKER}" == true ]]; then
  backup_args+=(--skip-docker)
fi

BACKUP_OUTPUT_FILE="$(mktemp /tmp/homelab-backup-out.XXXXXX)"
if ! bash "${SCRIPT_DIR}/backup.sh" "${backup_args[@]}" | tee "${BACKUP_OUTPUT_FILE}"; then
  rm -f "${BACKUP_OUTPUT_FILE}"
  echo "Error: backup failed; aborting upgrade without modifying repository or data." >&2
  exit 1
fi
BACKUP_PATH="$(grep -E '^BACKUP_PATH=' "${BACKUP_OUTPUT_FILE}" | tail -n 1 | cut -d= -f2-)"
rm -f "${BACKUP_OUTPUT_FILE}"

if [[ -z "${BACKUP_PATH}" || ! -d "${BACKUP_PATH}" ]]; then
  echo "Error: backup succeeded but failed to determine BACKUP_PATH" >&2
  exit 1
fi
echo "==> Verified backup stored at: ${BACKUP_PATH}"

UPGRADE_IN_PROGRESS=true
ROLLBACK_DONE=false

run_auto_rollback() {
  local reason="$1"
  if [[ "${ROLLBACK_DONE}" == true ]]; then
    return 0
  fi
  ROLLBACK_DONE=true
  echo "" >&2
  echo "ERROR: ${reason}" >&2
  if [[ "${AUTO_ROLLBACK}" != true ]]; then
    echo "Automatic rollback disabled (--no-auto-rollback). To restore manually run:" >&2
    echo "  bash ${SCRIPT_DIR}/rollback.sh --repo-dir ${REPO_DIR} --backup ${BACKUP_PATH} --yes" >&2
    return 0
  fi
  echo "==> Initiating automatic rollback from ${BACKUP_PATH}..." >&2
  local rb_args=(--repo-dir "${REPO_DIR}" --backup "${BACKUP_PATH}" --yes)
  if [[ "${SKIP_DOCKER}" == true ]]; then
    rb_args+=(--skip-docker)
  fi
  if bash "${SCRIPT_DIR}/rollback.sh" "${rb_args[@]}"; then
    echo "==> Automatic rollback succeeded; restored pre-upgrade state." >&2
  else
    echo "CRITICAL: automatic rollback failed! Inspect backup at ${BACKUP_PATH}." >&2
  fi
}

on_exit() {
  local rc=$?
  trap - EXIT INT TERM
  if [[ "${UPGRADE_IN_PROGRESS}" == true && "${rc}" -ne 0 ]]; then
    set +e
    run_auto_rollback "Upgrade aborted with exit code ${rc}"
  fi
  exit "${rc}"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

compose_cmd() {
  (
    cd "${REPO_DIR}"
    docker compose "$@"
  )
}

get_service_state() {
  local service="$1"
  local cid status health
  cid="$(compose_cmd ps -a -q "${service}" 2>/dev/null | head -n 1 || true)"
  if [[ -z "${cid}" ]]; then
    printf 'missing|none|\n'
    return 0
  fi
  status="$(docker inspect -f '{{.State.Status}}' "${cid}" 2>/dev/null || echo 'unknown')"
  health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${cid}" 2>/dev/null || echo 'unknown')"
  printf '%s|%s|%s\n' "${status}" "${health}" "${cid}"
}

wait_for_services_healthy() {
  local -a services=("$@")
  local deadline=$((SECONDS + TIMEOUT))
  local service status health cid

  if [[ "${#services[@]}" -eq 0 ]]; then
    echo "Error: no services found to verify health" >&2
    return 1
  fi

  echo "==> Waiting up to ${TIMEOUT}s for ${#services[@]} service(s): ${services[*]}"
  while true; do
    local pending=0
    local -a waiting_list=()

    for service in "${services[@]}"; do
      IFS='|' read -r status health cid <<< "$(get_service_state "${service}")"

      if [[ "${status}" == "exited" || "${status}" == "dead" || "${health}" == "unhealthy" ]]; then
        echo "" >&2
        echo "Error: service '${service}' entered failure state (status=${status}, health=${health})" >&2
        if [[ -n "${cid}" ]]; then
          echo "--- Last 60 log lines for ${service} (${cid:0:12}) ---" >&2
          docker logs --tail 60 "${cid}" >&2 || true
        fi
        return 1
      fi

      if [[ "${status}" == "running" && ( "${health}" == "healthy" || "${health}" == "none" ) ]]; then
        continue
      fi

      pending=$((pending + 1))
      waiting_list+=("${service}(${status}/${health})")
    done

    if [[ "${pending}" -eq 0 ]]; then
      echo "==> All ${#services[@]} service(s) are running and healthy."
      return 0
    fi

    if (( SECONDS >= deadline )); then
      echo "" >&2
      echo "Error: timed out after ${TIMEOUT}s waiting for services: ${waiting_list[*]}" >&2
      for service in "${services[@]}"; do
        IFS='|' read -r status health cid <<< "$(get_service_state "${service}")"
        if [[ "${status}" != "running" || ( "${health}" != "healthy" && "${health}" != "none" ) ]]; then
          if [[ -n "${cid}" ]]; then
            echo "--- Last 60 log lines for ${service} (${cid:0:12}, status=${status}, health=${health}) ---" >&2
            docker logs --tail 60 "${cid}" >&2 || true
          fi
        fi
      done
      return 1
    fi

    echo "    Waiting on ${pending}/${#services[@]} service(s): ${waiting_list[*]}"
    sleep 5
  done
}

# Step 1: Discard local .env edits now that verified backup exists
echo "==> Checking out target ref ${TARGET_REF} (${TARGET_COMMIT:0:8})..."
git -C "${REPO_DIR}" checkout -- .env

# Step 2: Check out target ref cleanly (branch tracking or detached tag/commit)
if git -C "${REPO_DIR}" show-ref --verify --quiet "refs/heads/${TARGET_REF}"; then
  git -C "${REPO_DIR}" checkout "${TARGET_REF}"
  if git -C "${REPO_DIR}" show-ref --verify --quiet "refs/remotes/origin/${TARGET_REF}"; then
    git -C "${REPO_DIR}" merge --ff-only "origin/${TARGET_REF}"
  fi
elif git -C "${REPO_DIR}" show-ref --verify --quiet "refs/remotes/origin/${TARGET_REF}"; then
  git -C "${REPO_DIR}" checkout -B "${TARGET_REF}" "origin/${TARGET_REF}"
else
  git -C "${REPO_DIR}" checkout "${TARGET_COMMIT}"
fi

# Step 3: Merge backed-up .env values into new template .env
echo "==> Merging existing .env values into updated template..."
merge_args=(
  --template "${REPO_DIR}/.env"
  --current "${BACKUP_PATH}/.env"
  --output "${REPO_DIR}/.env"
  --check-placeholders
)
if [[ "${BETA}" == true ]]; then
  merge_args+=(--beta)
fi
python3 "${SCRIPT_DIR}/merge-env.py" "${merge_args[@]}"

if [[ "${SKIP_DOCKER}" != true ]]; then
  compose_profile_args=()
  if [[ "${BETA}" == true ]]; then
    compose_profile_args=(--profile beta)
  fi

  # Step 4: Validate Compose config
  echo "==> Validating Docker Compose configuration..."
  compose_cmd "${compose_profile_args[@]}" config --quiet

  # Step 5: Repair volume permissions if fix-permissions.sh is present
  if [[ -f "${REPO_DIR}/scripts/fix-permissions.sh" ]]; then
    echo "==> Checking application directory permissions..."
    bash "${REPO_DIR}/scripts/fix-permissions.sh"
  fi

  # Step 6: Start upgraded stack
  echo "==> Starting upgraded HomeLab stack..."
  up_args=(up -d --build --remove-orphans)
  if [[ "${NO_PULL}" != true ]]; then
    up_args+=(--pull always)
  fi
  compose_cmd "${compose_profile_args[@]}" "${up_args[@]}"

  # Step 7: Verify all started services become healthy
  mapfile -t TARGET_SERVICES < <(compose_cmd "${compose_profile_args[@]}" config --services)
  wait_for_services_healthy "${TARGET_SERVICES[@]}"
fi

UPGRADE_IN_PROGRESS=false
echo ""
echo "==> Upgrade complete!"
echo "    From:   ${CURRENT_COMMIT:0:8} (${CURRENT_DESC})"
echo "    To:     ${TARGET_COMMIT:0:8} (${TARGET_REF})"
echo "    Backup: ${BACKUP_PATH}"
echo "    To roll back manually later, run:"
echo "      bash ${REPO_DIR}/scripts/rollback.sh --backup ${BACKUP_PATH}"
