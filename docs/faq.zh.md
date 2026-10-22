# 常见问题

**模型加载要好几分钟，能更快吗？**
加载速度取决于磁盘。把常用模型放到 SSD 上（让 `MODELS_DIR` 指向 SSD，或对单个文件做软链接）。Linux 会用空闲内存缓存最近读过的文件，内存大于模型时第二次加载会快很多。SlotServe 会在预约前预加载，尽量把等待藏起来。

**客户端收到 `503 model_unavailable`。**
模型正在加载或启动失败。`Retry-After` 头给出了大致的等待时间。如果一直失败，打开**模型 → 运行日志**查看推理进程的报错（常见原因是 `-c` 太大导致显存不足，或参数写错）。

**客户端收到 `409 model_not_loaded`。**
请求里写了另一个已登记的模型名。去掉 `model` 或改成当前模型，或者预约你想用的模型。

**多个人能同时用 GPU 吗？**
可以，在没人预约时：所有有效密钥都能并发使用默认模型（在启动参数里调大 `-np` 可增加并发 slot）。预约期间 GPU 是独占的。

**支持多张 GPU 或同时加载多个模型吗？**
支持。在**管理 → GPU 与通道**中把 GPU 分成多个通道，每个通道运行自己的模型、单独预约，因此可以同时加载多个模型。要让一个模型用多张卡，把这些卡放进同一个通道，并在参数里加上对应后端的多卡参数（llama.cpp 的 `--split-mode`/`--tensor-split`，vLLM 的 `--tensor-parallel-size`）。详见[多 GPU 与推理后端](gpus-and-engines.md)。

**能用 vLLM 或 SGLang 代替 llama.cpp 吗？**
可以，按模型选择：**模型 → 添加模型**，选推理后端即可。后端需要安装在 SlotServe 运行的环境里，`Dockerfile.vllm` 可以构建带 vLLM 的镜像。

