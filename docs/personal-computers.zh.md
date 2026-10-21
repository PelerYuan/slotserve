# 个人电脑

小型实验室或工科宿舍里，常常有几台公共机器，再加上每个人自己的电脑——带 GPU 的游戏本和台式机，很多是 Windows。
SlotServe 可以把它们汇集起来：每台个人电脑作为一个[节点](multiple-machines.md)加入，并有一位**主人**：主人决定什么时候共享、
随时可以收回，并能看到别人在上面运行什么。控制平面可以是任何一直开着的设备：NAS、旧电脑或树莓派。

节点只会**主动连出**到服务器，所以个人电脑不需要开放任何端口。

## 添加一台个人电脑

1. 管理员打开 **管理 → 节点 → 添加节点**，给机器起名（例如 `alice-desktop`），复制令牌旁显示的**一行命令安装**。
   令牌只显示一次；**新令牌**可以重新签发。
2. 管理员设置机器的**主人**（管理 → 节点 → 编辑）。主人随后可以在 **我的机器** 里找到它。
3. 在这台电脑上运行对应系统的安装命令（见下文）。几秒钟内机器会显示为**在线**；管理 → 节点里还会显示它是哪种机器
   （例如 *Windows · WSL2 (Docker Desktop)*）。
4. 在 **管理 → GPU 与通道** 中为这台机器添加通道并选择 GPU。

安装脚本由你的 SlotServe 服务器在 `/install/node.sh` 和 `/install/node.ps1` 提供，已填好服务器地址和令牌。
再次运行同一行命令会替换节点代理（例如换了新令牌之后）。除非设置了 `SLOTSERVE_MODELS_DIR` / `SLOTSERVE_LLAMA_CPP_DIR`，
两个脚本都把模型目录和 llama.cpp 放在 `~/slotserve`（Windows：`%USERPROFILE%\slotserve`）下。

!!! warning "llama.cpp 必须是 Linux 版本"
    在所有系统上，节点代理都运行在 Linux 容器里。请把 **Linux** 版的 llama.cpp（`llama-server` 及其库；NVIDIA GPU 用 CUDA 版）
    放进 llama.cpp 目录——Windows 上也一样，Windows 的 `.exe` 无法使用。没有它，机器可以加入，但无法运行模型。
    vLLM / SGLang 与其他节点一样，需要相应的镜像或命令。

=== "Linux"

    ```bash
    curl -fsSL 'https://slotserve.example/install/node.sh?token=ssn-...' | bash
    ```

    脚本会检查 Docker 是否运行；如果机器有 NVIDIA GPU，还会检查是否安装了
    [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)，
    然后以 `--restart unless-stopped`、`--gpus all` 和主机网络启动 `slotserve-agent` 容器（这样代理能看到真实网卡，
    用于网络唤醒，并能唤醒邻近的机器）。设置 `SLOTSERVE_NO_GPU=1` 可作为纯 CPU 节点加入。

=== "Windows"

    要求：Windows 10 22H2 或 11，较新的 NVIDIA 驱动（有 GPU 时），以及启用了 **基于 WSL 2 的引擎** 和 **Linux 容器** 的
    [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/)。在 PowerShell 中运行：

    ```powershell
    irm 'https://slotserve.example/install/node.ps1?token=ssn-...' | iex
    ```

    脚本会检查 Docker Desktop、WSL 2 引擎以及容器能否使用 GPU（`docker run --gpus all ... nvidia-smi`），
    找到正在使用的物理网卡（其 MAC 地址和子网，用于网络唤醒，并检查是否开启了 *Wake on Magic Packet*），
    然后以自动重启方式启动节点代理。

    - 打开 **登录时启动 Docker Desktop**（Docker Desktop → Settings → General），这样重启后代理会自动回来。
      Docker Desktop 在用户登录后才启动，所以被网络唤醒的电脑需要自动登录，否则在有人登录之前它会一直离线。
    - 放在 Windows 目录里的模型要经过 WSL 的文件共享读取，比 Linux 磁盘慢，大模型加载会更慢。
      把 `SLOTSERVE_MODELS_DIR` 指向 WSL 发行版内的目录（`\\wsl$\...`）会更快。
    - Windows 的*快速启动*可能导致关机后网卡无法唤醒电脑；如果关机状态下网络唤醒无效，请关闭它
      （控制面板 → 电源选项 → 选择电源按钮的功能）。

