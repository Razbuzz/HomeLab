# Hermes Agent (Beta)

Hermes Agent ([GitHub](https://github.com/NousResearch/hermes-agent) · [Official Docs](https://hermes-agent.nousresearch.com/docs/) · [Docker Hub](https://hub.docker.com/r/nousresearch/hermes-agent)) is an MIT-licensed, self-hosted autonomous AI agent running as an opt-in `beta` service in HomeLab (`nousresearch/hermes-agent:v0.23.3`).

Because it uses Compose `profiles: ["beta"]`, standard `make up` ignores it completely. Use `make up-beta` and `make down-beta` when you want Hermes running alongside `ollama` and `mattermost`.

## Architecture & Why This Stack

```text
User (Mattermost / Web Dashboard :9119)
   │
   ▼
hermes-agent (s6-overlay PID 1 -> drops to UID/GID 1000:1000, data in /opt/data)
   ├──► Primary LLM:  http://ollama:11434/v1  (64K context, LAN-private, $0)
   ├──► Chat Gateway: http://mattermost:8065  (outbound REST v4 + WebSocket, no inbound ports)
   └──► Fallback LLM: OpenRouter API          (optional per-turn failover when Ollama errors)
```

Key design choices:
- **64K context on Ollama**: Hermes sends ~10K–13K+ tokens of instructions, memory, and tool schemas on every turn. HomeLab configures `OLLAMA_CONTEXT_LENGTH=65536`, `OLLAMA_KEEP_ALIVE=24h`, `OLLAMA_FLASH_ATTENTION=1`, and `OLLAMA_KV_CACHE_TYPE=q8_0` directly on the `ollama` service so Ollama never silently truncates prompts to 4096 tokens and does not clamp cloud fallback context windows (avoiding `model.ollama_num_ctx` bug `#99943`).
- **Outbound Mattermost gateway**: Connects internally over the Compose network (`http://mattermost:8065`) with zero extra inbound ports on `hermes-agent`.
- **Built-in Web Dashboard (`:9119`)**: Bound strictly to `${DEFAULT_LAN_IP}:${HERMES_DASHBOARD_PORT}:9119` with mandatory fail-closed Basic Auth (`HERMES_DASHBOARD_BASIC_AUTH_*`).
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

## Mattermost Bot Setup

1. **Enable Bot Accounts**: Log in to Mattermost (`http://<DEFAULT_LAN_IP>:8065`) as System Admin → **System Console → Integrations → Bot Accounts** → set **Enable Bot Account Creation** = `true` → **Save**.
2. **Create Bot Account**: Open **☰ → Integrations → Bot Accounts → Add Bot Account** (e.g., username `hermes`, display name `Hermes Agent`, role `Member`) → **Create Bot Account**. Save the one-time **bot token** immediately (`HERMES_MATTERMOST_TOKEN`).
3. **Add Bot to Channels**: In any channel where Hermes should respond, click **Channel Name → Add Members** and add `@hermes`. (DMs work without channel membership.)
4. **Find Your 26-Character User ID** *(required — NOT `@username`!)*:
   - In System Console → **User Management → Users → open your user**, or
   - While logged in, visit `http://<DEFAULT_LAN_IP>:8065/api/v4/users/me`, or
   - Query via bot token:
     ```bash
     curl -s -H "Authorization: Bearer <BOT_TOKEN>" \
       "http://<DEFAULT_LAN_IP>:8065/api/v4/users/username/<YOUR_USERNAME>"
     ```
     Copy the 26-character `"id"` into `HERMES_MATTERMOST_ALLOWED_USERS`.
5. **Find Channel IDs**: Open **Channel Name → View Info** (or copy from the channel URL) for `HERMES_MATTERMOST_ALLOWED_CHANNELS` and `HERMES_MATTERMOST_HOME_CHANNEL`.

### Known Mattermost Caveats (`v0.23.3`)

- **`MATTERMOST_REPLY_MODE` must stay in `.env`** (`#54227`): `mattermost.reply_mode` in `config.yaml` is ignored by `_apply_yaml_config`. Keep `HERMES_MATTERMOST_REPLY_MODE=thread` (or `off`) in `.env`.
- **DM threading nuance (`#138303`)**: With `HERMES_MATTERMOST_REPLY_MODE=thread`, replying *inside* a DM thread starts a new session. Reply at the top level in DMs, or switch `HERMES_MATTERMOST_REPLY_MODE=off` if your workflow is mostly DMs.
- **Keep `HERMES_MATTERMOST_REQUIRE_MENTION=true`** (`#143420`): Prevents multi-bot silence-marker loops in channels.
- **Slash commands on mobile (`#25184`)**: Mattermost mobile intercepts leading `/`; use the web/desktop client for `/status` or `/model`, or prefix with a space.
- **Reverse proxy WebSockets**: If proxying Mattermost through nginx/Caddy, forward `/api/v4/websocket` upgrade headers with `proxy_read_timeout 600s`.

---

## Security Hardening Checklist

- [x] **No `docker.sock` mount**: `terminal.backend: local` runs commands strictly inside the `hermes-agent` container as UID/GID `1000:1000`. Do not add `/var/run/docker.sock` or `cap_drop: [ALL]` (s6-overlay `/init` remaps `HERMES_UID`/`HERMES_GID` and drops privileges via `s6-setuidgid`).
- [x] **LAN-only Dashboard + Mandatory Basic Auth**: Bound to `${DEFAULT_LAN_IP}:${HERMES_DASHBOARD_PORT}:9119` (use `127.0.0.1` for SSH/Tailscale-only access). Fail-closed Basic Auth requires username, password, and a random secret (`openssl rand -base64 32`).
- [x] **API server disabled**: `API_SERVER_ENABLED=false` and port `8642` is not published.
- [x] **Strict Mattermost allowlists**: `gateway.allow_all_users: false`, `unauthorized_dm_behavior: "ignore"`, plus explicit `HERMES_MATTERMOST_ALLOWED_USERS` and `HERMES_MATTERMOST_ALLOWED_CHANNELS`.
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
   - Set `DEFAULT_LAN_IP`, `DEFAULT_TIMEZONE`, `DEFAULT_STORAGE_MOUNT`.
   - Set `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD` and generate `HERMES_DASHBOARD_BASIC_AUTH_SECRET` (`openssl rand -base64 32`).
   - Fill `HERMES_MATTERMOST_*` values from the Mattermost Bot Setup steps above.
   - Optionally set `HERMES_OPENROUTER_API_KEY` (or leave as placeholder if 100% local).
   - Restrict permissions on the host: `chmod 600 .env`.

3. **Start the beta stack and pull your Ollama model**:
   ```bash
   make up-beta
   docker exec -it ollama ollama pull gemma4:12b
   ```

4. **Disable the Web Dashboard (optional)**:
   If you only want the Mattermost bot, set `HERMES_DASHBOARD=0` in `.env` and restart (`make up-beta`). The built-in healthcheck automatically falls back to checking `/opt/data/gateway.sock`.

---

## Verification Commands

```bash
# Check container health and startup logs
docker ps --filter name=hermes-agent
docker logs --tail 50 hermes-agent

# Verify Hermes CLI diagnostics inside the container
docker exec hermes-agent hermes doctor
docker exec hermes-agent hermes gateway status

# Verify fail-closed Web Dashboard auth (/api/status is public; / redirects to /login; /api/* returns 401)
curl -fsS "http://${DEFAULT_LAN_IP}:9119/api/status"
curl -s -o /dev/null -w '%{http_code}\n' "http://${DEFAULT_LAN_IP}:9119/"              # expects 302 (/login)
curl -s -o /dev/null -w '%{http_code}\n' "http://${DEFAULT_LAN_IP}:9119/api/sessions"  # expects 401
curl -fsS -c /tmp/hermes-cookies.txt -X POST -H "Content-Type: application/json" \
  -d "{\"provider\":\"basic\",\"username\":\"${HERMES_DASHBOARD_BASIC_AUTH_USERNAME}\",\"password\":\"${HERMES_DASHBOARD_BASIC_AUTH_PASSWORD}\",\"next\":\"/\"}" \
  "http://${DEFAULT_LAN_IP}:9119/auth/password-login"
curl -s -b /tmp/hermes-cookies.txt -o /dev/null -w '%{http_code}\n' "http://${DEFAULT_LAN_IP}:9119/"  # expects 200
rm -f /tmp/hermes-cookies.txt
```

> **Note (`#152519`):** `hermes` CLI invocations on `v0.23.3` may print a harmless `pnpm: not installed or outdated` warning; it does not affect gateway or dashboard operation.

---

## Recommended Future Complements

- **SearXNG (`searxng/searxng`)**: Self-hosted private web search backend for Hermes (`web.search_backend: searxng` + `SEARXNG_URL=http://searxng:8080` with `json` format enabled).
- **Scoped Read-Only MCP Servers**: Connect Prometheus/Grafana/Loki or Home Assistant MCP servers with strict `mcp_servers.<name>.tools.include` allowlists to avoid bloating local Ollama context.
- **Official References**:
  - [Hermes Docker Guide](https://hermes-agent.nousresearch.com/docs/user-guide/docker)
  - [Local Ollama Setup](https://hermes-agent.nousresearch.com/docs/guides/local-ollama-setup)
  - [Mattermost Messaging Setup](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/mattermost)
  - [Security Hardening Guide](https://hermes-agent.nousresearch.com/docs/user-guide/security)
  - [Fallback Providers](https://hermes-agent.nousresearch.com/docs/user-guide/features/fallback-providers)
