# Getting started

SlotServe ships as one Docker image. It does **not** bundle llama.cpp: you mount the `llama-server` you already built for your GPU, so SlotServe works with any CUDA architecture and llama.cpp version.

## Requirements

- Linux host with an NVIDIA GPU and driver
- [Docker](https://docs.docker.com/engine/install/) with Compose v2
- [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
- A CUDA build of llama.cpp (`llama-server`)

!!! warning "Volta (V100) and CUDA 13"
    CUDA 13 dropped support for Volta (`sm_70`). On a V100, build llama.cpp with CUDA 12.x and keep SlotServe's `CUDA_IMAGE` on 12.x as well.

## 1. Build llama.cpp (skip if you already have it)

```bash
git clone https://github.com/ggml-org/llama.cpp
cd llama.cpp
cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=70   # 70 = V100; use your GPU's value
cmake --build build --config Release -j --target llama-server
ls build/bin/llama-server
```

The directory that contains `llama-server` (and its `lib*.so` files for shared builds) is what you mount into the container - usually `llama.cpp/build/bin`.

## 2. Configure

```bash
git clone https://github.com/PelerYuan/slotserve
cd slotserve
cp .env.example .env
```

Edit `.env`. The important values:

| Variable | Example | Meaning |
| --- | --- | --- |
| `LLAMA_CPP_DIR` | `/opt/llama.cpp/build/bin` | Host directory with `llama-server` |
| `MODELS_DIR` | `/mnt/ssd/models` | Host directory with `.gguf` files (prefer an SSD) |
| `DATA_DIR` | `./data` | Database and logs |
| `PORT` | `8000` | Web UI and API port |
| `SLOTSERVE_INVITE_CODE` | *your secret* | Needed to sign up |
| `SLOTSERVE_SECRET_KEY` | `openssl rand -hex 32` | Signs session cookies |

See [Configuration](configuration.md) for everything else.

## 3. Start

If llama.cpp currently runs as a systemd service, stop it first so the two do not compete for GPU memory:

```bash
sudo systemctl disable --now llama-server.service   # use your unit's name
```

Then:

```bash
docker compose up -d --build
docker compose logs -f
```

Open `http://<server>:8000`.

## 4. First steps in the UI

1. **Sign up.** The first account becomes the administrator.
2. **Models → Scan folder.** Every `.gguf` in `MODELS_DIR` is registered (multimodal projector files and the non-first parts of split models are skipped).
3. **Edit launch arguments** for each model if needed, e.g. `-ngl 99 -c 16384 --flash-attn on`.
4. **Set a default model** (admin). It is loaded whenever the GPU is not booked.
5. **API keys → New key**, then call the API:

```bash
curl http://<server>:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-ss-..." \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "Hello"}]}'
```

## Updating

```bash
git pull
docker compose up -d --build
```

The database schema is created automatically; your data lives in `DATA_DIR`.

## Running without Docker

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
