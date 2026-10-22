# Getting started

SlotServe ships as one Docker image. It does **not** bundle llama.cpp: you mount the `llama-server` you already built for your GPU, so SlotServe works with any CUDA architecture and llama.cpp version.

Installing takes four steps on the command line. After that you only **follow the setup checklist** in the web UI: it ticks itself as things start working and says what is next.

## Requirements {#requirements}

- Linux host with an NVIDIA GPU and driver
- [Docker](https://docs.docker.com/engine/install/) with Compose v2
- [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
- A CUDA build of llama.cpp (`llama-server`), or vLLM / SGLang - see [GPUs, lanes & engines](gpus-and-engines.md)

!!! tip "Match the CUDA version"
    The CUDA major version of SlotServe's `CUDA_IMAGE` must match the one your `llama-server` was built with, and that CUDA version must support your GPU. See [Configuration](configuration.md).

## 1. Build llama.cpp (skip if you already have it) {#1-build-llamacpp-skip-if-you-already-have-it}

```bash
git clone https://github.com/ggml-org/llama.cpp
cd llama.cpp
cmake -B build -DGGML_CUDA=ON
cmake --build build --config Release -j --target llama-server
ls build/bin/llama-server
```

The directory that contains `llama-server` (and its `lib*.so` files for shared builds) is what you mount into the container - usually `llama.cpp/build/bin`.

## 2. Configure {#2-configure}

```bash
git clone https://github.com/PelerYuan/slotserve
cd slotserve
cp .env.example .env
```

Edit `.env`. The important values:

| Variable | Example | Meaning |
| --- | --- | --- |
| `LLAMA_CPP_DIR` | `/opt/llama.cpp/build/bin` | Host directory with `llama-server` |
| `MODELS_DIR` | `/mnt/ssd/models` | Host directory with `.gguf` files (prefer an SSD); must be readable by the container |
| `DATA_DIR` | `./data` | Database, logs, backups and the generated secret key |
| `PORT` | `8000` | Web UI and API port |
| `SLOTSERVE_INVITE_CODE` | *your secret* | Needed to sign up (until it is set, only the first admin can sign up); invite links work without it |
| `SLOTSERVE_SECRET_KEY` | *empty* | Leave it empty: SlotServe generates one and keeps it in `DATA_DIR/secret_key` (see [The secret key](operations.md#secret-key)) |

Behind a reverse proxy also set `SLOTSERVE_PUBLIC_URL` and read [Reverse proxy](operations.md#reverse-proxy). See [Configuration](configuration.md) for everything else.

## 3. Start {#3-start}

If llama.cpp currently runs as a systemd service, stop it first so the two do not compete for GPU memory:

```bash
sudo systemctl disable --now llama-server.service   # use your unit's name
```

Then:

```bash
docker compose pull          # the published image; or: docker compose up -d --build
docker compose up -d
docker compose logs -f
```

!!! tip "Prebuilt image or your own build"
    The published image uses CUDA 12.8 runtime libraries, which work with llama.cpp builds for any CUDA 12.x. `docker-compose.yml` follows a minor version (`ghcr.io/peleryuan/slotserve:0.10`), so `docker compose pull` brings fixes but never a new minor. Build locally (`docker compose up -d --build`) when your llama.cpp needs another CUDA major.

Open `http://<server>:8000`. If people should reach it from other machines, open the ports in [Ports and firewall](operations.md#firewall).

## 4. Create the admin account {#first-admin}

The first account needs a one-time **setup code**, printed in the log while no admin exists:

```bash
docker compose logs slotserve | grep -A4 "No admin account"
docker compose exec slotserve slotserve setup-code          # or ask for it directly
```

On the sign-up form enter the setup code; that account becomes the admin, and the code stops working. See [First admin and setup code](operations.md#setup-code).

## 5. Follow the setup checklist {#setup-checklist}

After signing in, the **Overview** starts with **Finish setting up SlotServe**, and **Admin → Overview** shows the full **Setup progress**. Each item ticks itself when the thing really works (not when a form was filled in), and nothing in it blocks SlotServe from being used:

| Item | Ticks when | How |
| --- | --- | --- |
| **Inference engine** | `llama-server` (or your engine) is found | Check `LLAMA_CPP_DIR` in `.env`; for vLLM see [GPUs, lanes & engines](gpus-and-engines.md#engines). |
| **GPUs** | `nvidia-smi` sees your GPUs | The NVIDIA Container Toolkit; [GPUs gone](operations.md#gpu-gone) if not. With several GPUs, decide how to split them into lanes in **Admin → GPUs & lanes**. |
| **A model and an idle default** | A model is registered and every lane has a default model | **Models → Scan folder** registers every `.gguf` in `MODELS_DIR` (or deploy one from the [Model catalog](model-catalog.md)), then **Set as default**. The default model serves everyone while the GPU is not booked. |
| **Invite people** | The invite code is set (or you dismiss the item because you only hand out invite links) | **Create invite link**: it expires, counts its sign-ups, can be revoked and comes with a ready-to-post message (Admin → Users). Or set **Invite code** in Admin → System settings. |
| *Optional:* **Dev environments** | Docker is reachable and dev environments are on | Mount the Docker socket and switch them on: [Dev environments](dev-environments.md#setting-it-up-admin). |
| *Optional:* **Chat notifications** | A chat platform is connected | [Notifications & integrations](integrations.md). Without one, everything still lands in the inbox. |
| *Optional:* **Backups kept elsewhere** | A backup was copied off the machine | Set **Copy backups to**: [Backups](operations.md#backup-target). |

Below the checklist, the **System health** table lists every check SlotServe runs, with buttons and a diagnosis when something fails; [Health checks](operations.md#health) explains each.

## 6. Three things to try {#three-paths}

Everyone who signs in for the first time sees **Start here** on the Overview with the same three paths. Try them yourself before you invite people.

### Call a model in the Playground {#call-a-model}

Press **Try it here** on the Overview (or open a model on **Models**). The **Playground** chats with a model that is already running, signed in, without an API key; next to it is the same request as Python, cURL and JavaScript. **Copy code to run locally** creates a key named `playground` (valid 90 days) on request and fills it into the code:

```bash
curl http://<server>:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-ss-..." \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "Hello"}]}'
```

Any OpenAI-compatible client works with `http://<server>:8000/v1` and a key from **Access credentials**. The badge next to a model says whether it is **callable now**, **available after booking** or **in use** by someone.

### Open VS Code in the browser {#open-vs-code}

Needs dev environments switched on (checklist item above). On **Dev environments**: choose **VS Code (browser)**, answer **Do you need a GPU?** with **Not now**, press **Open**. About a minute later VS Code opens in a new tab, on your `/workspace`, which is kept after you close it. No SSH key and no booking needed; the environment runs on the CPU cores no GPU lane reserves. See [Dev environments](dev-environments.md#first-environment).

### Book a GPU {#book-a-gpu}

**Book the GPU** on the Overview (or **Book a slot**, **Schedule → New booking**) opens a short dialog: what (a model, a dev environment with the GPU, or the whole GPU for your own processes), when (**Now · 1 h**, until a time, or a time later) - and SlotServe picks a free GPU. If it cannot be booked, the dialog says why (quota, who has it) and offers the next free time. During the booking the GPU is yours alone; see the [User guide](user-guide.md#booking-the-gpu).

## Updating {#updating}

```bash
git pull
docker compose pull && docker compose up -d     # or: docker compose up -d --build
```

Before it changes the database, SlotServe saves a snapshot in `DATA_DIR/backups/` and upgrades all or nothing. Read the release's breaking changes first; for v0.8 → v0.10 follow [Upgrade from v0.8](operations.md#upgrade).

## Running without Docker {#running-without-docker}

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install ./backend
(cd frontend && npm ci && npm run build)      # outputs into backend/slotserve/static

export SLOTSERVE_LLAMA_SERVER_CMD=/opt/llama.cpp/build/bin/llama-server
export SLOTSERVE_MODELS_DIR=/mnt/ssd/models
export SLOTSERVE_STATIC_DIR=$PWD/backend/slotserve/static
slotserve
```

A systemd unit for SlotServe itself:

```ini
[Unit]
Description=SlotServe
After=network-online.target

[Service]
WorkingDirectory=/opt/slotserve
EnvironmentFile=/opt/slotserve/.env
ExecStart=/opt/slotserve/.venv/bin/slotserve
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

The admin commands (`slotserve setup-code`, `reset-password`, `doctor`, `restore`) are then run directly, as the same user and with the same environment.