**忘记密码了。**
由另一位管理员在**管理 → 用户**中重置，或在服务器上执行 `docker compose exec slotserve slotserve reset-password <用户名>`。见[忘记管理员密码或被锁定](operations.md#admin-password)。

**如何放在 HTTPS / 反向代理后面？**
把 8000 端口代理过去，转发原始 `Host` 头、开启 WebSocket、关闭缓冲；设置 `SLOTSERVE_PUBLIC_URL`、`SLOTSERVE_COOKIE_SECURE=true` 和 `SLOTSERVE_FORWARDED_ALLOW_IPS`；并给开发环境的网页端口（8001）一个单独的地址（`SLOTSERVE_DEVENV_WEB_URL`）。完整的 nginx 示例见[反向代理](operations.md#reverse-proxy)。

**在网页上修改时提示「不是从 SlotServe 页面发出的」。**
反向代理改写了 `Host` 头。请转发原始的 `Host`（`proxy_set_header Host $host;`），或把 `SLOTSERVE_PUBLIC_URL` 设为大家使用的地址；见[反向代理](operations.md#reverse-proxy)。

**如何备份？**
SlotServe 每天在 `DATA_DIR/backups/` 写出一个备份包（数据库、自动生成的会话密钥、SSH 主机密钥）。设置**备份复制到**，让另一台机器上也有一份；workspace 和模型请在它们所在的地方另行备份。见[备份](operations.md#backups)和[从备份恢复](operations.md#restore)。

**`ssh` 提示 REMOTE HOST IDENTIFICATION HAS CHANGED。**
升级到 v0.10 之后，每个环境出现一次是正常的：现在每个环境都有自己的主机密钥。把指纹与环境卡片上的**主机密钥**对比，然后执行 `ssh-keygen -R "[服务器]:端口"`。见[从 v0.8 升级](operations.md#upgrade-after)。

**开发环境里 `sudo` 用不了。**
环境以 no new privileges 运行；你本来就是 `root`，去掉 `sudo` 即可。确实需要它的镜像可以用兼容模式运行；见[容器加固](operations.md#hardening)。

**模型加载不起来，怎么看原因？**
打开**管理 → 运行监控**。这里能看到加载阶段和进度、推理进程的实时输出；失败时会显示错误、退出码、最后几行输出、实际执行的命令，对于 SlotServe 能识别的错误还会给出诊断（见下文）。以往每次加载也都保留了这些信息。

**推理进程的日志在哪？**
第一个通道是 `DATA_DIR/logs/llama-server.log`，其他通道是 `llama-server-lane<N>.log`（超过 20 MB 会轮转），网页上也能查看。各种后端的日志文件名相同。

## 诊断 {#diagnosis}

模型加载、开发环境、存储或健康检查失败时，SlotServe 会把错误和最后几行日志与已知的失败做比对。能识别的失败会附带**诊断**：可能的原因、下一步怎么做，以及匹配到的日志行，显示在运行监控、加载历史和健康表（管理 → 概览）中。这里列出同样的原因和步骤，你也可以在本页搜索错误文本。

无法识别的失败会保留退出码和日志末尾：错误通常在最后几行。求助时请使用卡片上的**复制诊断信息**。

### 端口已被其他程序占用 {#port-in-use}

典型日志: `couldn't bind HTTP server socket`, `address already in use`, `port is already allocated`

- 找到并停掉占用该端口的程序（常见：手动启动的 llama-server / vLLM，或旧的服务）。Linux：ss -ltnp；Windows：netstat -ano
- 或把 SlotServe 的端口挪开：模型服务用 SLOTSERVE_LLAMA_PORT（每个通道依次使用后面的端口），开发环境用 SSH 与直连端口范围；改完重启 SlotServe
- 端口空出后重试加载

### 磁盘已满 {#disk-full}

典型日志: `No space left on device`, `[Errno 28]`

- 清理日志中提到的磁盘：模型目录、数据目录或 Docker 数据目录（docker system df；docker image prune）
- 删除不再需要的模型或已保存的环境镜像
- 空间足够后重试

### 上下文长度放不进剩余给 KV 缓存的显存 {#context-too-long}

典型日志: `max seq len (...) is larger than the maximum number of tokens that can be stored in KV cache`

- 调小 --max-model-len
- 或在 GPU 无其他占用时调大 --gpu-memory-utilization（最高约 0.95）
- 或使用更多 GPU（--tensor-parallel-size）、更小或量化程度更高的模型

### 加载模型时 GPU 显存不足 {#gpu-out-of-memory}

典型日志: `cudaMalloc failed: out of memory`, `CUDA out of memory`, `torch.OutOfMemoryError`

- 调小上下文长度（llama.cpp 的 -c，vLLM 的 --max-model-len）：KV 缓存随之增长
- 少放几层到 GPU（llama.cpp 的 -ngl），或换用量化程度更高的模型
- 确认没有其他程序占用 GPU（nvidia-smi）：另一个模型、开发环境或任务

### 机器内存（RAM）不足，进程被系统终止 {#host-out-of-memory}

典型日志: `Killed`, `Out of memory: Killed process`, `std::bad_alloc`

- 查看可用内存（free -h）和其他正在运行的程序；开发环境和其他模型也占用内存
- 换用更小的模型或量化版本；llama.cpp 用 -ngl 放更多层到 GPU，且不要加 --no-mmap
- 开发环境：设置内存上限，避免一个容器占满全部内存

### Docker 无法为容器分配 GPU：未安装或未配置 NVIDIA Container Toolkit {#gpu-runtime-missing}

典型日志: `could not select device driver "nvidia" with capabilities: [[gpu]]`, `nvidia-container-cli: initialization error`

- 在 Docker 主机上安装 NVIDIA Container Toolkit，执行 sudo nvidia-ctk runtime configure --runtime=docker 并重启 Docker
- 确认宿主机驱动正常（nvidia-smi），并测试：docker run --rm --gpus all ubuntu nvidia-smi
- Windows 上的 Docker Desktop：使用 WSL 2 后端并安装较新的 NVIDIA 驱动

### 引擎的 CUDA 版本与 GPU 驱动或 GPU 不匹配 {#cuda-mismatch}

典型日志: `CUDA driver version is insufficient for CUDA runtime version`, `no kernel image is available for execution on the device`

- 升级宿主机 NVIDIA 驱动，或改用针对较低 CUDA 版本构建的引擎
- 使用 Docker 时：CUDA_IMAGE 的 CUDA 主版本必须与构建 llama-server 时一致
- 很新或很老的 GPU 需要包含其架构的构建（CMAKE_CUDA_ARCHITECTURES）

### 引擎无法启动：缺少所需的库（DLL 或 .so） {#engine-library-missing}

典型日志: `error while loading shared libraries: libcudart.so.12: cannot open shared object file`

- 通常是 CUDA 运行库与引擎构建不匹配：安装对应的 CUDA 运行库，或换用与已安装 CUDA 对应的引擎构建
- Windows：把 llama.cpp 发布包里的 cudart DLL 放到 llama-server.exe 同一目录
- Linux：ldd $(which llama-server) 可以看到缺少哪些库

### 引擎程序无法运行（没有执行权限或为其他系统构建） {#engine-not-executable}

典型日志: `spawn failed: [Errno 13] Permission denied`, `exec format error`

- 加上执行权限：chmod +x <llama-server 路径>
- 确认构建与本机匹配（x86_64 或 arm64，Linux 或 Windows）
- 以 noexec 挂载的目录不能运行程序：把引擎移到别处，或去掉 noexec 重新挂载

### 找不到引擎程序 {#engine-not-found}

典型日志: `spawn failed: [Errno 2] No such file or directory`, `command not found`

- 设置引擎的完整路径：llama.cpp 用 SLOTSERVE_LLAMA_SERVER_CMD，vLLM 用 SLOTSERVE_VLLM_CMD；改完重启 SlotServe
- 使用 Docker 时：选择包含该引擎的镜像（vLLM 模型需要 vLLM 镜像）
- 确认模型使用的是已安装的引擎（模型 → 引擎）

### SlotServe 无法连接 Docker {#docker-unreachable}

典型日志: `Cannot connect to the Docker daemon at unix:///var/run/docker.sock`, `permission denied while trying to connect to the Docker daemon socket`

- 确认宿主机上 Docker 正在运行（docker info）
- Docker 部署时把 socket 挂进 SlotServe（-v /var/run/docker.sock:/var/run/docker.sock）；非 Docker 部署时运行 SlotServe 的用户需要有权限访问它（docker 组）
- 如果 Docker 监听在别处（例如 Podman 的 socket 或 tcp://），检查 SLOTSERVE_DOCKER_HOST

### Docker 镜像拉取失败 {#image-pull-failed}

典型日志: `pull access denied for ...`, `manifest unknown`, `toomanyrequests`

- 检查镜像名称和 tag（拼写错误、tag 不存在）
- 确认 Docker 主机能访问镜像仓库（代理、防火墙，或在 /etc/docker/daemon.json 配置镜像加速）
- 私有镜像或触发 Docker Hub 拉取限额：在宿主机 docker login，或先手动 docker pull 一次

### Docker 主机无法挂载该 NFS/SMB 共享：缺少挂载工具 {#mount-helper-missing}

典型日志: `you might need a /sbin/mount.<type> helper program`, `wrong fs type, bad option, bad superblock`

- 在 Docker 主机上（不是容器里）安装挂载工具：NFS 用 nfs-common（Debian/Ubuntu）或 nfs-utils（RHEL），SMB 用 cifs-utils
- 然后在存储上重新点击「检查」
- Docker Desktop：挂载发生在其虚拟机内，请改用宿主机目录类型的存储

### 存储服务器拒绝挂载（凭据错误或未授权） {#mount-access-denied}

典型日志: `mount.nfs: access denied by server while mounting ...`, `mount error(13): Permission denied`

- SMB：检查存储设置中的用户名、密码和域
- NFS：确认导出允许本 Docker 主机的 IP（服务器上的 /etc/exports，然后 exportfs -ra）
- 然后在存储上重新点击「检查」

### NFS/SMB 共享挂载失败：服务器不可达或共享不存在 {#mount-failed}

典型日志: `mount.nfs: Connection timed out`, `failed to mount local volume`

- 检查存储设置中的服务器地址和共享路径
- 确认 Docker 主机能访问服务器（ping；NFS 端口 2049、SMB 端口 445 已放行）
- 然后在存储上重新点击「检查」

### Hugging Face 拒绝访问该模型 {#hf-auth}

典型日志: `Access to model ... is restricted`, `401 Client Error: Unauthorized for url: https://huggingface.co/...`

- 受限模型：先用你的账号在该模型的 Hugging Face 页面同意许可
- 设置 Hugging Face 访问令牌（SLOTSERVE_HF_TOKEN，读权限）后重试
- 检查仓库名称（私有或改名的仓库同样返回 401/404）

### 没有读取模型文件的权限 {#models-permission-denied}

典型日志: `failed to open GGUF file '...' (Permission denied)`, `[Errno 13] Permission denied: '...'`

- 给运行 SlotServe 和引擎的用户模型目录的读权限：chmod -R a+rX <模型目录>
- Docker 中需容器用户可读；NFS/SMB 上检查 uid/gid 映射与挂载选项（uid=、file_mode=）
- 启用 SELinux 的主机：挂载目录时加 :z，或查看审计日志

### 模型文件已损坏或不完整 {#model-corrupt}

典型日志: `invalid magic characters`, `failed to read tensor info`, `data is not within the file bounds`

- 重新下载模型：常见原因是下载中断（与来源比对大小或 SHA-256）
- 分卷模型（…-00001-of-00003.gguf）：所有分卷都要在，加载第一卷
- 确认文件确实是 llama.cpp 用的 GGUF 模型（不是改了扩展名的 safetensors）

### 当前引擎版本不支持该模型的格式或架构 {#model-unsupported}

典型日志: `unknown model architecture: '...'`, `Model architectures ['...'] are not supported`

- 升级引擎：新的模型架构需要较新的 llama.cpp 或 vLLM 版本
- 或换一个版本的模型（其他量化，或其他发布者的 GGUF）
- llama.cpp 加载 GGUF 文件；vLLM 加载 Hugging Face 模型目录（safetensors）

### 模型文件不存在或无法打开 {#model-file-missing}

典型日志: `failed to open GGUF file '...'`, `does not appear to have a file named config.json`

- 确认文件仍然存在（可能已被移动、改名或删除），并重新扫描模型目录
- 模型在 NFS/SMB 上时：确认共享已挂载到本机
- vLLM：目录中需要有 config.json 和权重文件

### 模型没有在规定时间内加载完成 {#load-timeout}

典型日志: `model did not become ready within ...s`

- 大模型放在慢盘或网络存储上加载更慢：调大 SLOTSERVE_LOAD_TIMEOUT_SECONDS
- 查看日志末尾：没有进展的卡住通常另有错误
- 把模型复制到本地 SSD 以加快加载

### 所需的文件或目录不存在 {#file-not-found}

典型日志: `No such file or directory`, `The system cannot find the file specified`

- 检查日志中的路径：启动参数拼写错误、模型文件被移动，或目录未挂载
- Docker 内的路径与宿主机不同：使用容器内看到的路径
- 修正路径后重试