=== "macOS"

    ```bash
    curl -fsSL 'https://slotserve.example/install/node.sh?token=ssn-...' | bash
    ```

    Mac 上的 Docker Desktop 无法把 Apple GPU 传给容器，因此 Mac 作为**纯 CPU 节点**加入：它的通道没有 GPU，
    运行 CPU 版 llama.cpp（Apple 芯片上用 Linux arm64 版本）。改为原生运行节点代理（`pip install` SlotServe，
    然后 `slotserve agent --server ... --token ...`）可以让 Metal 版 llama.cpp 使用 GPU，但 SlotServe 仍会显示该通道没有 GPU。

不使用脚本时，令牌旁显示的 `docker run` 命令和 `docker-compose.node.yml` 仍然可用（见[多台机器](multiple-machines.md)）。

## 主人的控制

主人（以及管理员）在 **我的机器** 或 **管理 → 节点 → 共享** 中管理机器。

### 共享时段

**一直共享**（默认）、**按时段共享** 或 **不共享**（只有主人使用）。时段是按服务器时区（管理 → 系统设置中的 `timezone`）
设置的每周时间窗口；结束早于开始的时段会延续到第二天。**夜间和周末** 会填入工作日 23:00–09:00 和整个周末。

- 他人的**预约**必须完全落在共享时段内，否则会被拒绝并给出明确提示（延长预约和候补也一样）。主人可以随时预约自己的机器。
  预约日历会用斜线标出某通道所在机器不共享的时间，预约对话框也会在提交前提醒。
- 共享时段之外，这台机器的通道**不会自己运行任何东西**：不加载默认模型，也不运行共享开发环境。已有的预约照常运行
  （它们是在机器共享时预约的）。
- 如果新的时段让他人已有的预约落在共享时段之外，这些预约**仍然有效**，但会通知相关的人改期，并给主人发一份汇总
  （保存后也会立即显示）。

### “我要用电脑”

**暂停共享** 1–8 小时，或直到手动恢复：

1. 立即通知所有在这台机器上有正在运行或在暂停期间开始的预约的人。
2. 在宽限期内（`sharing_grace_minutes`，默认 10 分钟）一切照常运行，方便大家保存工作。
3. 宽限期结束后，他人正在运行的预约会被结束。他们保留的开发环境会**暂停而不是删除**，与任何预约结束时一样。
   之后在暂停结束前，通道不再运行任何东西（主人自己的预约除外），其他人也不能预约暂停时段。

**恢复共享** 会立即结束暂停。

### 我的机器上在运行什么

列出机器的通道（在运行哪个模型、为什么）、正在运行或未来 24 小时内开始的预约，以及开发环境。
**停止** 会结束正在运行的预约（或取消即将开始的预约）并通知对方；**停止到下一个预约** 会卸载通道的默认模型或共享环境。

## 网络唤醒（Wake-on-LAN）

处于睡眠或关机状态的机器，可以在其上的预约开始前不久被唤醒（`wol_lead_minutes`，默认 5 分钟；0 表示关闭），
也可以用 **立即唤醒** 按钮（主人、管理员，以及一小时内在这台机器上有预约的人可用）。由于控制平面可能在另一个网络里，
唤醒包由**同一局域网内一台在线机器**发送：另一个节点代理，或服务器本身。具有相同**网络标签**（在机器上设置，
或 `SLOTSERVE_AGENT_NETWORK`；Windows 脚本会使用电脑的子网，例如 `192.168.1.0/24`）或物理网卡子网相同的机器，视为同一网络。

