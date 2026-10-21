# 开发环境

除了运行模型和整卡，预约还可以是**开发环境**：预约期间，SlotServe 会运行一个带该通道 GPU 和 SSH 服务的 Docker 容器，你用 SSH 公钥登录。`/workspace` 里的文件在多次预约之间一直保留。

```mermaid
sequenceDiagram
    participant U as 你
    participant S as SlotServe
    participant D as Docker
    U->>S: 预约开发环境（模板、时间）
    Note over S,D: 开始前最多一小时
    S->>D: 下载镜像
    Note over S,D: 预约开始时
    S->>S: 停止该通道的推理进程
    S->>D: 创建容器（通道的 GPU、/workspace、只读的 /models）
    S->>D: 放入 SSH 服务和你的公钥，启动
    S-->>U: ssh -p 2201 root@服务器（网页和 Telegram）
    U->>D: SSH、VS Code Remote-SSH、端口转发
    Note over S,D: 预约结束时
    S->>D: 停止并删除容器（/workspace 保留）
    S->>S: 重新加载该通道的默认模型
```

## 开启（管理员）

1. 把 Docker 套接字挂载进 SlotServe：在 `docker-compose.yml` 里取消注释

    ```yaml
    - /var/run/docker.sock:/var/run/docker.sock
    ```

    然后执行 `docker compose up -d`。容器通过 [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) 使用 GPU，SlotServe 本身已经依赖它。

2. 打开**管理 → 系统设置 → 开发环境 → 开发环境**。

3. 在宿主机防火墙中放行 **SSH 端口范围**（默认 `2200-2299`），每个运行中的环境占用其中一个端口。

4. 按需设置 **SSH 主机名**（用户在 `root@` 后面输入的地址）、CPU、内存、共享内存上限，以及允许的镜像仓库。

!!! warning "安全"
    能控制 Docker 套接字就能控制宿主机。SlotServe 启动的容器不加额外特权，只挂载该通道的 GPU、用户自己的 `/workspace` 卷和只读的模型目录；但 SlotServe 的管理员从此可以在宿主机上运行容器。只在用户可信的服务器上开启开发环境。

    **Podman** 提供与 Docker 兼容、不需要 root 守护进程的接口。把 `SLOTSERVE_DOCKER_HOST` 指向它的套接字（如 `unix:///run/podman/podman.sock`）即可改用 Podman，GPU 需要按 Podman 的 CDI 方式配置。

## 使用

1. 打开**开发环境**，点**添加 SSH 公钥**，粘贴 `~/.ssh/id_ed25519.pub` 的内容（没有的话用 `ssh-keygen -t ed25519` 生成）。只接受公钥登录。
2. 点**预约开发环境**（或在预约弹窗中选「开发环境」），选择模板和时间。
3. 镜像会提前下载，页面上能看到进度。预约开始后，页面会显示连接命令：

    ```bash
    ssh -p 2201 root@服务器
    ```

    在容器里你是 `root`。文件请放在 `/workspace`；共享模型在 `/models`（只读）。

4. **VS Code**：把页面上的配置片段加到 `~/.ssh/config`，用 Remote-SSH 连接。每次预约的端口可能不同。
5. **Jupyter 等网页工具**：自己启动后用端口转发访问，如 `ssh -p 2201 -L 8888:localhost:8888 root@服务器`。

预约结束时容器会被删除。需要更久的话请及时延长（结束前 10 分钟的提醒里有延长按钮）。

### 模板

| 模板 | 镜像 |
| --- | --- |
| PyTorch | `pytorch/pytorch:2.14.1-cuda12.6-cudnn9-devel` |
| Jupyter + PyTorch | `quay.io/jupyter/pytorch-notebook:cuda12-pytorch-2.11.0` |
| TensorFlow | `tensorflow/tensorflow:2.21.0-gpu-jupyter` |
| CUDA | `nvidia/cuda:12.9.2-cudnn-devel-ubuntu24.04` |

内置模板使用 CUDA 12 版本，兼容的显卡和驱动范围更广。管理员可以更换镜像、停用模板或添加共享模板。

每个人都可以用任意镜像新建**自己的模板**（除非管理员关闭了自定义镜像或限制了镜像仓库），可以指定主进程和环境变量。镜像需要包含 `/bin/sh`；SSH 不依赖镜像，SlotServe 会放入自带的小型静态 SSH 服务。

### 保存环境

**保存为镜像**会把运行中的容器（装好的软件、`/workspace` 以外的文件）保存成你自己的新模板，下次预约直接从这里继续。`/workspace` 本来就会保留，不会打进镜像。每人可保存的镜像数量有上限（默认 3 个）；删除已保存的模板会同时删除服务器上的镜像。

## 管理员能看到什么

- 在**开发环境**页面打开**所有人**，可以看到每个环境的状态、镜像、端口和错误；**日志**显示容器输出。
- 启动失败的环境（镜像不存在、容器退出、没有空闲端口）会显示原因，可以点**重试**重新启动，预约人也会收到通知。
- SlotServe 重启后会保留仍在运行的环境，并清理残留的容器。
