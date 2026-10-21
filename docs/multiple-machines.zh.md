# 多台机器

!!! note "预览"
    多机支持是新功能，仍在完善中：模型服务、下载、模型扫描、[模型目录](model-catalog.md)的一键部署和[模型库](storage.md)已可在每台机器上使用；**开发环境（含无 GPU 环境及其端口）目前仍只在服务器本机上运行**。其他机器上的通道不会占用服务器的 CPU 核心预留。

一个 SlotServe 服务器可以使用多台机器的 GPU（或 CPU）。大家仍然使用同一个网址、同一个 API 密钥和同一个预约日历；每台机器只是增加了自己的[通道](gpus-and-engines.md)。

| 角色 | 运行内容 | 数量 |
| --- | --- | --- |
| **服务器**（控制平面） | 网页、API、数据库、预约与调度、OpenAI 兼容网关、通知、用量统计 | 一个 |
| **节点** | 一个轻量的*节点代理*：在该机器上启动和停止模型服务，上报 GPU、CPU、内存和模型文件，发送实时指标和日志，并把模型下载到自己的模型目录 | 每台额外机器一个 |

服务器本身也是一个节点（**内置节点**），所以只有一台机器时无需本页的任何设置，行为与以前完全相同。

```mermaid
flowchart LR
    people(["用户与 API 客户端"]) --> server["SlotServe 服务器<br/>（内置节点）"]
    agentB["节点代理 · gpu-b"] -- "WebSocket（主动连出，令牌）" --> server
    agentC["节点代理 · gpu-c"] -- "WebSocket（主动连出，令牌）" --> server
    server -- "模型请求（HTTP）" --> msB["gpu-b 上的模型服务"]
    server -- "模型请求（HTTP）" --> msC["gpu-c 上的模型服务"]
    agentB --- msB
    agentC --- msC
```

## 工作方式

- 每个节点代理使用自己的令牌，通过 WebSocket **主动连接**服务器，因此控制连接不需要在节点上开放端口。各种命令（启动或停止模型服务、读取日志、扫描模型、下载文件）都通过这条连接传递。
- **数据流量**直接发往节点：网关把 API 请求转发到该通道模型服务的 `http://<节点地址>:<端口>`。因此服务器必须能访问节点上的模型服务端口（该节点上的 `SLOTSERVE_LLAMA_PORT` + 通道 ID − 1）。请把这些端口放在可信网络中，它们本身没有鉴权。
- **一个通道只属于一个节点**，不会跨机器。它的 GPU 编号以该机器上看到的为准。
- **模型按节点区分。** 每个节点扫描自己的模型目录；只有当通道所在的节点有某个模型的文件时，才能在该通道上预约这个模型或把它设为默认模型。使用共享存储（例如在每台机器的同一路径挂载 NFS）时，同一个模型就会在所有机器上可用。
- 服务器重启或连接中断时，模型服务会继续运行。代理重新连接后会上报正在运行的内容；服务器直接接管，不会重启任何东西，并会停止在此期间被删除或迁移的通道的模型服务。

## 添加机器

1. 在服务器上打开**管理 → 节点 → 添加节点**，为机器起个名字（例如 `gpu-b`）。**地址**留空表示使用代理连接时的来源地址；如果中间有 NAT 或代理，请填写服务器访问该机器时使用的主机名或 IP。
2. 复制显示的令牌和命令。**令牌只显示一次。**随时可以用**新令牌**重新生成（旧令牌立即失效）。
3. 在新机器上启动代理。使用 Docker（与服务器相同的镜像）：

    ```bash
    docker run -d --name slotserve-agent --restart unless-stopped \
      --gpus all --network host \
      -v /path/to/llama.cpp:/opt/llama.cpp:ro \
      -v /path/to/models:/models \
      -v slotserve-agent:/data \
      -e SLOTSERVE_AGENT_SERVER=http://slotserve-server:8000 \
      -e SLOTSERVE_AGENT_TOKEN=ssn-... \
      ghcr.io/peleryuan/slotserve:latest slotserve agent
    ```

    或者使用 Compose：把 `docker-compose.node.yml` 和包含 `SLOTSERVE_AGENT_SERVER`、`SLOTSERVE_AGENT_TOKEN`、`LLAMA_CPP_DIR`、`MODELS_DIR` 的 `.env` 复制到该机器，然后运行 `docker compose -f docker-compose.node.yml up -d`。

    不使用 Docker 时，在该机器上安装 SlotServe 并运行 `slotserve agent --server http://slotserve-server:8000 --token ssn-...`。

