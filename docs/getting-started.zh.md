# 快速开始

SlotServe 以一个 Docker 镜像发布，**不内置** llama.cpp：你把已经为自己 GPU 编译好的 `llama-server` 挂载进容器即可，因此适用于任何 CUDA 架构和 llama.cpp 版本。

在命令行上安装只需四步。之后你只要在网页上**跟着设置清单走**：功能真正可用时它会自动打勾，并告诉你下一步做什么。

## 环境要求 {#requirements}

- 带 NVIDIA GPU 和驱动的 Linux 主机
- [Docker](https://docs.docker.com/engine/install/) 及 Compose v2
- [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
- 启用 CUDA 编译的 llama.cpp（`llama-server`），或 vLLM / SGLang，见[多 GPU 与推理后端](gpus-and-engines.md)

!!! tip "CUDA 版本保持一致"
    SlotServe 的 `CUDA_IMAGE` 的 CUDA 主版本必须与编译 `llama-server` 时使用的一致，并且该 CUDA 版本要支持你的 GPU。见[配置参考](configuration.md)。

## 1. 编译 llama.cpp（已有可跳过） {#1-build-llamacpp-skip-if-you-already-have-it}

```bash
git clone https://github.com/ggml-org/llama.cpp
cd llama.cpp
cmake -B build -DGGML_CUDA=ON
cmake --build build --config Release -j --target llama-server
ls build/bin/llama-server
```

需要挂载进容器的是包含 `llama-server`（动态库编译时还有 `lib*.so`）的目录，通常是 `llama.cpp/build/bin`。

## 2. 配置 {#2-configure}

```bash
git clone https://github.com/PelerYuan/slotserve
cd slotserve
cp .env.example .env
```

编辑 `.env`，重要的几项：

| 变量 | 示例 | 含义 |
| --- | --- | --- |
| `LLAMA_CPP_DIR` | `/opt/llama.cpp/build/bin` | 宿主机上 `llama-server` 所在目录 |
| `MODELS_DIR` | `/mnt/ssd/models` | 宿主机上存放 `.gguf` 的目录（建议 SSD）；容器必须能读取 |
| `DATA_DIR` | `./data` | 数据库、日志、备份和自动生成的会话密钥 |
| `PORT` | `8000` | 网页与接口端口 |
| `SLOTSERVE_INVITE_CODE` | *你的邀请码* | 注册时需要填写（未设置前只有第一个管理员能注册）；邀请链接不需要它 |
| `SLOTSERVE_SECRET_KEY` | *留空* | 保持留空：SlotServe 会自动生成并保存在 `DATA_DIR/secret_key`（见[会话密钥](operations.md#secret-key)） |

如果放在反向代理之后，还要设置 `SLOTSERVE_PUBLIC_URL` 并阅读[反向代理](operations.md#reverse-proxy)。其余配置见[配置参考](configuration.md)。

## 3. 启动 {#3-start}

如果 llama.cpp 目前以 systemd 服务运行，请先停掉，避免两个进程抢显存：

```bash
sudo systemctl disable --now llama-server.service   # 换成你的服务名
```

然后：

```bash
docker compose pull          # 使用发布的镜像；或：docker compose up -d --build
docker compose up -d
docker compose logs -f
```

!!! tip "预构建镜像还是自己构建"
    发布的镜像自带 CUDA 12.8 运行库，兼容用任意 CUDA 12.x 编译的 llama.cpp。`docker-compose.yml` 跟随次版本号（`ghcr.io/peleryuan/slotserve:0.10`），所以 `docker compose pull` 会带来修复，但不会升级到新的次版本。如果你的 llama.cpp 需要其他 CUDA 主版本，请在本地构建（`docker compose up -d --build`）。

浏览器打开 `http://<服务器>:8000`。如果其他机器也要访问，请按[端口与防火墙](operations.md#firewall)开放端口。

## 4. 创建管理员账号 {#first-admin}

第一个账号需要一个一次性的**初始化码**，在还没有管理员时会打印在日志中：

```bash
docker compose logs slotserve | grep -A4 "No admin account"
docker compose exec slotserve slotserve setup-code          # 或直接获取
```

在注册表单中填写初始化码，该账号即成为管理员，初始化码随即失效。详见[初始化码与第一个管理员](operations.md#setup-code)。

## 5. 跟着设置清单走 {#setup-checklist}

登录后，**总览**最上方是**完成 SlotServe 的设置**，**管理 → 概览**中是完整的**设置进度**。每一项在功能真正可用时自动打勾（而不是填完表单就算），未完成的项也不影响 SlotServe 的使用：

| 项目 | 何时打勾 | 怎么做 |
| --- | --- | --- |
| **推理引擎** | 找到了 `llama-server`（或你的推理引擎） | 检查 `.env` 中的 `LLAMA_CPP_DIR`；vLLM 见[多 GPU 与推理后端](gpus-and-engines.md#engines)。 |
| **GPU** | `nvidia-smi` 能看到你的 GPU | 需要 NVIDIA Container Toolkit；看不到时见[GPU 不见了](operations.md#gpu-gone)。有多张 GPU 时，在**管理 → GPU 与通道**中决定如何划分通道。 |
| **模型与空闲默认模型** | 已登记模型，并且每个通道都有默认模型 | **模型 → 扫描目录**会登记 `MODELS_DIR` 中的每个 `.gguf`（或从[模型目录](model-catalog.md)一键部署），然后**设为默认**。GPU 没有被预约时，默认模型供所有人使用。 |
| **邀请成员** | 设置了邀请码（如果你只发邀请链接，也可以忽略这一项） | **生成邀请链接**：会过期、统计注册人数、可以撤销，并附带可直接转发的群消息（管理 → 用户）。也可以在管理 → 系统设置中设置**邀请码**。 |
| *可选：* **开发环境** | Docker 可以访问，并且开发环境已开启 | 挂载 Docker 套接字并开启：见[开发环境](dev-environments.md#setting-it-up-admin)。 |
| *可选：* **聊天通知** | 已连接一个聊天平台 | 见[通知与集成](integrations.md)。不接聊天平台时，所有消息也都在收件箱里。 |
| *可选：* **备份异地保存** | 已有备份复制到本机以外 | 设置**备份复制到**：见[备份](operations.md#backup-target)。 |

清单下面的**系统健康**表列出 SlotServe 运行的所有检查，出问题时附带操作按钮和诊断；每一项的含义见[健康检查](operations.md#health)。

## 6. 试试这三件事 {#three-paths}

每个人第一次登录时，总览上都有**从这里开始**，给出同样的三条路径。邀请大家之前，先自己试一遍。

### 在 Playground 中调用大模型 {#call-a-model}

在总览上点**在这里试试**（或在**模型**页面打开一个模型）。**Playground** 在登录状态下直接与正在运行的模型对话，无需 API 密钥；旁边给出同一个请求的 Python、cURL 和 JavaScript 写法。**复制代码在本地调用**会按需创建一个名为 `playground` 的密钥（有效期 90 天）并填进代码：

```bash
curl http://<服务器>:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-ss-..." \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "你好"}]}'
```

任何 OpenAI 兼容客户端都可以使用 `http://<服务器>:8000/v1` 和**访问凭据**中的密钥。模型旁的标记会说明它**现在可直接调用**、**预约后可用**，还是正被某人使用。

### 在浏览器里打开 VS Code {#open-vs-code}

需要先开启开发环境（见上面的清单项）。在**开发环境**页面：选择 **VS Code（浏览器）**，**要 GPU 吗？** 选**先不用**，点**打开**。大约一分钟后 VS Code 会在新标签页中打开你的 `/workspace`，关掉之后文件也会保留。不需要 SSH 密钥，也不需要预约；环境运行在没有被 GPU 通道预留的 CPU 核心上。详见[开发环境](dev-environments.md#first-environment)。

### 预约 GPU {#book-a-gpu}

在总览上点**预约 GPU**（或**预约时段**、**预约 → 新建预约**），会打开一个简短的对话框：做什么（一个模型、带 GPU 的开发环境，或整块 GPU 给你自己的进程）、什么时候（**现在 · 1 小时**、到某个时间，或之后的某个时间），SlotServe 会自动挑选一块空闲的 GPU。无法预约时，对话框会说明原因（配额、谁在用）并给出下一个空闲时间。预约期间 GPU 由你独占；详见[使用指南](user-guide.md)。

## 升级 {#updating}

```bash
git pull
docker compose pull && docker compose up -d     # 或：docker compose up -d --build
```

修改数据库之前，SlotServe 会在 `DATA_DIR/backups/` 中保存一份快照，并以「全部成功或全部不做」的方式升级。请先阅读该版本的不兼容变更；从 v0.8 升级到 v0.10 请按[从 v0.8 升级](operations.md#upgrade)操作。

## 不使用 Docker {#running-without-docker}

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

此时管理命令（`slotserve setup-code`、`reset-password`、`doctor`、`restore`）直接运行即可，使用相同的用户和相同的环境变量。
