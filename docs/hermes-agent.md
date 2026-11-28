# Hermes Agent (Beta)

Hermes Agent ([GitHub](https://github.com/NousResearch/hermes-agent) · [Official Docs](https://hermes-agent.nousresearch.com/docs/) · [Docker Hub](https://hub.docker.com/r/nousresearch/hermes-agent)) is an MIT-licensed, self-hosted autonomous AI agent running as an opt-in `beta` service in HomeLab (`nousresearch/hermes-agent:v0.23.3`).

Because it uses Compose `profiles: ["beta"]`, standard `make up`, `make restart`, `make upgrade`, and systemd boot ignore it unless enabled. Use `make up-beta` and `make down-beta` for on-demand runs alongside `ollama`, or set `COMPOSE_PROFILES=beta` in your server's `.env` (or exported environment) to start and keep Hermes running automatically across `make up`, `make restart`, `make upgrade`, and host reboots. (Do **not** commit `COMPOSE_PROFILES=beta` to the repository `.env` template so default `make up` stays non-beta.)

## Architecture & Why This Stack

```text
User (Web Dashboard :9119 / CLI `docker exec -it hermes-agent hermes`)
   │
   ▼
hermes-agent (s6-overlay PID 1 -> drops to UID/GID 1000:1000, data in /opt/data)
   ├──► Primary LLM:  http://ollama:11434/v1  (64K context, LAN-private, $0)
   ├──► Fallback LLM: OpenRouter API          (optional per-turn failover when Ollama errors)
   └──► Gateways:     Optional outbound chat  (Telegram, Discord, Slack, etc.)
```

Key design choices:
- **64K context on Ollama**: Hermes sends ~10K–13K+ tokens of instructions, memory, and tool schemas on every turn. HomeLab configures `OLLAMA_CONTEXT_LENGTH=65536`, `OLLAMA_KEEP_ALIVE=24h`, `OLLAMA_FLASH_ATTENTION=1`, and `OLLAMA_KV_CACHE_TYPE=q8_0` directly on the `ollama` service so Ollama never silently truncates prompts to 4096 tokens and does not clamp cloud fallback context windows (avoiding `model.ollama_num_ctx` bug `#99943`).
- **Built-in Web Dashboard (`:9119`) & CLI**: Bound to `${HERMES_DASHBOARD_BIND_IP}:${HERMES_DASHBOARD_PORT}:9119` (`HERMES_DASHBOARD_BIND_IP` defaults to `0.0.0.0` so LAN, `localhost`, and Tailscale `100.x.y.z` access work out of the box) behind mandatory fail-closed Basic Auth (`HERMES_DASHBOARD_BASIC_AUTH_*`) on every non-loopback request, plus interactive/one-shot CLI access via `docker exec -it hermes-agent hermes`. Set `HERMES_DASHBOARD_BIND_IP=127.0.0.1` or your specific LAN/Tailscale IP to restrict binding to a single interface.
- **Optional outbound messaging gateways**: No local chat server is bundled in the stack; if you want mobile or chat-based access later, Hermes supports outbound gateways (Telegram, Discord, Slack, etc.) with zero extra inbound ports.
- **Isolated local execution**: Runs `terminal.backend: local` inside the container as UID/GID `1000:1000` without mounting `/var/run/docker.sock`.

---

## VRAM-Tier Model Recommendations (Ollama at 64K Context)

| GPU VRAM Tier | Recommended Ollama Tag(s) | Notes & Trade-offs |
| --- | --- | --- |
| **8 GB VRAM** | `qwen3.5:9b` *(tight)*, `gemma4:e4b`, or `qwen3.5:4b` | Requires `OLLAMA_KV_CACHE_TYPE=q8_0` (or `q4_0`); tight for long multi-step tool chains. Consider OpenRouter workhorse primary if reliability falters. |
| **12 GB VRAM** | `gemma4:12b` *(primary)* or `qwen3.5:9b` | Best 12 GB sweet spot with `OLLAMA_FLASH_ATTENTION=1` + `OLLAMA_KV_CACHE_TYPE=q8_0`. `gemma4:12b` includes multimodal vision; `qwen3.5:9b` leaves extra KV headroom. |
| **16 GB VRAM** | `gemma4:12b` *(all-rounder)* or `qwen3.5:9b` *(speed/ctx)* | Runs `gemma4:12b` effortlessly at 64K–128K context with zero CPU spill. |
| **24 GB VRAM** *(RTX 3090/4090)* | `qwen3.8:27b` *(coding/agents)*, `gemma4:31b` *(strict tools)*, or `gemma4:26b` *(MoE speed)* | `qwen3.8:27b` leads coding benchmarks; `gemma4:31b` is Nous's official Ollama pick (keep `OLLAMA_KV_CACHE_TYPE=q8_0`); `gemma4:26b` delivers fastest tokens/sec. |

### Optional Hybrid Cloud Fallback (OpenRouter)

The template [`configs/hermes/config.yaml.example`](../configs/hermes/config.yaml.example) defaults to `fallback_providers: []` so 100% local operation works immediately even with placeholder `.env` keys. To enable automatic per-turn cloud failover when Ollama errors or times out:

1. Set a real `HERMES_OPENROUTER_API_KEY` in `.env`.
2. In `${DEFAULT_APPS_MOUNT}/${HERMES_FOLDER}/config.yaml`, replace `fallback_providers: []` with:
   ```yaml
   fallback_providers:
     - provider: "openrouter"
       model: "deepseek/deepseek-v4.1-flash-1017"
   ```
3. Restart Hermes and verify live failover:
   ```bash
   docker exec hermes-agent hermes fallback check --live
   ```

> **Privacy note:** Avoid routing sensitive credentials or private files through third-party APIs; keep sensitive tasks on local Ollama.

---

## Optional External Messaging Gateways

Out of the box, HomeLab exposes Hermes through the Web Dashboard (`http://<your-server-ip>:9119`, over LAN or Tailscale) and container CLI (`docker exec -it hermes-agent hermes`). If you also want to interact with Hermes from Telegram, Discord, Slack, or another supported platform:

1. Follow the [Official Hermes Messaging Docs](https://hermes-agent.nousresearch.com/docs/user-guide/messaging) to create a bot token and find your numeric/platform user ID.
2. Pass the platform environment variables to the `hermes-agent` service (or store non-secret gateway settings in `${DEFAULT_APPS_MOUNT}/${HERMES_FOLDER}/config.yaml` while keeping tokens in `.env`).
3. Keep `gateway.allow_all_users: false` and `unauthorized_dm_behavior: "ignore"` (already set in `configs/hermes/config.yaml.example`) and explicitly allowlist only your own user/channel IDs.

---

## Security Hardening Checklist

- [x] **No `docker.sock` mount**: `terminal.backend: local` runs commands strictly inside the `hermes-agent` container as UID/GID `1000:1000`. Do not add `/var/run/docker.sock` or `cap_drop: [ALL]` (s6-overlay `/init` remaps `HERMES_UID`/`HERMES_GID` and drops privileges via `s6-setuidgid`).
- [x] **Configurable Interface Binding + Mandatory Basic Auth**: Bound via `${HERMES_DASHBOARD_BIND_IP}:${HERMES_DASHBOARD_PORT}:9119` (`0.0.0.0` by default so LAN and Tailscale `100.x.y.z` work out of the box; set `HERMES_DASHBOARD_BIND_IP=127.0.0.1` or your specific LAN/Tailscale IP to bind a single interface). Fail-closed Basic Auth requires username, password, and a random secret (`openssl rand -base64 32`) and is enforced on every non-loopback request.
- [x] **API server disabled**: `API_SERVER_ENABLED=false` and port `8642` is not published.
- [x] **Fail-closed gateway defaults**: `gateway.allow_all_users: false` and `unauthorized_dm_behavior: "ignore"` prevent unauthorized access if you enable an external messaging gateway.
- [x] **Approval gates & loop guardrails**: `approvals.mode: "smart"` with `cron_mode`, `single_query_mode`, and `unattended_mode` set to `"deny"`, plus `tool_loop_guardrails.hard_stop_enabled: true`.
- [x] **SSRF protection**: `security.allow_private_urls: false` and `browser.allow_private_urls: false` prevent `web_extract` and headless Chromium from probing other LAN services or cloud metadata (`169.254.169.254`).
- [x] **Secret hygiene**: Keep `.env` at `chmod 600` on the host, use `CHANGE_TO_*` placeholders in git, and never paste secrets into `config.yaml` or ask Hermes to read `.env`.
- [x] **Resource bounds**: `shm_size: "1g"` (prevents Chromium crashes), `mem_limit: 4g`, `pids_limit: 512`.

---

## First-Boot Setup

1. **Pre-seed `config.yaml` before first start** (interactive `hermes setup` hangs headless without a TTY; pre-seeding prevents auto-generating generic defaults):
   ```bash
   set -a && source .env && set +a
   mkdir -p "${DEFAULT_APPS_MOUNT}/${HERMES_FOLDER}"
   cp configs/hermes/config.yaml.example "${DEFAULT_APPS_MOUNT}/${HERMES_FOLDER}/config.yaml"
   chown -R "${DEFAULT_PUID}:${DEFAULT_GUID}" "${DEFAULT_APPS_MOUNT}/${HERMES_FOLDER}"
   ```
   Edit `${DEFAULT_APPS_MOUNT}/${HERMES_FOLDER}/config.yaml` if you want a different `model.default` tag for your GPU VRAM tier.

2. **Configure `.env`**:
   - Set `DEFAULT_TIMEZONE`, `DEFAULT_STORAGE_MOUNT` (and `DEFAULT_LAN_IP` if used by other host references).
   - Keep `HERMES_DASHBOARD_BIND_IP=0.0.0.0` for LAN + Tailscale (`100.x.y.z`) access, or restrict it to `127.0.0.1` or a specific LAN/Tailscale IP.
   - Set `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD` and generate `HERMES_DASHBOARD_BASIC_AUTH_SECRET` (`openssl rand -base64 32`).
   - Optionally set `HERMES_OPENROUTER_API_KEY` (or leave as placeholder if 100% local).
   - *(Optional — keep Hermes enabled automatically)* Add `COMPOSE_PROFILES=beta` to your server's `.env` (or export `COMPOSE_PROFILES=beta` in your shell) so `make up`, `make restart`, `make upgrade`, and systemd boot (`homelab.service`) include `hermes-agent` automatically. (`merge-env.py` preserves custom `.env` keys like `COMPOSE_PROFILES` across upgrades.)
   - Restrict permissions on the host: `chmod 600 .env`.

3. **Start the beta stack and pull your Ollama model**:
   ```bash
   make up-beta
   docker exec -it ollama ollama pull gemma4:12b
   ```

4. **Use Hermes via Web Dashboard or CLI (optional headless mode)**:
   - Open `http://<your-server-ip>:9119` (LAN IP, Tailscale IP/MagicDNS, or `localhost`) in your browser and sign in with `HERMES_DASHBOARD_BASIC_AUTH_USERNAME` / `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD`, or run `docker exec -it hermes-agent hermes` for the CLI.
   - If you run headless (e.g., CLI or an external messaging gateway only), set `HERMES_DASHBOARD=0` in `.env` and restart (`make up-beta`). The built-in healthcheck automatically falls back to checking `/opt/data/gateway.sock`.

---

## Verification & Troubleshooting

```bash
# Check container health, profile status, host bind, and startup logs
docker compose --profile beta ps hermes-agent
docker logs --tail 50 hermes-agent
ss -tlnp | grep 9119   # expects *:9119 (0.0.0.0) or your configured HERMES_DASHBOARD_BIND_IP:9119

# Verify Hermes CLI diagnostics inside the container
docker exec hermes-agent hermes doctor
docker exec hermes-agent hermes gateway status

# Verify fail-closed Web Dashboard auth (/api/status is public; / redirects to /login; /api/* returns 401)
HOST_IP="${DEFAULT_LAN_IP:-127.0.0.1}"   # or your Tailscale 100.x.y.z / localhost address
curl -fsS "http://${HOST_IP}:9119/api/status"
curl -s -o /dev/null -w '%{http_code}\n' "http://${HOST_IP}:9119/"              # expects 302 (/login)
curl -s -o /dev/null -w '%{http_code}\n' "http://${HOST_IP}:9119/api/sessions"  # expects 401
curl -fsS -c /tmp/hermes-cookies.txt -X POST -H "Content-Type: application/json" \
  -d "{\"provider\":\"basic\",\"username\":\"${HERMES_DASHBOARD_BASIC_AUTH_USERNAME}\",\"password\":\"${HERMES_DASHBOARD_BASIC_AUTH_PASSWORD}\",\"next\":\"/\"}" \
  "http://${HOST_IP}:9119/auth/password-login"
curl -s -b /tmp/hermes-cookies.txt -o /dev/null -w '%{http_code}\n' "http://${HOST_IP}:9119/"  # expects 200
rm -f /tmp/hermes-cookies.txt
```

- **Connection refused on `:9119`**:
  1. Confirm `hermes-agent` is actually running (`docker compose --profile beta ps hermes-agent`). Remember `make up` and `make restart` skip `profiles: ["beta"]` unless `make up-beta` or `COMPOSE_PROFILES=beta` is used.
  2. Check `HERMES_DASHBOARD_BIND_IP` in `.env` and `ss -tlnp | grep 9119` — if it is bound to a single IP that does not match your Tailscale or active LAN interface, set `HERMES_DASHBOARD_BIND_IP=0.0.0.0` and run `make up-beta`.
  3. Inspect `docker logs hermes-agent` for startup or auth configuration errors (missing `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD` / `HERMES_DASHBOARD_BASIC_AUTH_SECRET`).

> **Note (`#152519`):** `hermes` CLI invocations on `v0.23.3` may print a harmless `pnpm: not installed or outdated` warning; it does not affect gateway or dashboard operation.

---

## Recommended Future Complements

- **SearXNG (`searxng/searxng`)**: Self-hosted private web search backend for Hermes (`web.search_backend: searxng` + `SEARXNG_URL=http://searxng:8080` with `json` format enabled).
- **Scoped Read-Only MCP Servers**: Connect Prometheus/Grafana/Loki or Home Assistant MCP servers with strict `mcp_servers.<name>.tools.include` allowlists to avoid bloating local Ollama context.
- **Official References**:
  - [Hermes Docker Guide](https://hermes-agent.nousresearch.com/docs/user-guide/docker)
  - [Local Ollama Setup](https://hermes-agent.nousresearch.com/docs/guides/local-ollama-setup)
  - [Messaging Gateways Overview](https://hermes-agent.nousresearch.com/docs/user-guide/messaging)
  - [Security Hardening Guide](https://hermes-agent.nousresearch.com/docs/user-guide/security)
  - [Fallback Providers](https://hermes-agent.nousresearch.com/docs/user-guide/features/fallback-providers)