目标机器的 **MAC 地址** 来源：代理能看到真实网卡时自动上报（使用主机网络的 Linux），由安装脚本传入（Windows、macOS、
`SLOTSERVE_AGENT_WOL_MAC`），或手动填写。如果预约开始时机器仍然离线，会通知预约人和主人。

被唤醒机器上的设置：

- **BIOS / UEFI**：开启 *Wake on LAN* / *Power On by PCI-E* / *Resume by LAN*（名称各异），并关闭会切断网卡供电的
  *ErP / EuP* 或 *深度睡眠* 模式。
- **Windows**：设备管理器 → 网卡 → *电源管理*：允许此设备唤醒计算机，*只允许幻数据包唤醒计算机*；
  *高级*：开启 *Wake on Magic Packet*。若要从关机状态唤醒，请关闭快速启动。
- **Linux**：`ethtool -s <网卡> wol g`（通过网络管理器使其永久生效，例如
  `nmcli connection modify <连接> 802-3-ethernet.wake-on-lan magic`）。
- **macOS**：系统设置 → 能耗 → *唤醒以供网络访问*。
- 使用**有线**连接；无线网卡很少能唤醒睡眠中的电脑。唤醒包是广播，无法跨越路由器：发送的机器必须在同一网段。

## 贡献统计

在线的个人机器每分钟统计一次：**在线**小时数、**共享**小时数（在线、处于共享时段且未暂停）以及**提供的 GPU 时**
（共享小时数 × 该机器各通道的 GPU 数）。结合用量采样，可以得到每个人：自己的机器提供了多少、**他人**在上面用了多少 GPU 时、
以及自己**使用**了多少 GPU 时（在任何机器上的预约）。**用量** 页面显示这张表（可导出 CSV），每台机器的数据在其共享面板中。
接口：`GET /api/usage/contribution`、`GET /api/usage/contribution/export`、`GET /api/nodes/{id}/contribution`。

## 设置

| 设置 | 默认值 | 说明 |
| --- | --- | --- |
| `sharing_grace_minutes`（`SLOTSERVE_SHARING_GRACE_MINUTES`） | `10` | 从“暂停共享”到停止他人任务之间的时间 |
| `wol_lead_minutes`（`SLOTSERVE_WOL_LEAD_MINUTES`） | `5` | 在预约开始前这么久唤醒离线机器；0 表示从不自动唤醒 |
| `SLOTSERVE_WOL_PORT` | `9` | 唤醒包的 UDP 端口 |

节点代理：`SLOTSERVE_AGENT_WOL_MAC`（MAC 地址，逗号分隔）、`SLOTSERVE_AGENT_NETWORK`（网络标签）、
`SLOTSERVE_AGENT_HOST_OS`（`windows` / `macos` / `linux`，由安装脚本设置）。

## 接口

| 方法与路径 | 谁可以用 | 作用 |
| --- | --- | --- |
| `GET /api/nodes/mine` | 所有人 | 你拥有的机器 |
| `GET`、`PUT /api/nodes/{id}/sharing` | 主人、管理员 | 共享时段（`schedule`：`null` 一直共享，`[]` 不共享，或时段列表）、`network`、`mac_addresses`；`PUT` 返回 `conflicts` |
| `POST /api/nodes/{id}/pause` `{hours}` / `POST .../resume` | 主人、管理员 | 暂停（`hours` 为 `null` 表示直到恢复）/ 恢复 |
| `GET /api/nodes/{id}/running`、`POST .../stop` `{reservation_id}` 或 `{lane_id}` | 主人、管理员 | 查看在运行什么；停止它 |
| `POST /api/nodes/{id}/wake` | 主人、管理员、预约人 | 通过同一网络中的机器发送唤醒包 |
| `GET /api/lanes/unavailable?start=&end=` | 所有人 | 每个通道所在机器不共享的时间 |

`GET /api/admin/nodes` 包含 `owner_id`、`owner_name`、`sharing_schedule`、`shared_now`、`sharing_paused`、
`sharing_paused_until`、`sharing_next_change`、`platform`、`host_os`、`network`、`wol_macs` 和 `woken_at`。
