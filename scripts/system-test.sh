#!/usr/bin/env bash
set -euo pipefail

# End-to-end system test for the default HomeLab Compose stack:
#   1. Backs up .env and generates a temporary CI .env in-place (with isolated storage/apps dirs).
#   2. Exports COMPOSE_FILE=docker-compose.yml:tests/docker-compose.ci.yml for runner compatibility.
#   3. Runs `make up` (permissions + build dashboard + pull/start all 15 default services).
#   4. Polls Docker healthchecks until every expected service is running + healthy.
#   5. Verifies HTTP responses on published host ports.
#   6. Runs `make down` and restores .env cleanly in an EXIT trap.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

SYSTEM_TEST_TIMEOUT="${SYSTEM_TEST_TIMEOUT:-240}"
POLL_INTERVAL=5

if [[ ! "${SYSTEM_TEST_TIMEOUT}" =~ ^[0-9]+$ ]] || [[ "${SYSTEM_TEST_TIMEOUT}" -le 0 ]]; then
  echo "Error: SYSTEM_TEST_TIMEOUT must be a positive integer (got '${SYSTEM_TEST_TIMEOUT}')." >&2
  exit 1
fi

TMP_DIR="$(mktemp -d /tmp/homelab-system-test.XXXXXX)"
ENV_PATH="${REPO_ROOT}/.env"
ENV_BACKUP="${TMP_DIR}/.env.backup"
ENV_BACKED_UP=false

if [[ ! -f "${ENV_PATH}" ]]; then
  echo "Error: expected ${ENV_PATH} to exist." >&2
  rm -rf "${TMP_DIR}"
  exit 1
fi

cleanup() {
  local exit_code=$?
  set +e

  export COMPOSE_FILE="docker-compose.yml:tests/docker-compose.ci.yml"
  if [[ -f "${ENV_PATH}" ]]; then
    echo ""
    echo "==> Cleaning up stack via make down..."
    make -C "${REPO_ROOT}" down >/dev/null 2>&1 || true
  fi

  if [[ "${ENV_BACKED_UP}" == true ]] && [[ -f "${ENV_BACKUP}" ]]; then
    mv -f "${ENV_BACKUP}" "${ENV_PATH}"
    echo "==> Restored ${ENV_PATH}"
  fi

  if [[ -d "${TMP_DIR}" ]]; then
    if ! rm -rf "${TMP_DIR}" 2>/dev/null; then
      if command -v sudo >/dev/null 2>&1; then
        sudo rm -rf "${TMP_DIR}" || true
      fi
    fi
  fi

  exit "${exit_code}"
}
trap cleanup EXIT

cp -a "${ENV_PATH}" "${ENV_BACKUP}"
ENV_BACKED_UP=true

mkdir -p "${TMP_DIR}/storage" "${TMP_DIR}/apps"

# Dashboard Dockerfile lives under ${DEFAULT_APPS_MOUNT}/home-server-dashboard in docker-compose.yml,
# so copy apps/home-server-dashboard into the isolated temporary apps mount.
if [[ -d "${REPO_ROOT}/apps/home-server-dashboard" ]]; then
  cp -a "${REPO_ROOT}/apps/home-server-dashboard" "${TMP_DIR}/apps/home-server-dashboard"
fi

# Generate temporary CI .env from backup, substituting only CI-safe overrides
# while preserving all existing keys, ordering, and comments.
python3 - "${ENV_BACKUP}" "${ENV_PATH}" "${TMP_DIR}/storage" "${TMP_DIR}/apps" "$(id -u)" "$(id -g)" <<'PY'
import sys
from pathlib import Path

src_path, dst_path, storage_dir, apps_dir, uid, gid = sys.argv[1:7]

overrides = {
    "DEFAULT_STORAGE_MOUNT": storage_dir,
    "DEFAULT_APPS_MOUNT": apps_dir,
    "DEFAULT_PUID": uid,
    "DEFAULT_GUID": gid,
    "DEFAULT_TIMEZONE": "UTC",
    "DEFAULT_LAN_IP": "127.0.0.1",
    "DASHBOARD_PORT": "18080",
    "SAMBA_PORT": "1445",
    "SAMBA_USERNAME": "ciuser",
    "SAMBA_PASSWORD": "cipassword123",
    "COUCHDB_USER": "ciadmin",
    "COUCHDB_PASSWORD": "cipassword123",
    "PI_HOLE_WEB_PASSWORD": "cipassword123",
}

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

