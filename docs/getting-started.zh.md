# 快速开始

SlotServe 以一个 Docker 镜像发布，**不内置** llama.cpp：你把已经为自己 GPU 编译好的 `llama-server` 挂载进容器即可，因此适用于任何 CUDA 架构和 llama.cpp 版本。

## 环境要求

- 带 NVIDIA GPU 和驱动的 Linux 主机
- [Docker](https://docs.docker.com/engine/install/) 及 Compose v2
- [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
- 启用 CUDA 编译的 llama.cpp（`llama-server`），或 vLLM / SGLang，见[多 GPU 与推理后端](gpus-and-engines.md)

!!! tip "CUDA 版本保持一致"
    SlotServe 的 `CUDA_IMAGE` 的 CUDA 主版本必须与编译 `llama-server` 时使用的一致，并且该 CUDA 版本要支持你的 GPU。见[配置参考](configuration.md)。

## 1. 编译 llama.cpp（已有可跳过）

```bash
git clone https://github.com/ggml-org/llama.cpp
cd llama.cpp
cmake -B build -DGGML_CUDA=ON
cmake --build build --config Release -j --target llama-server
ls build/bin/llama-server
```

需要挂载进容器的是包含 `llama-server`（动态库编译时还有 `lib*.so`）的目录，通常是 `llama.cpp/build/bin`。

## 2. 配置

```bash
git clone https://github.com/PelerYuan/slotserve
cd slotserve
cp .env.example .env
```

编辑 `.env`，重要的几项：

| 变量 | 示例 | 含义 |
| --- | --- | --- |
| `LLAMA_CPP_DIR` | `/opt/llama.cpp/build/bin` | 宿主机上 `llama-server` 所在目录 |
| `MODELS_DIR` | `/mnt/ssd/models` | 宿主机上存放 `.gguf` 的目录（建议 SSD） |
| `DATA_DIR` | `./data` | 数据库与日志 |
| `PORT` | `8000` | 网页与接口端口 |
| `SLOTSERVE_INVITE_CODE` | *你的邀请码* | 注册时需要填写 |
| `SLOTSERVE_SECRET_KEY` | `openssl rand -hex 32` | 会话 Cookie 签名密钥 |

其余配置见[配置参考](configuration.md)。

## 3. 启动

如果 llama.cpp 目前以 systemd 服务运行，请先停掉，避免两个进程抢显存：

```bash
sudo systemctl disable --now llama-server.service   # 换成你的服务名
```

然后：

```bash
docker compose up -d --build
docker compose logs -f
```

!!! tip "预构建镜像"
    也可以不自己构建，直接使用发布的镜像：`docker compose pull && docker compose up -d`。镜像自带 CUDA 12.8 运行库，兼容用任意 CUDA 12.x 编译的 llama.cpp。如需固定版本，在 `.env` 中设置 `SLOTSERVE_IMAGE=ghcr.io/peleryuan/slotserve:<版本号>`（版本见 [Releases 页面](https://github.com/PelerYuan/slotserve/releases)）。


浏览器打开 `http://<服务器>:8000`。

## 4. 网页上的第一步

1. **注册。** 第一个注册的账号自动成为管理员。
2. **模型 → 扫描目录。** `MODELS_DIR` 中的 `.gguf` 文件会被登记（多模态投影文件和分片模型的非首片会被跳过）。
3. 按需**编辑启动参数**，例如 `-ngl 99 -c 16384 --flash-attn on`。
4. **设为默认模型**（管理员）。GPU 没有预约时会加载它。有多张 GPU 时，先在**管理 → GPU 与通道**中划分通道，每个通道各有默认模型。
5. **API 密钥 → 新建密钥**，然后调用接口：

```bash
curl http://<服务器>:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-ss-..." \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "你好"}]}'
```

## 升级

```bash
git pull
docker compose up -d --build
```

数据库表会自动创建；数据保存在 `DATA_DIR`。

## 不使用 Docker

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install ./backend
(cd frontend && npm ci && npm run build)      # 输出到 backend/slotserve/static

export SLOTSERVE_LLAMA_SERVER_CMD=/opt/llama.cpp/build/bin/llama-server
export SLOTSERVE_MODELS_DIR=/mnt/ssd/models
export SLOTSERVE_STATIC_DIR=$PWD/backend/slotserve/static
slotserve
```

SlotServe 自身的 systemd 服务示例：

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
