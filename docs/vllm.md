# vLLM (Opt-in GPU Inference)

[vLLM](https://docs.vllm.ai/en/stable/) runs as an opt-in OpenAI-compatible inference server (`vllm/vllm-openai:v0.34.0`) under the Docker Compose `vllm` profile. It pulls Hugging Face models on demand into persistent host cache (`${DEFAULT_APPS_MOUNT}/${VLLM_FOLDER}`) and runs side-by-side with `ollama` (`:11434`) on a single NVIDIA GPU.

Because it uses `profiles: ["vllm"]`, standard `make up`, `make up-beta`, `make restart`, `make upgrade`, and systemd boot skip `vllm` unless you opt in via `make up-vllm` (or add `COMPOSE_PROFILES=vllm` to your server's `.env`).

---

## Prerequisites

- **NVIDIA GPU & Container Toolkit:** An NVIDIA GPU with [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) installed and configured (`runtime: nvidia`, matching `ollama` and `plex`).
- **Host NVIDIA driver:** `vllm/vllm-openai:v0.34.0` is built on CUDA 13.0.2 (Ubuntu 24.04) and requires host NVIDIA driver **R580 or newer** (`nvidia-smi` to check your driver version).
- **Required `.env` secret (`VLLM_API_KEY`):** The container fails closed on startup if `VLLM_API_KEY` is empty or still `CHANGE_TO_VLLM_API_KEY`. Generate a strong token first:
  ```bash
  openssl rand -hex 24
  ```
  Set `VLLM_API_KEY=<generated-token>` in `.env` (`chmod 600 .env`).
- **Optional Hugging Face token (`VLLM_HF_TOKEN`):** Needed **only** for gated or private Hugging Face repositories (e.g., Llama or Gemma gated weights). Leave `VLLM_HF_TOKEN=CHANGE_TO_HF_TOKEN` for public models—the container entrypoint automatically unsets `HF_TOKEN` when blank or `CHANGE_TO_*` so public Hugging Face downloads never fail with HTTP 401.

---

## Pulling & Switching Hugging Face Models on Demand

Native `vllm serve` loads one base model per running server instance. Persistent weights (`HF_HOME=/home/vllm/.cache/huggingface`) and compiled Triton/Inductor/FlashInfer artifacts (`VLLM_CACHE_ROOT=/home/vllm/.cache/vllm`) live under `${DEFAULT_APPS_MOUNT}/${VLLM_FOLDER}` (`./apps/vllm` by default) so subsequent starts reuse cached weights without re-downloading.

```bash
# 1. Start vLLM with the model configured in .env (default: Qwen/Qwen3-8B-AWQ)
make up-vllm
docker logs -f vllm

# 2. Switch to any Hugging Face repo on demand (updates .env atomically, repairs permissions, recreates vllm)
make vllm-model MODEL=Qwen/Qwen3-8B-AWQ PARSER=hermes

# Examples with different architectures & tool-call parsers:
make vllm-model MODEL=Qwen/Qwen3-14B-AWQ PARSER=qwen3_coder
make vllm-model MODEL=NousResearch/Hermes-3-Llama-3.1-8B PARSER=hermes
make vllm-model MODEL=meta-llama/Llama-3.1-8B-Instruct PARSER=llama3_json

# 3. Stop and remove vLLM to reclaim 100% of its GPU VRAM and host RAM
make down-vllm
```

You can also run `scripts/vllm-model.sh` directly for dry runs or `.env`-only updates:

```bash
bash scripts/vllm-model.sh --model Qwen/Qwen3-8B-AWQ --parser hermes --dry-run
bash scripts/vllm-model.sh --model Qwen/Qwen3-8B-AWQ --no-restart
```

> **Note:** Initial model downloads and CUDA graph capture can take a few minutes depending on link speed and model size. The container `healthcheck` configures `start_period: 300s` so initial pulls do not trigger false unhealthy states.

---

## Running vLLM in Parallel with Ollama (GPU VRAM Budgeting)

Starting with `vLLM v0.34.0`, extensible KV cache is enabled by default, changing upstream vLLM's default `--gpu-memory-utilization` from `0.9` to `1.0` (claiming 100% of GPU memory). HomeLab explicitly sets **`VLLM_GPU_MEMORY_UTILIZATION=0.45`**, **`VLLM_MAX_MODEL_LEN=32768`**, and **`VLLM_MAX_NUM_SEQS=8`** so `vllm` can share a single GPU alongside `ollama` (`:11434`) and `plex` hardware transcoding.

Because `--gpu-memory-utilization` caps vLLM as a fraction of **total** GPU VRAM (not remaining free VRAM) and HomeLab configures `OLLAMA_KEEP_ALIVE=24h` by default, budget combined VRAM across both engines:

| GPU VRAM | Recommended `VLLM_GPU_MEMORY_UTILIZATION` | Recommended `VLLM_MAX_MODEL_LEN` | Concurrent Ollama Budget |
| :--- | :--- | :--- | :--- |
| **12 GB** | `0.40`–`0.45` *(tight)* | `16384`–`32768` | Unload Ollama (`docker exec ollama ollama stop <model>`) or run small 4B–8B tags; use `make down-vllm` when Ollama needs full VRAM. |
| **16 GB** | `0.45` | `32768` | ~8.5 GB remaining for Ollama (`qwen3.5:9b` / `gemma4:e4b`) + Plex NVENC/NVDEC. |
| **24 GB** *(RTX 3090/4090)* | `0.45` *(or `0.55` for 14B–32B AWQ/FP8)* | `32768`–`65536` | ~10–13 GB remaining for `gemma4:12b` / `qwen3.5:9b` concurrently without CPU spill. |
| **Dedicated vLLM mode** | `0.85`–`0.90` | `65536+` | Stop or unload Ollama first (`docker exec ollama ollama stop <model>` or set `OLLAMA_KEEP_ALIVE=15m`). |

If `vllm` logs `Free memory on device is less than desired GPU memory utilization`, unload any pinned Ollama model or stop one of the two engines (`make down-vllm` frees 100% of vLLM's VRAM immediately).

> **Backups & rollbacks:** `scripts/backup.sh` excludes `./${VLLM_FOLDER}/huggingface` and `./${VLLM_FOLDER}/vllm` by default (just like `./${OLLAMA_FOLDER}/models`) so multi-GB Hugging Face weights are not re-archived during routine upgrades and are preserved untouched in place on rollback. Pass `--include-heavy` to `scripts/backup.sh` if you ever want weights included in a snapshot.

---

## Connecting Hermes Agent & OpenAI-Compatible Clients

Because `vllm` joins the default Docker Compose network, `hermes-agent` (when started with `make up-beta`) and other containers reach vLLM internally over Compose DNS at:

- **Internal Compose base URL:** `http://vllm:8000/v1`
- **Host loopback base URL:** `http://127.0.0.1:8000/v1` (controlled by `VLLM_BIND_IP` and `VLLM_PORT`)
- **Served model name:** `${VLLM_SERVED_MODEL_NAME}` (default `homelab-vllm`, plus the Hugging Face repo ID)

### Example: Hermes Agent (`~/.hermes/config.yaml` / `${DEFAULT_APPS_MOUNT}/${HERMES_FOLDER}/config.yaml`)

Keep Ollama as your always-on default and switch to vLLM on demand via `/model` inside Hermes (no `depends_on` link is needed between `hermes-agent` and `vllm`):

```yaml
model:
  default: "gemma4:12b"
  provider: "custom"
  base_url: "http://ollama:11434/v1"
  api_key: "no-key-required"

providers:
  vllm:
    api: "http://vllm:8000/v1"
    api_key: "<your-VLLM_API_KEY>"
    discover_models: true

model_aliases:
  vllm-active: "vllm/homelab-vllm"
```

Or use `vllm` directly from any OpenAI SDK / `curl` client on the host:

```bash
curl -fsS http://127.0.0.1:8000/health

curl -fsS http://127.0.0.1:8000/v1/models \
  -H "Authorization: Bearer ${VLLM_API_KEY}"

curl -fsS http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${VLLM_API_KEY}" \
  -d '{
    "model": "homelab-vllm",
    "messages": [{"role": "user", "content": "Reply with one word: ready."}]
  }'
```

---

## Security Notes

- **Loopback bind by default (`VLLM_BIND_IP=127.0.0.1`):** Upstream vLLM enforces `VLLM_API_KEY` on `/v1/*`, `/v2/*`, `/inference/*`, and `/cohere/*`, while leaving routes such as `/health`, `/metrics`, `/tokenize`, `/detokenize`, `/invocations`, `/pause`, and `/resume` **unauthenticated**. Keeping `VLLM_BIND_IP=127.0.0.1` ensures unauthenticated endpoints are never exposed to the LAN, while containers on the Docker network reach `http://vllm:8000/v1` cleanly. If you ever expose `:8000` beyond loopback, place it behind an authenticating reverse proxy (or Tailscale ACLs) rather than setting `VLLM_BIND_IP=0.0.0.0` on an untrusted LAN.
- **Fail-closed API key:** `vllm` refuses to start if `VLLM_API_KEY` is empty or `CHANGE_TO_*`.
- **Least-privilege non-root execution:** Runs with `user: "${DEFAULT_PUID}:0"` (`HOME=/home/vllm`) using upstream vLLM's GID `0` group-writable cache permissions (managed by `scripts/fix-permissions.sh`), without mounting `/var/run/docker.sock` and without `VLLM_SERVER_DEV_MODE=1`.
- **Telemetry disabled:** Sets `VLLM_NO_USAGE_STATS=1` and `DO_NOT_TRACK=1`.