export COMPOSE_FILE="docker-compose.yml:tests/docker-compose.ci.yml"

echo "==> Starting default HomeLab stack via make up..."
make -C "${REPO_ROOT}" up

mapfile -t EXPECTED_SERVICES < <(cd "${REPO_ROOT}" && docker compose config --services)
if [[ "${#EXPECTED_SERVICES[@]}" -eq 0 ]]; then
  echo "Error: no services returned by docker compose config --services" >&2
  exit 1
fi
echo "==> Waiting up to ${SYSTEM_TEST_TIMEOUT}s for ${#EXPECTED_SERVICES[@]} services to become healthy: ${EXPECTED_SERVICES[*]}"

get_service_state() {
  local service="$1"
  local cid status health

  cid="$(cd "${REPO_ROOT}" && docker compose ps -a -q "${service}" 2>/dev/null | head -n 1 || true)"
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
  [[ -z "${short_cid}" ]] && short_cid="-"
  printf '%-16s %-12s %-12s %-14s\n' "${service}" "${status}" "${health}" "${short_cid}"
done

if [[ "${all_healthy}" != true ]]; then
  echo "" >&2
  echo "==> Failure Diagnostics" >&2
  (cd "${REPO_ROOT}" && docker compose ps -a) >&2 || true
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
echo "==> Verifying host-port HTTP endpoints..."

check_http_endpoint() {
  local service="$1"
  local url="$2"
  local expected_substring="${3:-}"
  local tmp_body="${TMP_DIR}/http-${service}.body"
  local code=""

  for _ in $(seq 1 6); do
    code="$(curl -sS -L --max-time 10 -o "${tmp_body}" -w '%{http_code}' "${url}" || true)"
    if [[ "${code}" =~ ^2[0-9]{2}$ ]]; then
      if [[ -n "${expected_substring}" ]] && ! grep -Fq "${expected_substring}" "${tmp_body}"; then
        echo "ERROR: ${service} (${url}) returned HTTP ${code} but body missed expected text: ${expected_substring}" >&2
        head -c 400 "${tmp_body}" >&2 || true
        echo "" >&2
        return 1
      fi
      echo "OK: ${service} (${url}) -> HTTP ${code}"
      return 0
    fi
    sleep 2
  done

  echo "ERROR: ${service} (${url}) failed host-port check (last HTTP code: ${code:-none})" >&2
  if [[ -s "${tmp_body}" ]]; then
    head -c 400 "${tmp_body}" >&2 || true
    echo "" >&2
  fi
  return 1
}

check_http_endpoint "dashboard" "http://127.0.0.1:18080/" "Home Server Dashboard"
check_http_endpoint "plex" "http://127.0.0.1:32400/identity"
check_http_endpoint "qbittorrent" "http://127.0.0.1:8080/"
check_http_endpoint "filebrowser" "http://127.0.0.1:10000/health"
check_http_endpoint "sonarr" "http://127.0.0.1:8989/ping"
check_http_endpoint "radarr" "http://127.0.0.1:7878/ping"
check_http_endpoint "jackett" "http://127.0.0.1:9117/health"
check_http_endpoint "maintainerr" "http://127.0.0.1:6246/api/health/ready"
check_http_endpoint "couchdb" "http://127.0.0.1:5984/_up" '"status":"ok"'
check_http_endpoint "pihole" "http://127.0.0.1:12120/api/info/login"
check_http_endpoint "seerr" "http://127.0.0.1:5055/api/v1/settings/public"
check_http_endpoint "flaresolverr" "http://127.0.0.1:8191/health"
check_http_endpoint "ollama" "http://127.0.0.1:11434/api/version"
check_http_endpoint "qdirstat" "http://127.0.0.1:5800/site.webmanifest"

python3 - <<'PY'
import socket

with socket.create_connection(("127.0.0.1", 1445), timeout=5):
    print("OK: samba (127.0.0.1:1445) -> TCP reachable")
PY

echo ""
echo "==> Tearing down stack via make down..."
make -C "${REPO_ROOT}" down

remaining="$(cd "${REPO_ROOT}" && docker compose ps -q)"
if [[ -n "${remaining}" ]]; then
  echo "ERROR: expected docker compose ps -q to be empty after make down, found: ${remaining}" >&2
  exit 1
fi

echo "==> System test passed: all ${#EXPECTED_SERVICES[@]} default services reached healthy, passed host-port checks, and tore down cleanly."
