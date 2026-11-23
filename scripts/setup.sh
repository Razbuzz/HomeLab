#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# 0. Enable IP forwarding persistently and idempotently
sudo tee /etc/sysctl.d/99-homelab.conf >/dev/null <<'EOF'
net.ipv4.ip_forward=1
net.ipv6.conf.all.forwarding=1
EOF
sudo sysctl -p /etc/sysctl.d/99-homelab.conf

# 1. Export variables from .env so the shell can use them
if [[ -f "${REPO_ROOT}/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  . "${REPO_ROOT}/.env"
  set +a
fi

# 2. Ensure bind-mount app directories exist with expected non-root ownership
"${SCRIPT_DIR}/fix-permissions.sh"

# 3. Start containers (Docker reads .env automatically)
(
  cd "${REPO_ROOT}"
  docker compose up -d --pull always --build --remove-orphans
)

# 4. Fix storage mount permissions using DEFAULT_PUID/DEFAULT_GUID
if [[ -n "${DEFAULT_STORAGE_MOUNT:-}" ]]; then
  if [[ "$(id -u)" -eq 0 ]]; then
    chown -R "${DEFAULT_PUID}:${DEFAULT_GUID}" "${DEFAULT_STORAGE_MOUNT}"
  else
    sudo chown -R "${DEFAULT_PUID}:${DEFAULT_GUID}" "${DEFAULT_STORAGE_MOUNT}"
  fi
fi

# 5. Restart services to apply storage permission changes, then stop stack
(
  cd "${REPO_ROOT}"
  docker compose restart
  docker compose down
)