4. 几秒钟内节点会变为**在线**，并显示其 GPU、CPU 核数、内存、模型目录和版本。
5. 在**管理 → GPU 与通道**中添加通道，选择这台机器及其 GPU。
6. 点击**扫描模型**（在模型页面，或在“管理 → 节点”中针对单个节点）登记该节点上已有的模型文件；也可以直接把模型下载到节点：有多台机器时，模型页面会询问**下载到**哪台机器。

没有 NVIDIA GPU 的机器请去掉 `--gpus all`；它的通道没有 GPU，使用推理后端的 CPU 版本。`--network host` 让服务器可以通过节点自身的地址访问模型服务。不使用它时，请发布模型服务端口（`-p 8081-8090:8081-8090`）并相应地设置节点的**地址**。

## 节点离线时

- 在 `SLOTSERVE_NODE_OFFLINE_SECONDS`（30）秒内没有任何消息的节点会被视为**离线**：它的通道显示为离线，网关不再把请求转发到这些通道（发往你在该机器上预约的通道的请求会得到 `503`），也不会在其上启动任何东西。
- 如果持续离线，会通知管理员，以及在其通道上有正在进行或 24 小时内开始的预约的用户。节点恢复后会再次通知管理员。
- 代理会自动重连（间隔逐渐增加），服务器会核对实际运行的内容。
- **吊销**会断开节点并拒绝其令牌；代理会停止其模型服务。**新令牌**可以让它重新接入。没有通道的已吊销节点可以删除。

## 版本

服务器和代理之间使用带版本号的协议。版本不兼容的代理会被拒绝，双方都会给出明确的提示（在“管理 → 节点”中显示为**版本不匹配**）；请在所有机器上运行相同版本的 SlotServe。

## 保护连接

节点代理的连接会传输节点令牌和所有命令，因此在可信网络之外请通过 TLS 运行：

- 通过 **https** 提供 SlotServe —— 置于 TLS 反向代理之后（设置 `SLOTSERVE_PUBLIC_URL=https://...`），或直接使用
  `SLOTSERVE_SSL_CERTFILE` / `SLOTSERVE_SSL_KEYFILE` —— 并给节点代理使用 `https://` 服务器地址。代理随后通过 `wss://`
  连接并校验服务器证书。
- 公共 CA 签发的证书无需其他设置。**私有 CA**：用 `--ca-bundle`（`SLOTSERVE_AGENT_CA_BUNDLE`）把 CA 证书交给代理。
  **自签名**证书：固定它的指纹。**管理 → 节点**会显示服务器证书的 SHA-256 指纹（从证书文件读取，或从公开地址上提供的证书读取），
  用 `--fingerprint`（`SLOTSERVE_AGENT_SERVER_FINGERPRINT`）传给代理。代理在发送令牌之前校验指纹，指纹不符时拒绝连接。
  证书续期后记得更新固定的指纹。
- 使用 `http://` 服务器地址时，代理会在日志中警告，**管理 → 节点**也会提示连接未加密。

令牌：

- 服务器只保存每个节点令牌的 SHA-256；令牌本身只显示一次。
- **新令牌**用于轮换令牌。在线的代理会通过连接收到新令牌，保存在其数据目录中（`agent-token.json`，只要配置中的令牌没有被修改，
  就会使用它代替配置的令牌）并继续运行 —— 无需在该机器上做任何修改。离线节点会得到新令牌，需要填入其配置。
