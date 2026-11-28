#!/usr/bin/env bash
set -euo pipefail

# End-to-end System QA test for the HomeLab Compose stack:
#   1. Backs up .env and generates a temporary CI .env in-place (with isolated storage/apps dirs).
#   2. Pre-seeds Hermes config.yaml from configs/hermes/config.yaml.example (when beta mode is active).
#   3. Exports COMPOSE_FILE=docker-compose.yml:tests/docker-compose.ci.yml for runner compatibility.
#   4. Runs `make up-beta` (16 services, default) or `make up` (15 services with `--default-only`).
#   5. Polls Docker healthchecks until every expected service is running + healthy.
#   6. Runs scripts/qa-test.py across 127.0.0.1 and the host's non-loopback IP.
#   7. Runs `make down-beta` (or `make down`), verifies zero leftover containers, and restores .env cleanly.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

BETA=true
while [[ $# -gt 0 ]]; do
  case "$1" in
    --default-only)
      BETA=false
      shift
      ;;
    --beta)
      BETA=true
      shift
      ;;
    -h|--help)
      echo "Usage: $(basename "$0") [--default-only]"
      echo "  Default: full 16-service QA (15 default services + beta hermes-agent via make up-beta)."
      echo "  --default-only: run 15 default services only (via make up)."
      exit 0
      ;;
    *)
      echo "Error: unknown option: $1" >&2
      exit 1
      ;;
  esac
done

SYSTEM_TEST_TIMEOUT="${SYSTEM_TEST_TIMEOUT:-300}"
POLL_INTERVAL=5

if [[ ! "${SYSTEM_TEST_TIMEOUT}" =~ ^[0-9]+$ ]] || [[ "${SYSTEM_TEST_TIMEOUT}" -le 0 ]]; then
  echo "Error: SYSTEM_TEST_TIMEOUT must be a positive integer (got '${SYSTEM_TEST_TIMEOUT}')." >&2
  exit 1
fi

