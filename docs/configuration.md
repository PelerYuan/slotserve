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
| `SLOTSERVE_SECRET_KEY` | `change-me` | Session cookie signing key |
| `SLOTSERVE_HOST` | `0.0.0.0` | Listen address |
| `SLOTSERVE_PORT` | `8000` | Listen port |
| `SLOTSERVE_COOKIE_SECURE` | `false` | Mark cookies `Secure`; set `true` behind HTTPS |
| `SLOTSERVE_DATA_DIR` | `data` | Database and logs |
| `SLOTSERVE_MODELS_DIR` | `models` | Model directory (scanned, and target of downloads) |
| `SLOTSERVE_STATIC_DIR` | *bundled* | Built frontend directory |
| `SLOTSERVE_LLAMA_SERVER_CMD` | `llama-server` | Command used to start llama-server; may include a prefix such as `taskset -c 0-15 llama-server` |
| `SLOTSERVE_LLAMA_HOST` | `127.0.0.1` | Address llama-server binds to (keep it local) |
| `SLOTSERVE_LLAMA_PORT` | `8081` | Port llama-server binds to |
| `SLOTSERVE_DEFAULT_LLAMA_ARGS` | `-ngl 99 -c 8192` | Arguments given to newly registered models |
| `SLOTSERVE_LOAD_TIMEOUT_SECONDS` | `1800` | Give up if a model is not ready after this long |
| `SLOTSERVE_STOP_TIMEOUT_SECONDS` | `20` | Grace period before llama-server is killed |
| `SLOTSERVE_DISK_READ_MBPS` | `150` | Used to estimate load time before a model's first load |
| `SLOTSERVE_SLOT_MINUTES` | `30` | Calendar granularity |
| `SLOTSERVE_MAX_RESERVATION_HOURS` | `8` | Longest single booking |
| `SLOTSERVE_MAX_ADVANCE_DAYS` | `30` | How far ahead people can book |
| `SLOTSERVE_PRELOAD_BUFFER_SECONDS` | `60` | Extra margin when preloading before a booking |
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
| `SLOTSERVE_METRICS_TOKEN` | *empty* | Bearer token for `/metrics`; empty disables it |

Admins can override most of these in **Admin → System settings** without a restart: registration, invite code, booking limits and quotas, default launch arguments, time zone, public URL, Hugging Face, Telegram and metrics settings. Overrides live in the database and win over `.env`; **Use default** removes an override.

## Per-model launch arguments

Each model has a free-form argument string that is appended to the command:

```text
<SLOTSERVE_LLAMA_SERVER_CMD> -m <file> --host 127.0.0.1 --port 8081 --alias <name> <your arguments>
```

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

## Admin command line

Most account tasks are done in **Admin → Users**. The command line is for when no admin can sign in:

```bash
docker compose exec slotserve slotserve reset-password <username>
docker compose exec slotserve slotserve set-admin <username>
docker compose exec slotserve slotserve set-admin <username> --revoke
```