- 失败的代理登录（未知或已轮换的令牌）按来源地址限制：15 分钟内超过 `SLOTSERVE_NODE_LOGIN_ATTEMPTS`（20）次后，
  该地址会被暂时拒绝，即使令牌有效。
- 添加、重命名、轮换令牌、吊销和删除节点都会记录在**管理 → 审计日志**中。

## 节点上的下载

- 每台机器有**自己的下载队列**：每台机器同时下载一个文件，不同机器并行下载。离线或繁忙的节点不会阻塞其他机器的下载；
  **管理 → 节点**会显示每个节点正在进行和排队中的下载。
- 节点在下载过程中离线时，下载会回到它的队列，节点恢复后从中断处**继续**（`.part` 文件保留在节点上）。连接中断期间已完成的下载不会重新下载；
  这期间被取消的下载会在节点重新连接时在节点上取消。
- **模型目录**：部署到其他机器上的通道时，文件会下载到该节点的模型目录，模型登记在该节点上。变体是否放得下，按通道所在机器的 GPU
  （以其代理上报的为准）判断，并会先检查节点上报的剩余空间。该节点必须至少连接过一次。
- **模型库**（管理 → 存储，用途为*模型库*）：**同时用于节点**可以让其他机器使用这个模型库。**主机目录**必须在这些机器上的相同路径下存在。
  **NFS / SMB** 共享由各节点的代理按服务器相同的方式挂载：代理在其机器上创建 Docker 卷，并保持一个很小的占位容器
  （`SLOTSERVE_STORAGE_HELPER_IMAGE`）运行以维持挂载，然后把卷的目录用于下载和模型服务。为此代理需要 Docker 套接字；
  代理运行在容器中时，必须能在相同路径下看到这些卷：
  `-v /var/run/docker.sock:/var/run/docker.sock -v /var/lib/docker/volumes:/var/lib/docker/volumes:rslave`。
  共享的凭据会通过连接发送给代理（这也是使用 TLS 的另一个原因）。节点无法挂载的模型库会在“管理 → 节点”中显示错误。
  带 `storage_id` 下载到未开放该模型库的节点会被拒绝（`storage_remote_node`）。

## 时钟、版本与停止代理

- **时钟**：代理在每次心跳中发送其时间；**管理 → 节点**显示每个节点的时钟偏差，超过 `SLOTSERVE_NODE_CLOCK_SKEW_WARN_SECONDS`（10）
  秒时给出警告。用量采样和加载事件始终按服务器时钟记录（事件按到达时间记录），节点上报的时间会按其偏差校正，因此错误的时钟不会扭曲统计。
  但仍应在每台机器上保持 NTP 运行。
- **版本**：比服务器旧的代理会在“管理 → 节点”中标记为**更新节点代理**，并给出确切命令（Compose、`docker run`、pip）；代理日志中也有同样的提示。
  协议版本不兼容的代理会被拒绝，消息中包含更新命令。
- **重连**：代理会以指数退避自动重连（`SLOTSERVE_AGENT_RETRY_SECONDS` 1 秒起，每次加倍，最多 `SLOTSERVE_AGENT_MAX_RETRY_SECONDS` 60 秒），
  并带有随机抖动，避免服务器重启后大量节点同时重连。
- **停止**：收到 `SIGTERM` / Ctrl+C 时代理会正常退出。`SLOTSERVE_AGENT_ON_EXIT=stop`（默认）时会停止其模型服务；`keep`（`--on-exit keep`）
  时会让已就绪的模型服务继续运行，之后在该机器上启动的代理会直接接管它们，无需重新加载模型 —— 适合更新不使用 Docker 安装的代理。
  在容器中运行时，模型服务总是随容器一起结束。

## 监控