HOST_IP=""
if command -v hostname >/dev/null 2>&1; then
  for candidate in $(hostname -I 2>/dev/null || true); do
    if [[ "${candidate}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] && [[ "${candidate}" != 127.* ]]; then
      HOST_IP="${candidate}"
      break
    fi
  done
fi
if [[ -z "${HOST_IP}" ]]; then
  HOST_IP="127.0.0.1"
fi

TMP_DIR="$(mktemp -d /tmp/homelab-system-test.XXXXXX)"
ENV_PATH="${REPO_ROOT}/.env"
ENV_BACKUP="${TMP_DIR}/.env.backup"
ENV_BACKED_UP=false
STACK_STARTED=0

if [[ ! -f "${ENV_PATH}" ]]; then
  echo "Error: expected ${ENV_PATH} to exist." >&2
  rm -rf "${TMP_DIR}"
  exit 1
fi

cleanup() {
  local exit_code=$?
  local restore_ok=true
  trap - EXIT
  trap '' INT TERM
  set +e

  unset COMPOSE_PROFILES
  export COMPOSE_FILE="docker-compose.yml:tests/docker-compose.ci.yml"
  if [[ "${STACK_STARTED}" -eq 1 ]] && [[ -f "${ENV_PATH}" ]]; then
    echo ""
    if [[ "${BETA}" == true ]]; then
      echo "==> Cleaning up stack via make down-beta..."
      make -C "${REPO_ROOT}" down-beta >/dev/null 2>&1 || true
    else
      echo "==> Cleaning up stack via make down..."
      make -C "${REPO_ROOT}" down >/dev/null 2>&1 || true
    fi
  fi

  if [[ "${ENV_BACKED_UP}" == true ]]; then
    if [[ -f "${ENV_BACKUP}" ]]; then
      if mv -f "${ENV_BACKUP}" "${ENV_PATH}"; then
        echo "==> Restored ${ENV_PATH}"
      else
        restore_ok=false
        echo "ERROR: failed to restore ${ENV_PATH}; preserved backup at ${ENV_BACKUP}" >&2
      fi
    else
      restore_ok=false
      echo "ERROR: expected .env backup missing at ${ENV_BACKUP}; preserving ${TMP_DIR}" >&2
    fi
  fi

  if [[ "${restore_ok}" == true ]] && [[ -d "${TMP_DIR}" ]]; then
    if ! rm -rf "${TMP_DIR}" 2>/dev/null; then
      if command -v sudo >/dev/null 2>&1; then
        sudo rm -rf "${TMP_DIR}" || true
      fi
    fi
  fi

  exit "${exit_code}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

cp -a "${ENV_PATH}" "${ENV_BACKUP}"
ENV_BACKED_UP=true

mkdir -p "${TMP_DIR}/storage" "${TMP_DIR}/apps"

# Dashboard Dockerfile lives under ${DEFAULT_APPS_MOUNT}/home-server-dashboard in docker-compose.yml,
# so copy apps/home-server-dashboard into the isolated temporary apps mount.
if [[ -d "${REPO_ROOT}/apps/home-server-dashboard" ]]; then
  cp -a "${REPO_ROOT}/apps/home-server-dashboard" "${TMP_DIR}/apps/home-server-dashboard"
fi

# Pre-seed Hermes config.yaml from configs/hermes/config.yaml.example (matches operator flow)
if [[ "${BETA}" == true ]]; then
  HERMES_CFG_EXAMPLE="${REPO_ROOT}/configs/hermes/config.yaml.example"
  if [[ ! -f "${HERMES_CFG_EXAMPLE}" ]]; then
    echo "Error: expected ${HERMES_CFG_EXAMPLE} to exist." >&2
    exit 1
  fi
  mkdir -p "${TMP_DIR}/apps/hermes"
  cp -a "${HERMES_CFG_EXAMPLE}" "${TMP_DIR}/apps/hermes/config.yaml"
fi

# Generate temporary CI .env from backup, substituting only CI-safe overrides
# while preserving all existing keys, ordering, and comments.
python3 - "${ENV_BACKUP}" "${ENV_PATH}" "${TMP_DIR}/storage" "${TMP_DIR}/apps" "$(id -u)" "$(id -g)" "${HOST_IP}" "${BETA}" <<'PY'
import sys
from pathlib import Path

src_path, dst_path, storage_dir, apps_dir, uid, gid, host_ip, beta_flag = sys.argv[1:9]

overrides = {
    "DEFAULT_STORAGE_MOUNT": storage_dir,
    "DEFAULT_APPS_MOUNT": apps_dir,
    "DEFAULT_PUID": uid,
    "DEFAULT_GUID": gid,
    "DEFAULT_TIMEZONE": "UTC",
    "DEFAULT_LAN_IP": host_ip,
    "DASHBOARD_PORT": "18080",
    "SAMBA_PORT": "1445",
    "SAMBA_USERNAME": "ciuser",
    "SAMBA_PASSWORD": "cipassword123",
    "COUCHDB_USER": "ciadmin",
    "COUCHDB_PASSWORD": "cipassword123",
    "PI_HOLE_WEB_PASSWORD": "cipassword123",
}

if beta_flag == "true":
    overrides.update(
        {
            "HERMES_DASHBOARD": "1",
            "HERMES_DASHBOARD_BIND_IP": "0.0.0.0",
            "HERMES_DASHBOARD_BASIC_AUTH_USERNAME": "ciadmin",
            "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD": "cipassword123",
            "HERMES_DASHBOARD_BASIC_AUTH_SECRET": "ci-session-secret-key-for-qa-testing-32bytes",
            "HERMES_OPENROUTER_API_KEY": "",
        }
    )

lines = Path(src_path).read_text(encoding="utf-8").splitlines()
seen: set[str] = set()
out_lines: list[str] = []

for line in lines:
    stripped = line.strip()
    if stripped and not stripped.startswith("#") and "=" in line:
        key = line.split("=", 1)[0].strip()
        if key in overrides:
            out_lines.append(f"{key}={overrides[key]}")
            seen.add(key)
            continue
    out_lines.append(line)

for key, val in overrides.items():
    if key not in seen:
        out_lines.append(f"{key}={val}")

Path(dst_path).write_text("\n".join(out_lines) + "\n", encoding="utf-8")
PY

unset COMPOSE_PROFILES
export COMPOSE_FILE="docker-compose.yml:tests/docker-compose.ci.yml"

if [[ "${BETA}" == true ]]; then
  echo "==> Starting full HomeLab stack (default + beta) via make up-beta (HOST_IP=${HOST_IP})..."
  STACK_STARTED=1
  make -C "${REPO_ROOT}" up-beta
  mapfile -t EXPECTED_SERVICES < <(cd "${REPO_ROOT}" && docker compose --profile beta config --services)
  expected_count=16
else
  echo "==> Starting default HomeLab stack via make up (HOST_IP=${HOST_IP})..."
  STACK_STARTED=1
  make -C "${REPO_ROOT}" up
  mapfile -t EXPECTED_SERVICES < <(cd "${REPO_ROOT}" && docker compose config --services)
  expected_count=15
fi

if [[ "${#EXPECTED_SERVICES[@]}" -eq 0 ]]; then
  echo "Error: no services returned by docker compose config --services" >&2
  exit 1
fi
if [[ "${#EXPECTED_SERVICES[@]}" -ne "${expected_count}" ]]; then
  echo "Error: expected ${expected_count} services, got ${#EXPECTED_SERVICES[@]}: ${EXPECTED_SERVICES[*]}" >&2
  exit 1
fi

echo "==> Waiting up to ${SYSTEM_TEST_TIMEOUT}s for ${#EXPECTED_SERVICES[@]} services to become healthy: ${EXPECTED_SERVICES[*]}"

get_service_state() {
  local service="$1"
  local cid status health

  if [[ "${BETA}" == true ]]; then
    cid="$(cd "${REPO_ROOT}" && docker compose --profile beta ps -a -q "${service}" 2>/dev/null | head -n 1 || true)"
  else
    cid="$(cd "${REPO_ROOT}" && docker compose ps -a -q "${service}" 2>/dev/null | head -n 1 || true)"
  fi
  if [[ -z "${cid}" ]]; then
    printf 'missing|none|\n'
    return 0
  fi

  status="$(docker inspect -f '{{.State.Status}}' "${cid}" 2>/dev/null || echo 'unknown')"
  health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${cid}" 2>/dev/null || echo 'unknown')"
  printf '%s|%s|%s\n' "${status}" "${health}" "${cid}"
}

deadline=$((SECONDS + SYSTEM_TEST_TIMEOUT))
all_healthy=false

while true; do
  pending=0
  unhealthy_list=()

  for service in "${EXPECTED_SERVICES[@]}"; do
    IFS='|' read -r status health _cid <<< "$(get_service_state "${service}")"
    if [[ "${status}" == "running" && "${health}" == "healthy" ]]; then
      continue
    fi
    pending=$((pending + 1))
    unhealthy_list+=("${service}(${status}/${health})")
  done

  if [[ "${pending}" -eq 0 ]]; then
    all_healthy=true
    break
  fi

  if [[ "${SECONDS}" -ge "${deadline}" ]]; then
    echo "Timed out after ${SYSTEM_TEST_TIMEOUT}s waiting for services: ${unhealthy_list[*]}" >&2
    break
  fi

  echo "Waiting on ${pending}/${#EXPECTED_SERVICES[@]} services: ${unhealthy_list[*]}"
  sleep "${POLL_INTERVAL}"
done

echo ""
echo "==> Final Service Health Summary"
printf '%-16s %-12s %-12s %-14s\n' "SERVICE" "STATUS" "HEALTH" "CONTAINER_ID"
printf '%-16s %-12s %-12s %-14s\n' "-------" "------" "------" "------------"
for service in "${EXPECTED_SERVICES[@]}"; do
  IFS='|' read -r status health cid <<< "$(get_service_state "${service}")"
  short_cid="${cid:0:12}"
  short_cid="${short_cid:--}"
  printf '%-16s %-12s %-12s %-14s\n' "${service}" "${status}" "${health}" "${short_cid}"
done

if [[ "${all_healthy}" != true ]]; then
  echo "" >&2
  echo "==> Failure Diagnostics" >&2
  if [[ "${BETA}" == true ]]; then
    (cd "${REPO_ROOT}" && docker compose --profile beta ps -a) >&2 || true
  else
    (cd "${REPO_ROOT}" && docker compose ps -a) >&2 || true
  fi
  for service in "${EXPECTED_SERVICES[@]}"; do
    IFS='|' read -r status health cid <<< "$(get_service_state "${service}")"
    if [[ "${status}" != "running" || "${health}" != "healthy" ]]; then
      echo "" >&2
      echo "--- Diagnostics for ${service} (status=${status}, health=${health}) ---" >&2
      if [[ -n "${cid}" ]]; then
        docker inspect -f '{{json .State.Health}}' "${cid}" >&2 || true
        docker logs --tail 80 "${cid}" >&2 || true
      else
        echo "No container found for service ${service}" >&2
      fi
    fi
  done
  exit 1
fi

echo ""
echo "==> Running System QA Suite (scripts/qa-test.py)..."
qa_args=(--env-file "${ENV_PATH}" --host-ip "${HOST_IP}")
if [[ "${BETA}" == true ]]; then
  qa_args+=(--beta)
fi
if ! python3 "${SCRIPT_DIR}/qa-test.py" "${qa_args[@]}"; then
  echo "" >&2
  echo "==> QA Suite Failure Container Diagnostics" >&2
  if [[ "${BETA}" == true ]]; then
    (cd "${REPO_ROOT}" && docker compose --profile beta ps -a) >&2 || true
    docker logs --tail 60 hermes-agent >&2 || true
  else
    (cd "${REPO_ROOT}" && docker compose ps -a) >&2 || true
  fi
  exit 1
fi

echo ""
if [[ "${BETA}" == true ]]; then
  echo "==> Tearing down stack via make down-beta..."
  make -C "${REPO_ROOT}" down-beta
else
  echo "==> Tearing down stack via make down..."
  make -C "${REPO_ROOT}" down
fi
STACK_STARTED=0

remaining="$(cd "${REPO_ROOT}" && docker compose --profile '*' ps -a -q)"
if [[ -n "${remaining}" ]]; then
  echo "ERROR: expected docker compose --profile '*' ps -a -q to be empty after teardown, found: ${remaining}" >&2
  exit 1
fi

echo "==> System QA test passed: all ${#EXPECTED_SERVICES[@]} services reached healthy, passed full-stack QA checks, and tore down cleanly."
