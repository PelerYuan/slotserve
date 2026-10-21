# Configuration

SlotServe reads `SLOTSERVE_*` environment variables, or a `.env` file in its working directory. Docker Compose loads `.env` automatically.

## Compose variables

These only exist in `docker-compose.yml` and describe the **host**.

| Variable | Default | Description |
| --- | --- | --- |
| `LLAMA_CPP_DIR` | *required* | Directory containing `llama-server`; mounted read-only at `/opt/llama.cpp` |
| `MODELS_DIR` | *required* | Directory with `.gguf` files; mounted at `/models` |
| `DATA_DIR` | `./data` | SQLite database and logs; mounted at `/data` |
| `PORT` | `8000` | Published port |
| `CUDA_IMAGE` | `nvidia/cuda:12.8.1-runtime-ubuntu24.04` | Base image; match the CUDA major version of your llama.cpp build |

## SlotServe settings

| Variable | Default | Description |
| --- | --- | --- |
| `SLOTSERVE_INVITE_CODE` | `change-me` | Code required to sign up |
| `SLOTSERVE_REGISTRATION_OPEN` | `true` | Allow sign-up with the invite code |
| `SLOTSERVE_API_KEY_REVEAL` | `false` | Keep an encrypted copy of new API keys so owners can view them again |
| `SLOTSERVE_SECRET_KEY` | `change-me` | Session cookie signing key |
| `SLOTSERVE_HOST` | `0.0.0.0` | Listen address |
| `SLOTSERVE_PORT` | `8000` | Listen port |
| `SLOTSERVE_COOKIE_SECURE` | `false` | Mark cookies `Secure`; set `true` behind HTTPS |
| `SLOTSERVE_DATA_DIR` | `data` | Database and logs |
| `SLOTSERVE_MODELS_DIR` | `models` | Model directory (scanned, and target of downloads) |
| `SLOTSERVE_STATIC_DIR` | *bundled* | Built frontend directory |
| `SLOTSERVE_LLAMA_SERVER_CMD` | `llama-server` | Command used to start llama-server; may include a prefix such as `taskset -c 0-15 llama-server` |
| `SLOTSERVE_LLAMA_HOST` | `127.0.0.1` | Address the model servers bind to (keep it local) |
| `SLOTSERVE_LLAMA_PORT` | `8081` | Port of the first lane's model server; lane *N* uses this + *N* - 1 |
| `SLOTSERVE_DEFAULT_LLAMA_ARGS` | `-ngl 99 -c 8192` | Arguments given to newly registered models |
| `SLOTSERVE_LOAD_TIMEOUT_SECONDS` | `1800` | Give up if a model is not ready after this long |
| `SLOTSERVE_STOP_TIMEOUT_SECONDS` | `20` | Grace period before a model server is killed |
| `SLOTSERVE_DISK_READ_MBPS` | `150` | Used to estimate load time before a model's first load |
| `SLOTSERVE_SLOT_MINUTES` | `30` | Calendar granularity |
| `SLOTSERVE_MAX_RESERVATION_HOURS` | `8` | Longest single booking |
| `SLOTSERVE_MAX_ADVANCE_DAYS` | `30` | How far ahead people can book |
| `SLOTSERVE_PRELOAD_BUFFER_SECONDS` | `60` | Extra margin when preloading before a booking |
| `SLOTSERVE_PRELOAD_MIN_MINUTES` | `3` | Start loading a booked model at least this many minutes early |
| `SLOTSERVE_APPROVAL_MODE` | `off` | `off`, `long` or `all`: which bookings need an admin's approval |
| `SLOTSERVE_APPROVAL_HOURS` | `4` | With `long`, bookings longer than this need approval |
| `SLOTSERVE_VLLM_CMD` | `vllm serve` | Command for the vLLM engine |
| `SLOTSERVE_SGLANG_CMD` | `python -m sglang.launch_server` | Command for the SGLang engine |
| `SLOTSERVE_CUSTOM_ENGINE_CMD` | *empty* | Template for the custom engine: `{model}` `{host}` `{port}` `{name}` |
| `SLOTSERVE_CUSTOM_ENGINE_HEALTH` | `/health` | Path polled until the custom engine is ready |
| `SLOTSERVE_SCHEDULER_INTERVAL_SECONDS` | `5` | How often the scheduler re-evaluates |
| `SLOTSERVE_DOWNLOAD_MIN_FREE_GB` | `2` | Disk space that must remain free after a download |
| `SLOTSERVE_PUBLIC_URL` | *empty* | Address people use to reach SlotServe; used in calendar links |
| `SLOTSERVE_TIMEZONE` | `UTC` | IANA time zone for notifications and weekly quotas |
| `SLOTSERVE_QUOTA_WEEKLY_HOURS` | `0` | Hours one person may book per week (0 = unlimited; admins exempt) |
| `SLOTSERVE_QUOTA_UPCOMING` | `0` | Open bookings one person may hold (0 = unlimited) |
| `SLOTSERVE_REMINDER_MINUTES` | `10` | Lead time of start and end reminders |
| `SLOTSERVE_HF_ENDPOINT` | `https://huggingface.co` | Hugging Face endpoint or mirror |
| `SLOTSERVE_HF_TOKEN` | *empty* | Token for gated or private repositories |
| `SLOTSERVE_TELEGRAM_BOT_TOKEN` | *empty* | Telegram bot token; empty disables the bot |
| `SLOTSERVE_TELEGRAM_PROXY` | *empty* | `http://` or `socks5://` proxy for Telegram |
| `SLOTSERVE_TELEGRAM_API_BASE` | `https://api.telegram.org` | Telegram Bot API address, for a self-hosted Bot API server |
| `SLOTSERVE_CHAT_DEFAULT_LOCALE` | `en` | `en` or `zh-CN`: language of group webhooks, group chats and chats not linked yet |
| `SLOTSERVE_FEISHU_ENABLED`, `_APP_ID`, `_APP_SECRET`, `_DOMAIN` | `false`, *empty*, *empty*, `https://open.feishu.cn` | Feishu / Lark bot ([setup](integrations.md#feishu-lark)); Lark uses `https://open.larksuite.com` |
| `SLOTSERVE_DINGTALK_ENABLED`, `_CLIENT_ID`, `_CLIENT_SECRET`, `_ROBOT_CODE` | `false`, *empty* … | DingTalk bot in Stream mode; robot code empty = client id |
| `SLOTSERVE_WECOM_ENABLED`, `_CORP_ID`, `_AGENT_ID`, `_SECRET`, `_TOKEN`, `_AES_KEY` | `false`, *empty* … | WeCom app; token and AES key only for receiving commands |
| `SLOTSERVE_QQ_ENABLED`, `_APP_ID`, `_APP_SECRET`, `_SANDBOX` | `false`, *empty*, *empty*, `false` | QQ official bot |
| `SLOTSERVE_FEISHU_WEBHOOK_URL`, `_SECRET` | *empty* | Feishu / Lark group robot webhook and signing secret |
| `SLOTSERVE_DINGTALK_WEBHOOK_URL`, `_SECRET` | *empty* | DingTalk group robot webhook and signing secret |
| `SLOTSERVE_WECOM_WEBHOOK_URL` | *empty* | WeCom group robot webhook |
| `SLOTSERVE_GROUP_EVENTS` | `admin_alerts,announcements` | What group webhooks receive: `admin_alerts`, `approvals`, `announcements` |
| `SLOTSERVE_DINGTALK_API_BASE`, `SLOTSERVE_WECOM_API_BASE`, `SLOTSERVE_QQ_API_BASE`, `SLOTSERVE_QQ_TOKEN_URL` | official addresses | API addresses (only for proxies or testing) |
| `SLOTSERVE_METRICS_TOKEN` | *empty* | Bearer token for `/metrics`; empty disables it |
| `SLOTSERVE_DRAIN_SECONDS` | `30` | Before a model switch, wait this long for running requests to finish |
| `SLOTSERVE_USAGE_RETENTION_DAYS` | `365` | Keep per-request rows this long, then fold them into daily totals (0 = forever; otherwise ≥ 90) |
| `SLOTSERVE_USAGE_SAMPLE_DAYS` | `30` | Keep per-minute GPU usage samples this long, then fold them into hourly totals (0 = never fold) |
| `SLOTSERVE_USAGE_BUSY_PERCENT` | `10` | A GPU counts as used in a minute when its utilization is at least this (applies to new samples) |
| `SLOTSERVE_AUDIT_RETENTION_DAYS` | `730` | Keep audit log entries this long (0 = forever) |
| `SLOTSERVE_BACKUP_KEEP` | `7` | Daily database backups to keep (0 = no automatic backups) |
| `SLOTSERVE_DEVENV_ENABLED` | `false` | Dev environments: book GPU time as a Docker container with SSH |
| `SLOTSERVE_DOCKER_HOST` | `unix:///var/run/docker.sock` | Docker (or Podman) API used for dev environments |
| `SLOTSERVE_DEVENV_SSH_HOST` | *empty* | Host name shown in ssh commands; empty = the address the browser uses |
| `SLOTSERVE_DEVENV_SSH_PORTS` | `2200-2299` | Host ports given to dev environments |
| `SLOTSERVE_DEVENV_CPUS` / `_MEMORY_GB` / `_SHM_GB` | `0` / `0` / `8` | Per-environment limits (0 = no limit) |
| `SLOTSERVE_DEVENV_CUSTOM_IMAGES` | `true` | Let people create templates from any image |
| `SLOTSERVE_DEVENV_ALLOWED_REGISTRIES` | *empty* | Comma-separated registries images may come from; empty = any |
| `SLOTSERVE_DEVENV_SAVED_IMAGES` | `3` | Saved environment images per person |
| `SLOTSERVE_DEVENV_PULL_MINUTES` | `60` | Download the image this long before a booking |
| `SLOTSERVE_DEVENV_PASSWORD_LOGIN` | `true` | Allow SSH logins with the environment password as well as keys |
| `SLOTSERVE_DEVENV_STORAGE_DIR` | *empty* | Directory on the Docker host for persistent data (bind mounts); empty = Docker named volumes |
| `SLOTSERVE_STORAGE_HELPER_IMAGE` | `busybox:1.37` | Image of the short-lived containers that check storages from **Admin → Storage** (pulled when missing) |
| `SLOTSERVE_STORAGE_CHECK_MINUTES` | `30` | How often storages are checked (free space, per-person usage); 0 = only on demand |
| `SLOTSERVE_DEVENV_PROJECTS` | `3` | Kept (paused) environments per person; 0 = none |
| `SLOTSERVE_DEVENV_PROJECT_DAYS` | `0` | Remove kept environments unused this many days (0 = never) |
| `SLOTSERVE_DEVENV_CPU_ENABLED` | `true` | Let people start dev environments without a GPU (no booking) |
| `SLOTSERVE_DEVENV_CPU_PER_USER` | `2` | Environments without a GPU one person can run at once |
| `SLOTSERVE_DEVENV_CPU_CORES_PER_GPU` | `8` | CPU cores each lane reserves per GPU for its bookings (cpuset); 0 = none |
| `SLOTSERVE_DEVENV_CPU_CORES` / `_MEMORY_GB` | `4` / `16` | Limits of one environment without a GPU (0 = the whole pool / no limit) |
| `SLOTSERVE_DEVENV_CPU_MEMORY_POOL_GB` | `0` | Memory all environments without a GPU may claim together (0 = no limit) |

Admins can change every setting in **Admin → System settings** without a restart, except the deployment ones (`HOST`, `PORT`, `SECRET_KEY`, `COOKIE_SECURE`, `STATIC_DIR`, `DATA_DIR`, `MODELS_DIR`, `LLAMA_SERVER_CMD`, `LLAMA_HOST`, `LLAMA_PORT`, `TELEGRAM_API_BASE`, the `…_API_BASE` and `QQ_TOKEN_URL` addresses, `DOCKER_HOST`, `DROPBEAR_PATH`), which need a restart and therefore stay in `.env`. Overrides live in the database and win over `.env`; **Use default** removes an override.

## Per-model launch arguments

Each model has a free-form argument string that is appended to the command. For llama.cpp models:

```text
<SLOTSERVE_LLAMA_SERVER_CMD> -m <file> --host 127.0.0.1 --port 8081 --alias <name> <your arguments>
```

vLLM, SGLang and custom engines build their command the same way; see [GPUs, lanes & engines](gpus-and-engines.md#engines).

`-m`, `--host`, `--port` and `--alias` are always set by SlotServe; copies in your arguments are ignored. Useful arguments:

| Goal | Arguments |
| --- | --- |
| Offload all layers | `-ngl 99` |
| Context size | `-c 16384` |
| Parallel requests | `-np 4` (context is split between slots) |
| Flash attention | `--flash-attn on` |
| Vision models | `--mmproj /models/mmproj-model-f16.gguf` |
| Embeddings | `--embedding --pooling mean` |
| KV cache quantization | `-ctk q8_0 -ctv q8_0` |

Run `llama-server --help` for the full list. After editing the arguments of the loaded model, press **Reload model** on the overview page.

## Backups and restore

SlotServe copies its database to `data/backups/` every 24 hours (SQLite's online backup, safe while running) and keeps the newest `SLOTSERVE_BACKUP_KEEP` copies. **Admin → Overview → Backups** lists them, makes one on demand and downloads them. A copy is also taken automatically before every database upgrade; those are never deleted.

To restore, stop the container, replace `data/slotserve.db` with a backup file and start it again:

```bash
docker compose stop
cp data/backups/slotserve-daily-20261020-030000.db data/slotserve.db
rm -f data/slotserve.db-wal data/slotserve.db-shm
docker compose start
```

Keep a copy of `data/` somewhere else as well - backups on the same disk do not survive a disk failure.

## Admin command line

Most account tasks are done in **Admin → Users**. The command line is for when no admin can sign in:

```bash
docker compose exec slotserve slotserve reset-password <username>
docker compose exec slotserve slotserve set-admin <username>
docker compose exec slotserve slotserve set-admin <username> --revoke
```