- `GET /api/health`（使用指标令牌作为 `Authorization: Bearer`，或管理员会话）返回一份汇总：`ok`、`degraded`（有通道的节点离线、
  代理过旧、时钟偏差、通道失败、下载失败）或 `down`（没有任何通道可以提供服务），并列出问题、节点、通道和下载。
  `?strict=true` 在不是 `ok` 时返回 `503`，便于简单的可用性检查。
- Prometheus：GPU 指标和采样的 GPU 计数器带有 `node` 标签（GPU 编号按机器区分）；每个节点有 `slotserve_node_up` 和
  `slotserve_node_clock_skew_seconds`；`slotserve_lane_occupant` 带有通道所在的节点。
- **用量**：有多台机器时，用量页面会增加按机器的表格（预约和使用的 GPU 时、利用率、被占用时间的比例、按类型的占用）；通道和 GPU
  会标出所在机器；CSV 导出中 *compute* 带有 `node` 列，并有按机器的导出（`kind=nodes`）。

## 设置

服务器：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SLOTSERVE_LOCAL_NODE_NAME` | `local` | 首次启动时内置节点的名称（可在“管理 → 节点”中改名） |
| `SLOTSERVE_NODE_HEARTBEAT_SECONDS` | `5` | 代理上报指标的间隔（代理连接时下发） |
| `SLOTSERVE_NODE_OFFLINE_SECONDS` | `30` | 超过这么久没有消息即视为离线 |
| `SLOTSERVE_NODE_RPC_TIMEOUT_SECONDS` | `30` | 服务器等待代理回应命令的时间 |
| `SLOTSERVE_NODE_LOGIN_ATTEMPTS` | `20` | 15 分钟内每个地址允许的代理登录失败次数，超过后暂时拒绝 |
| `SLOTSERVE_NODE_CLOCK_SKEW_WARN_SECONDS` | `10` | 时钟偏差超过该值时“管理 → 节点”给出警告 |
| `SLOTSERVE_SSL_CERTFILE`、`SLOTSERVE_SSL_KEYFILE` | *空* | 直接提供 HTTPS；会显示证书指纹以便固定 |

代理（此外，`SLOTSERVE_LLAMA_SERVER_CMD`、`SLOTSERVE_LLAMA_PORT`、`SLOTSERVE_MODELS_DIR`、`SLOTSERVE_DATA_DIR` 等常规模型服务设置作用于该机器）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SLOTSERVE_AGENT_SERVER` | *必填* | 服务器地址，例如 `http://slotserve-server:8000`（`--server`） |
| `SLOTSERVE_AGENT_TOKEN` | *必填* | “管理 → 节点”中生成的节点令牌（`--token`） |
| `SLOTSERVE_AGENT_ADDRESS` | *空* | 服务器访问这台机器时使用的地址（`--address`）；在“管理 → 节点”中设置的地址优先 |
| `SLOTSERVE_AGENT_MODEL_HOST` | `0.0.0.0` | 节点上模型服务监听的网卡地址 |
| `SLOTSERVE_AGENT_CA_BUNDLE` | *空* | 连接 `https://` 服务器时信任的 CA 证书（`--ca-bundle`） |
| `SLOTSERVE_AGENT_SERVER_FINGERPRINT` | *空* | 固定的服务器证书 SHA-256 指纹（`--fingerprint`） |
| `SLOTSERVE_AGENT_ON_EXIT` | `stop` | 代理停止时 `stop`（停止）模型服务，或 `keep`（保留）给下一个代理接管（`--on-exit`） |
| `SLOTSERVE_AGENT_RETRY_SECONDS`、`SLOTSERVE_AGENT_MAX_RETRY_SECONDS` | `1`、`60` | 重连退避（逐次加倍，带抖动） |

Hugging Face 下载使用服务器的 `SLOTSERVE_HF_TOKEN`；只有从 `SLOTSERVE_HF_ENDPOINT` 下载时才会把它传给节点。
