# 运维手册

写给维护 SlotServe 的人：升级、备份与恢复、第一个管理员、反向代理、健康检查与告警、容器加固、防火墙端口，以及在真实硬件上验证安装。每篇问题手册都按**症状 → 判断 → 处理 → 预防**来写。

下面的命令假定你按[快速开始](getting-started.md)用 Docker Compose 安装，并在 `docker-compose.yml` 所在目录执行。不使用 Docker 时，去掉 `docker compose exec slotserve` 前缀，以运行 SlotServe 的用户、在相同的环境（`.env`）下直接执行 `slotserve ...`。

| 情况 | 看这里 |
| --- | --- |
| 从 v0.8 升级到 v0.10 | [从 v0.8 升级](#upgrade) |
| 升级出了问题 | [升级失败：回滚](#rollback) |
| 设置或检查备份 | [备份](#backups)、[会话密钥](#secret-key) |
| 从备份恢复、迁移到另一台机器 | [从备份恢复](#restore)、[迁移到新机器](#new-machine) |
| 没有人能以管理员身份登录 | [初始化码与第一个管理员](#setup-code)、[忘记管理员密码](#admin-password) |
| 放在 nginx、Caddy 或 Traefik 后面 | [反向代理](#reverse-proxy) |
| 管理 → 概览里有红色项 | [健康检查与 `slotserve doctor`](#health)、[收件箱与告警](#alerts) |
| 环境里 `sudo` 或某个应用不能用 | [容器加固与兼容模式](#hardening) |
| 需要开放哪些端口 | [端口与防火墙](#firewall) |
| 磁盘满、GPU 不见了、环境起不来、密码泄露 | [问题手册](#runbooks) |
| 让大家用之前先检查一遍 | [在真实硬件上验证](#verify) |

## 从 v0.8 升级 {#upgrade}

v0.10 改变了登录方式、开发环境的隔离方式和备份方式。大部分不需要你做什么；本节列出大家会注意到的变化和需要检查的地方。完整列表见 `CHANGELOG.md` 中的 **Breaking changes**。

### 升级之前 {#upgrade-before}

1. 阅读该版本的 **Breaking changes**。
2. 做一份找得到的备份：**管理 → 概览 → 备份 → 立即备份**，或者停止 SlotServe 后复制整个数据目录（`DATA_DIR`，默认 `./data`）。升级时还会自动做一份快照（见下文）。
3. 留意镜像标签。`docker-compose.yml` 现在跟随次版本号（`ghcr.io/peleryuan/slotserve:0.10`，会收到 0.10.x 修复），不再使用 `latest`。如果你在 `.env` 里设置了 `SLOTSERVE_IMAGE`，请改成新版本。
4. 提前告诉大家下面的「需要重新登录一次」和 SSH 主机密钥警告。SlotServe 重启期间，正在运行的开发环境会继续运行；新的容器设置在它们下次启动时生效。

### 升级 {#upgrade-steps}

```bash
git pull                      # 新的 docker-compose.yml 和文档
docker compose pull           # 或：docker compose up -d --build
docker compose up -d
docker compose logs -f slotserve
```

首次启动时，服务会：

- 在修改数据库之前保存一份**快照**：`data/backups/slotserve-v<旧版本>-to-v<新版本>-<时间>/`，内含数据库、`secret_key` 和开发环境的 SSH 主机密钥。快照永远不会被自动删除；
- **要么全部成功、要么全部不做**地执行编号的数据库迁移（失败会回滚，服务不会启动；见[回滚](#rollback)），并写出 `data/migration_report.json`；
- 如果 `SLOTSERVE_SECRET_KEY` 未设置或仍是 `change-me`，在 `data/secret_key` 生成一个随机密钥（见[会话密钥](#secret-key)）。

数据库还需要升级时，`reset-password` 和 `set-admin` 会拒绝执行：请先启动一次服务。

### 大家会注意到什么 {#upgrade-after}

**每个人都要重新登录一次。** 浏览器登录现在保存在服务端，旧的登录状态都会随升级失效。API 密钥照常可用。修改密码现在会让你的其他浏览器退出登录。

**SSH 会提示主机密钥变了，每个环境一次。** 现在每个开发环境都有自己的 SSH 主机密钥（以前所有环境共用一把，任何人在自己的容器里都能读到）。已有的环境在下次启动时获得自己的密钥，之后第一次 `ssh` 会以 `REMOTE HOST IDENTIFICATION HAS CHANGED` 中止。这只会发生一次，属于正常现象。请大家：

1. 把 `ssh` 打印的指纹与环境卡片上显示的**主机密钥**（开发环境页面）对比；
2. 删除旧记录后重新连接：

    ```bash
    ssh-keygen -R "[server]:2201"     # 环境 ssh 命令里的主机和端口
    ```

如果在这第一次**之后**又出现警告，而卡片上的主机密钥并没有变，那就是真正的警告，不要忽略。由旧版本创建的保留环境会在下次启动时先做快照、再重建一次（这次启动会慢一些），因为新的设置无法加到已有的容器上。

**空闲通道的共享环境需要「加入」。** 共享环境只接受 SSH 密钥，并且只有加入的人才有账号（开发环境 → **加入**）。升级时已有 SSH 公钥的人会被自动加入所有通道。

**容器做了加固。** 开发环境里 `sudo` 等 setuid 程序不再起作用，`ping` 不可用，fork 炸弹或内存耗尽的任务只会影响它自己的容器。内存上限设为 0 现在表示「宿主机内存减去 max(4 GB, 10%)」，而不是「不限」。没有指定 GPU 的通道不能再启动 GPU 环境：请在**管理 → GPU 与通道**中为通道指定 GPU。确实需要 `sudo` 或自带 init 系统的模板以兼容模式运行；见[容器加固](#hardening)。

**注册。** 已经有管理员的安装不会要求初始化码。邀请码仍为 `change-me` 时，任何人都无法注册：请设置邀请码或发放邀请链接（管理 → 用户）。

**在反向代理之后**，在代理转发 `Host` 头或设置 `SLOTSERVE_PUBLIC_URL` 之前，网页上的修改可能被拒绝（提示「不是从 SlotServe 页面发出的」）；登录限速需要设置 `SLOTSERVE_FORWARDED_ALLOW_IPS`。见[反向代理](#reverse-proxy)。

**防火墙。** 开发环境的网页界面（VS Code、JupyterLab、桌面）使用单独的端口，默认 8001。如果大家要用，请开放它；见[端口与防火墙](#firewall)。

### 检查升级结果 {#upgrade-check}

1. **管理 → 概览**：设置进度和系统健康表。每一项都应为正常，或是你理解的提示；见[健康检查](#health)。
2. `docker compose exec slotserve slotserve doctor` 在命令行给出同样的列表。
3. `data/migration_report.json` 列出每一步迁移；如果升级时有数据行无法保留，健康检查 **migration** 会给出警告。
4. 登录，在浏览器里打开一个开发环境，在 Playground 里调用一次模型。

### 升级失败：回滚 {#rollback}

**症状。** 升级后服务起不来，或者能运行但你依赖的某个功能坏了。

**判断。**

- `docker compose logs slotserve` 会说明原因。迁移失败时日志写着 `database upgrade failed and was rolled back; the snapshot is in ...`：数据库没有被改动，仍是旧版本的。
- 在升级过的数据库上启动旧版本时，它会拒绝启动并提示 `... was used by a newer SlotServe`。这是在保护你的数据，本身不是问题所在。

**处理。** 要重新运行之前的版本：

```bash
docker compose stop slotserve
ls -d data/backups/slotserve-v*-to-v*          # 最新的那个就是这次升级的快照
SNAP=data/backups/slotserve-v...-to-v...-<时间>   # ls 显示的目录名
cp "$SNAP/slotserve.db" data/slotserve.db
rm -f data/slotserve.db-wal data/slotserve.db-shm
[ -f "$SNAP/secret_key" ] && cp "$SNAP/secret_key" data/secret_key
[ -d "$SNAP/devenv" ] && cp -r "$SNAP/devenv" data/
```

然后在 `.env` 中固定旧版本并启动：

```bash
echo "SLOTSERVE_IMAGE=ghcr.io/peleryuan/slotserve:0.8.0" >> .env
docker compose up -d
```

升级之后到回滚之前发生的一切（预约、密钥、设置）会随新数据库一起丢失；如果可能还需要，覆盖前先把 `data/slotserve.db` 另存一份。如果是迁移本身失败（数据库没有被改动），只需固定旧版本即可。

**预防。** 先在数据目录的副本上演练升级（另一个目录作为 `DATA_DIR`、另一个 `PORT`、关闭开发环境）。镜像固定在一个次版本上，有计划地升级。迁移失败时，带上 `data/migration_report.json` 和日志反馈问题。

## 备份 {#backups}

### 备份里有什么 {#backup-contents}

每天在 `backup_hour`（默认为 SlotServe 时区的 3:00；`-1` 表示距上一次每 24 小时一次），SlotServe 写出一个**备份包**，即目录 `data/backups/slotserve-daily-<日期>-<时间>/`，内含：

| 文件 | 内容 |
| --- | --- |
| `slotserve.db` | 数据库的一致副本（SQLite 在线备份，服务运行中也安全） |
| `secret_key` | 自动生成的[会话密钥](#secret-key)；密钥来自 `SLOTSERVE_SECRET_KEY` 时没有此文件 |
| `devenv/` | 开发环境的 SSH 主机密钥 |
| `manifest.json` | 版本、数据库结构版本、密钥指纹（从不包含密钥本身）、大小、校验和、行数、存储与保留的环境，以及备份包**不包含**的内容 |

**管理 → 概览 → 备份**列出所有备份包，可以随时手动备份（**立即备份**，生成 `manual` 备份包），也可以下载数据库文件。最新的 `backup_keep` 份（默认 7）每日和手动备份会保留；升级快照和 `prerestore` 备份包永远不会被自动删除。`backup_keep` 为 0 时关闭自动备份。

**不在备份里**：`/workspace` 和其他保留目录、保留环境的容器、保存的环境镜像、模型，以及托管存储上的数据。请在它们所在的地方另行备份：

- **存储**（管理 → 存储：主机目录、NFS、SMB）：随那台服务器或那个目录的备份一起做。
- **Docker 卷**（没有设置存储也没有设置存储目录时），例如某个人的 `/workspace`：

    ```bash
    docker run --rm -v slotserve-workspace-u3:/v:ro -v "$PWD":/out busybox:1.37 \
      tar czf /out/workspace-u3.tgz -C /v .
    ```

- **模型**：通常可以重新下载，或连同模型目录一起复制。
- **保存的镜像**：对需要的镜像执行 `docker save`。

### 在别处保留一份 {#backup-target}

和数据在同一块盘上的备份扛不住这块盘坏掉。设置**备份复制到**（`backup_target`，管理 → 系统设置）后，每个每日备份包写好后都会复制到那里的 `slotserve-backups/` 目录中，同样保留最新的 `backup_keep` 份：

- **绝对路径**，SlotServe 能写入，通常是挂载的 NAS 共享。使用 Docker 时这是**容器内**的路径：先把共享挂载进容器，例如在 `docker-compose.yml` 的 `volumes` 下添加：

    ```yaml
    - /mnt/nas/slotserve:/backup-target
    ```

    再把 `backup_target` 设为 `/backup-target`。
- **`storage:<id>`**：**管理 → 存储**中的某个存储（列表中有它的 id）。SlotServe 通过一个短时运行的辅助容器写入，所以不需要把共享挂载进 SlotServe。

备份包复制成功一次后，设置进度中的**备份异地保存**会自动打勾。

### 备份失败时 {#backup-alerts}

健康检查 **backup** 和 **backup_age** 盯着备份，以下情况会通知管理员（先进收件箱，再发到聊天平台）：

- 最近一次备份失败（详情里有错误信息；**重试**会重新备份一次）；
- 复制到备份目标失败，或备份目标无效；
- 最新的备份超过 36 小时（服务停过，或备份任务卡住了）。

在设置备份目标之前，会一直显示「备份只保存在本机磁盘上」的警告（可以填写原因后忽略）。`/metrics` 提供 `slotserve_backup_age_seconds`，可供外部监控使用。

### 会话密钥 {#secret-key}

会话密钥（secret key）用于签名浏览器会话，并加密 SlotServe 保存的密文：存储的密码（SMB）以及 API 密钥的可查看副本（开启**允许用户再次查看 API 密钥**时）。API 密钥本身（只保存哈希）和用户密码（同样是哈希）不依赖它。

- `SLOTSERVE_SECRET_KEY` **未设置、为空或为 `change-me`** 时，SlotServe 在首次启动时生成随机密钥，保存在 `data/secret_key`（只有属主可读）。每个备份包都包含它。
- `SLOTSERVE_SECRET_KEY` **设置为其他值**时，使用该值，并且它**不会**写进备份：请和其他机密一起保管（例如密码管理器），与 `.env` 放在一起。

**需要备份什么**：备份包（复制到本机以外），再加上 `.env`，以及你自行设置的会话密钥。在 SlotServe 停止时复制整个数据目录，也是 SlotServe 自身状态的完整备份。

#### 会话密钥丢失或被更换 {#secret-key-lost}

**症状。** 重启或迁移后所有人都被退出登录；健康检查 **secrets** 提示「N 项已保存的密钥无法用当前的会话密钥解密」；SMB 存储挂载失败；API 密钥的可查看副本无法显示。

**判断。** 该检查的详情列出了无法解密的项目，`slotserve doctor` 也会显示。对比更换前后两个备份包 `manifest.json` 中的密钥指纹（`secret_key.fingerprint`）。

**处理。**

- 如果还有旧密钥，**把它放回去**：更换之前某个备份包里的 `secret_key` 文件（停止 SlotServe，复制到 `data/secret_key`，再启动），或者 `.env` 中 `SLOTSERVE_SECRET_KEY` 的旧值。显式设置的 `SLOTSERVE_SECRET_KEY` 总是优先于 `data/secret_key`：想用文件里的密钥，就从 `.env` 中删除它。
- **没有旧密钥**时，重新填写列出的密文：在**管理 → 存储**中重新输入每个 SMB 存储的密码；API 密钥的可查看副本无法恢复（密钥本身仍然可用，持有人可以新建）。大家重新登录。

**主动更换密钥**（密钥泄露时）：停止 SlotServe，设置新的 `SLOTSERVE_SECRET_KEY`（`openssl rand -hex 32`），或删除 `data/secret_key` 让它重新生成，启动后按上面的方法重新填写密文。所有人都会被退出登录。

**预防。** 把备份保存到本机以外；如果密钥是你自己设置的，把它保存在服务器以外的地方。

## 从备份恢复 {#restore}

`slotserve restore` 把一个备份包放回去。执行时服务必须是停止的。

```bash
docker compose stop slotserve
docker compose run --rm --no-deps slotserve slotserve restore slotserve-daily-20261020-030000
docker compose up -d
```

- 参数是 `data/backups/` 中备份包的目录名，或者命令能看到的路径（在容器里：先把 NAS 上的备份包放进 `DATA_DIR/backups/`）。
- 它会要求你输入 `restore` 确认（`--yes` 跳过）。它会拒绝来自更新版本 SlotServe 的备份，以及未通过 SQLite 完整性检查的备份。
- 替换任何东西之前，当前状态会先保存为 `slotserve-prerestore-<时间>` 备份包，所以恢复本身也可以用同样的方法撤销。
- 它放回数据库、会话密钥（`--keep-key` 保留当前密钥；这样备份里的密文就无法解密）和 SSH 主机密钥。来自旧版本的备份会在下次启动时升级（升级前同样会做快照）。命令会打印你必须处理的警告，例如 `.env` 中的 `SLOTSERVE_SECRET_KEY` 会覆盖恢复的密钥。

旧的单文件备份（`slotserve-daily-<时间>.db`）和升级快照也可以用同样的方法恢复。

### 核对报告 {#reconcile}

恢复的数据库描述的是备份那一刻的状态，而 Docker 和磁盘已经变了。下次启动时 SlotServe 会把两者做比较，结果显示在**管理 → 概览**的健康检查 **restore** 中（也写入 `data/restore_report.json`）：

| 发现 | 含义 | 怎么做 |
| --- | --- | --- |
| 环境缺失 | 数据库期望的容器在 Docker 中不存在（备份之后被删除，或换了新机器） | 对保留的环境来说，装好的软件已经没了；它的保留目录不受影响。结束或删除它，再从模板重新启动。 |
| 未知容器 | 数据库不认识的 SlotServe 容器（备份之后创建的） | 先保存其中重要的内容（`docker cp`，或让它的主人把文件拷出来），再用 `docker rm -f <名称>` 删除。 |
| 模型文件缺失 | 模型文件不在数据库记录的位置 | 把文件拷回来，或重新扫描模型目录并删除过期的记录。 |
| 存储 | 列出来供重新检查 | 在**管理 → 存储**中逐个点**检查**。 |

卷和端口不做比较。Docker 无法连接时，该检查会等待（「等待 Docker 可用后核对」），Docker 恢复后再执行。

**在管理员忽略 restore 检查之前，SlotServe 不会删除任何数据库不认识的东西**（不清理未知的容器、网络或卷）。阅读报告，逐条处理，然后填写原因**忽略**它；之后恢复正常清理。

### 迁移到新机器 {#new-machine}

1. 按[快速开始](getting-started.md)在新机器上安装**相同或更新**版本的 SlotServe，并复制 `.env`（如果你设置了 `SLOTSERVE_SECRET_KEY`，它就在里面）。先不要启动。
2. 把最新的备份包复制到新的 `DATA_DIR/backups/` 并恢复：

    ```bash
    docker compose run --rm --no-deps slotserve slotserve restore --yes slotserve-daily-20261020-030000
    ```

3. 复制备份包以外的内容：模型目录、存放 `/workspace` 与保留目录的 Docker 卷或存储目录、保存的镜像（`docker save` / `docker load`）。NAS 上的存储只需新机器能访问（NFS 导出允许它的地址；安装 `nfs-common` / `cifs-utils`）。
4. 启动（`docker compose up -d`），阅读[核对报告](#reconcile)：旧机器上的环境会显示为缺失。
5. 让大家使用新地址（`SLOTSERVE_PUBLIC_URL`、DNS）。地址变了，大家 `known_hosts` 中的记录也要更新；主机密钥本身随备份包一起迁移过来了。

## 初始化码与第一个管理员 {#setup-code}

新安装没有管理员。在出现第一个管理员之前，SlotServe 每次启动都会在日志里打印一个一次性的**初始化码**；注册表单会要求填写它（代替邀请码），该账号即成为管理员：

```bash
docker compose logs slotserve | grep -A4 "No admin account"
docker compose exec slotserve slotserve setup-code
```

一旦有了管理员，初始化码立即失效；已有管理员的安装永远不会要求它。邀请码仍为 `change-me` 时，其他人都无法注册：在管理 → 系统设置中设置**邀请码**，或在管理 → 用户中生成**邀请链接**（会过期、统计注册人数、可以撤销，也可以要求管理员批准；链接里从不包含邀请码）。

增加管理员：**管理 → 用户**，或 `docker compose exec slotserve slotserve set-admin <用户名>`（`--revoke` 取消）。至少保留两位管理员，以便互相帮忙。

## 反向代理 {#reverse-proxy}

为了 HTTPS 或更好记的地址，可以把 SlotServe 放在反向代理之后。有三点要注意。

**1. 地址：`Host` 或 `SLOTSERVE_PUBLIC_URL`。** 对来自浏览器的修改类请求（POST、PUT、PATCH、DELETE），SlotServe 要求 `Origin` 是它自己的地址：请求发往的主机（`Host` 或 `X-Forwarded-Host`），或 `SLOTSERVE_PUBLIC_URL` 的源。如果代理把 `Host` 改写成 `127.0.0.1:8000`，网页上的所有修改都会失败，提示「不是从 SlotServe 页面发出的」。请转发原始主机名，并把 `SLOTSERVE_PUBLIC_URL` 设为大家输入的地址（SlotServe 发出的链接也用它）：

```bash
SLOTSERVE_PUBLIC_URL=https://gpu.example.org
SLOTSERVE_COOKIE_SECURE=true                 # cookie 只通过 HTTPS 发送
```

**2. 客户端地址：`SLOTSERVE_FORWARDED_ALLOW_IPS`。** 登录限速按地址锁定账号（账号 + IP），审计日志记录地址，API 密钥会显示最近一次使用的地址。SlotServe 只相信 `SLOTSERVE_FORWARDED_ALLOW_IPS` 中列出的代理（默认 `127.0.0.1`）发来的 `X-Forwarded-For`。使用 Docker 时，代理的连接来自 Docker 网桥的地址（例如 `172.18.0.1`），而不是 `127.0.0.1`：登录一次，在**管理 → 审计日志**中查看这次登录的地址，如果那是代理的地址而不是你自己的，就把它填进去：

```bash
SLOTSERVE_FORWARDED_ALLOW_IPS=172.18.0.1     # 逗号分隔；只有当 8000 端口仅代理可访问时才用 "*"
```

不设置的话，所有人共用代理的地址，一个人的登录失败可能拖累其他人。

**3. 流式输出和第二个端口。** 关闭缓冲以便模型回答能流式输出，允许长时间的请求，并转发 WebSocket。开发环境的网页界面使用 8001 端口，需要一个单独的地址（它必须与 SlotServe 保持不同的源）：另一个 `server` 块或另一个主机名，并把 `SLOTSERVE_DEVENV_WEB_URL` 设为它。

```nginx
server {
    listen 443 ssl;
    server_name gpu.example.org;
    # ssl_certificate ...;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_buffering off;
        proxy_read_timeout 1h;
        client_max_body_size 0;
    }
}

server {
    listen 8443 ssl;                            # 或 443 上的另一个 server_name
    server_name gpu.example.org;
    # ssl_certificate ...;
    location / {
        proxy_pass http://127.0.0.1:8001;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_buffering off;
        proxy_read_timeout 1h;
    }
}
```

```bash
SLOTSERVE_DEVENV_WEB_URL=https://gpu.example.org:8443
```

OpenAI 兼容接口（`/v1`）也通过同一个代理访问，客户端使用 `https://gpu.example.org/v1`。开发环境的 SSH 和直连端口不经过代理（见[端口与防火墙](#firewall)）。

## 健康检查与 `slotserve doctor` {#health}

所有查看 SlotServe 健康状况的地方都来自同一组检查：

| 位置 | 内容 | 谁能看 |
| --- | --- | --- |
| **管理 → 概览** | 设置进度，然后是**系统健康**表：每项检查的详情、操作按钮、已识别错误的诊断，以及填写原因后忽略 | 管理员 |
| `GET /healthz` | 浅检查：进程能响应（Docker 的 `HEALTHCHECK` 用它） | 任何人 |
| `GET /api/health` | 所有检查的 JSON（`checks[]`，`status: ok / degraded / down`）；`?strict=true` 在有检查失败时返回 503，适合可用性监控 | 管理员会话或监控令牌（`Authorization: Bearer <SLOTSERVE_METRICS_TOKEN>`） |
| `GET /metrics` | Prometheus：`slotserve_check_status`、`slotserve_backup_age_seconds`，以及 GPU 和用量指标 | 监控令牌 |
| `slotserve doctor` | 在命令行运行同样的检查 | 服务器上的 shell |

```bash
docker compose exec slotserve slotserve doctor          # 服务运行时
docker compose run --rm --no-deps slotserve slotserve doctor   # 服务起不来时
slotserve doctor --json                                 # 机器可读，已去除密文
```

`doctor` 每项检查打印一行（`ok`、`info`、`WARN`、`ERROR`），并附上警告和错误的详情；有检查处于错误级别时退出码为 1。它在自己的进程里运行，所以与运行中服务内部状态有关的检查（通道、后台任务）只在网页和 `/api/health` 中显示。

| 检查项 | 检查什么 | 不正常时 |
| --- | --- | --- |
| database | 数据库可以写入 | 磁盘满或只读、文件权限 |
| disk | 数据和模型所在位置以及各存储的剩余空间 | [磁盘满](#disk-full) |
| engine | 推理引擎程序存在 | 修正 `LLAMA_CPP_DIR` / `SLOTSERVE_LLAMA_SERVER_CMD`；见[配置参考](configuration.md#llama-server) |
| lanes | 每个通道的推理进程 | 加载失败，附带诊断；见[常见问题：诊断](faq.md#diagnosis) |
| models | 至少一个模型，每个通道有空闲默认模型 | 添加模型、设置默认模型（管理 → GPU 与通道） |
| gpu | `nvidia-smi` 能看到 GPU | [GPU 不见了](#gpu-gone) |
| docker | Docker 可以访问（开启开发环境时） | [开发环境：Docker](dev-environments.md#docker) |
| storage | 每个存储都能挂载并写入 | [存储](storage.md#types)，以及该检查上的诊断 |
| chat | 聊天平台已连接 | 管理 → 聊天渠道 |
| backup | 最近一次备份及其异地副本 | [备份](#backups) |
| backup_age | 最新的备份不超过 36 小时 | [备份失败时](#backup-alerts) |
| restore | 恢复后等待核对 | [核对报告](#reconcile) |
| migration | 最近一次升级保留了所有数据行 | `data/migration_report.json` |
| tasks | 后台任务在运行并按时回报 | 重启 SlotServe；附上详情反馈问题 |
| secrets | 已保存的密文都能解密 | [会话密钥丢失](#secret-key-lost) |
| security | 管理员人数；开启开发环境意味着拥有宿主机 root 权限 | 读一遍即可的提示 |
| invite | 邀请码不是 `change-me` | 设置邀请码或使用邀请链接 |

有些结果只是提示而不是问题（有意不接聊天平台、没有 GPU 的机器）：填写原因后**忽略…**。被忽略的检查仍留在表中，但不再算作问题，原因记入审计日志；它的级别变化时会自动恢复显示。

## 收件箱与告警 {#alerts}

所有通知都先进入应用内的**收件箱**（页头的铃铛），再发到个人绑定的聊天平台。普通成员收到与自己工作相关的消息（审批结果、提醒、环境已就绪或启动失败）。管理员还会收到管理消息：待审批的预约、等待批准的注册、模型加载失败、存储不可用、敏感设置的修改，以及健康告警。

- 某项检查达到告警级别（错误；部分检查为警告，部分要持续一段时间）时，会给所有管理员发送**一次**告警，恢复时再发一次。不会重复发送，重启后也不会。被忽略的检查不会告警。
- 群机器人（飞书 / Lark、钉钉、企业微信）也可以接收管理告警：**管理 → 聊天渠道**。
- 即使没有接入任何聊天平台也不会漏消息：收件箱里都有。「管理」菜单项会显示未读的管理消息数。
- 外部监控可以轮询 `/api/health?strict=true` 或抓取 `/metrics`（`slotserve_check_status`）。

## 容器加固与兼容模式 {#hardening}

SlotServe 为开发环境启动的每个容器，都会经过一个固定的最后加固步骤：

- **no new privileges**：`sudo` 等 setuid 程序无法提升权限；
- 去掉 `NET_RAW`、`MKNOD`、`SYS_CHROOT`、`AUDIT_WRITE` 和 `SETFCAP` 能力（`ping` 不可用）；
- 进程数最多 `SLOTSERVE_DEVENV_PIDS_LIMIT`（默认 4096），fork 炸弹只影响它自己的容器；
- 内存上限（**内存上限**设置；为 0 时为宿主机内存减去 max(4 GB, 10%)），内存耗尽的任务会在自己的容器里被结束，而不会拖垮 SlotServe；
- 日志大小有上限，GPU 恰好是通道的 GPU；从不使用特权模式，从不使用宿主机的网络或进程命名空间，从不挂载 Docker 套接字。

大家在容器里是 `root`，所以 `apt install` 不需要 `sudo`。会有这些变化：

| 环境里的症状 | 原因 | 怎么做 |
| --- | --- | --- |
| `sudo: effective uid is not 0` 或 setuid 程序失败 | no new privileges | 直接以 root 运行，不用 `sudo`。对于必须使用 setuid 的镜像（自带 init 系统、切换用户），由管理员在该模板的设置中打开**兼容模式**（开发环境页面，仅管理员；内置桌面已经使用）。隔离较弱：只用于可信的镜像。 |
| `ping: permission denied` | 去掉了 `NET_RAW` | 用 `curl` 或 `nc` 测试连接。 |
| `fork: Resource temporarily unavailable` | 进程数上限 | 减少并行进程，或调高 `SLOTSERVE_DEVENV_PIDS_LIMIT`。 |
| 任务被 `Killed` | 内存上限 | 减小批量，或调高**内存上限**。管理员可以在 `/api/devenv`（`hardening`）中看到实际生效的上限。 |

**网页界面兼容模式**（管理 → 系统设置，`SLOTSERVE_DEVENV_WEB_COMPAT`）是另一个开关：它放宽了「同一浏览器中一个环境的页面不能在另一个环境里操作」的规则（在网页端口 8001 上）。只有当某个应用在环境的网页界面里无法工作、且别无他法时才打开；它只会削弱环境之间的隔离，不影响其他方面。

## 端口与防火墙 {#firewall}

| 端口 | 用途 | 谁需要访问 |
| --- | --- | --- |
| **8000**（`PORT`） | 网页、`/api`、OpenAI 兼容的 `/v1`、`/metrics` | 所有人（或只有反向代理） |
| **8001**（`DEVENV_WEB_PORT`、`SLOTSERVE_DEVENV_WEB_PORT`） | 开发环境的网页界面（VS Code、JupyterLab、桌面、你自己的网页应用） | 使用它们的人（或只有反向代理） |
| **8002**（`SLOTSERVE_DEVENV_GATEWAY_PORT`） | 容器侧网关：只提供 `/v1`，供开发环境里的代码使用 | 只有开发环境容器；不要对网络开放 |
| **2200-2299**（SSH 端口） | 登录开发环境的 SSH，每个运行中的环境一个端口 | 使用 SSH 的人 |
| **2300-2399**（直连端口范围） | 大家从环境中直接发布的端口 | 使用它们的人；直连端口没有 SlotServe 登录保护 |

- 端口范围是设置项（**SSH 端口范围**、**直连端口范围**）；它们之间、以及与宿主机已用的端口之间不要重叠。
- **SSH 端口和直连端口由 Docker 自己发布**，通过它自己的 iptables 规则，会绕过宿主机上 `ufw` 和 `firewalld` 的规则。要限制谁能访问，请使用 `DOCKER-USER` 链或机器前面的防火墙，例如：

    ```bash
    # 只允许 10.0.0.0/8 网段访问开发环境的 SSH 和直连端口
    iptables -I DOCKER-USER -p tcp -m multiport --dports 2200:2399 ! -s 10.0.0.0/8 -j DROP
    ```

- SlotServe 运行在 Docker 中时，**8002 端口**无需任何设置：网关就在 SlotServe 的容器里，在每个环境的网络中以 `slotserve-gw` 的名字访问。SlotServe 直接运行在宿主机上时，它监听 Docker 网桥（`docker0`）的地址；如果宿主机防火墙会丢弃容器到宿主机的流量，需要放行来自 Docker 网络（默认在 `172.16.0.0/12` 和 `192.168.0.0/16` 之内）的 TCP 8002，例如 `ufw allow from 172.16.0.0/12 to any port 8002 proto tcp`。
- 在反向代理之后，可以把 8000 和 8001 绑定到 `127.0.0.1`（`ports: - "127.0.0.1:8000:8000"`），只让代理访问。

## 问题手册 {#runbooks}

### 忘记管理员密码或被锁定 {#admin-password}

**症状。** 管理员无法登录；或提示尝试次数过多。

**判断。** 登录失败会把账号在失败来源的地址上锁定 15 分钟（每个账号加地址 10 次，每个地址 30 次）；其他地址不受影响。在没有设置 `SLOTSERVE_FORWARDED_ALLOW_IPS` 的代理之后，所有人共用一个地址（见[反向代理](#reverse-proxy)）。

**处理。**

- 由另一位管理员在**管理 → 用户**中重置密码。
- 在服务器上：

    ```bash
    docker compose exec slotserve slotserve reset-password <用户名>    # 会提示输入新密码
    docker compose exec slotserve slotserve set-admin <用户名>         # 已经没有管理员时
    ```

    重置后该账号在所有地方都会退出登录。锁定状态保存在内存中：`docker compose restart slotserve` 会清除它。
- 已经完全没有管理员账号：见[初始化码](#setup-code)。

**预防。** 两位管理员；密码保存在密码管理器中。

### 磁盘满 {#disk-full}

**症状。** 健康检查 **disk** 警告或报错；备份、下载或模型加载失败并提示 "No space left on device"；数据库无法写入。

**判断。**

```bash
df -h                                  # 哪个文件系统
du -sh data/* | sort -h | tail         # SlotServe 的数据目录
docker system df                       # 镜像、容器、卷、构建缓存
du -sh "$MODELS_DIR"
```

常见的占用者：模型、Docker 镜像（环境模板、保存的镜像、保留环境的快照）、`/workspace` 卷、`data/backups/` 中的旧备份和升级快照、日志。

**处理。**

- 没人用的模型：在**模型**中删除（会删除文件）。
- Docker：`docker image prune`（未使用的镜像），以及大家不再需要的保存镜像（删除保存的模板会删除其镜像）。不要盲目执行 `docker volume prune`：卷里存着大家的 `/workspace`。
- 备份：升级快照和 `prerestore` 备份包不会被自动删除；不再需要时手动删除旧的。必要时调低 `backup_keep`。
- 日志：推理进程日志在 20 MB 时轮转，SlotServe 容器的 Docker 日志上限为 5 × 50 MB，开发环境容器也有同样的限制。
- 请求记录在 `SLOTSERVE_USAGE_RETENTION_DAYS` 后汇总；审计日志保留 `SLOTSERVE_AUDIT_RETENTION_DAYS`。

**预防。** 模型和 Docker 数据目录放在系统盘以外的磁盘上；设置备份目标；为个人存储设置软上限；留意磁盘检查的警告（从下载最小剩余空间的两倍、至少 5 GB 开始警告）。

### 开发环境启动失败 {#devenv-start}

**症状。** 环境卡片显示没有启动成功；本人已收到通知。

**判断。** 以管理员身份打开**开发环境 → 所有人**，查看该环境的错误和**日志**。已识别的错误会附带诊断（原因和下一步）；见[常见问题：诊断](faq.md#diagnosis)。再按原因处理：

| 原因 | 典型错误 | 处理 |
| --- | --- | --- |
| 拉取镜像 | `pull access denied`、`manifest unknown`、`toomanyrequests` | 检查镜像名和标签；Docker 主机能否访问镜像仓库（代理、镜像源）；私有镜像或 Docker Hub 限流时在主机上 `docker login`；或先手动拉取一次。 |
| 端口冲突 | `port is already allocated`、`address already in use` | 宿主机上有别的程序占用了 SSH 或直连端口范围内的端口：调整端口范围（设置）或那个程序。 |
| 挂载 | `failed to mount local volume`、`mount error(13)`、`access denied by server` | 存储：在管理 → 存储中**检查**；在 Docker 主机上安装 `nfs-common` / `cifs-utils`；NFS 导出必须允许该主机。存储目录：必须是 Docker 主机上的路径。 |
| CPU 绑定 | `Requested CPUs are not available`、`cpuset` | CPU 核心数少于各通道预留的数量：调低**每块 GPU 保留的 CPU 核心**；CPU 变化后重启 SlotServe，让它重新读取宿主机信息。 |
| GPU | `could not select device driver "nvidia"` | [GPU 不见了](#gpu-gone) |
| 通道没有 GPU | `devenv_lane_no_gpus` | 在管理 → GPU 与通道中为通道指定 GPU。 |
| Docker | `Cannot connect to the Docker daemon` | Docker 在运行吗？套接字挂载了吗？见[开发环境：Docker](dev-environments.md#docker)。 |

**处理。** 解决原因后，在该环境上点**重试**。

**预防。** 修改 Docker、驱动或模板后运行 `python -m tools.verify`（见[验证](#verify)）；重要的模板尽量使用你能控制的镜像仓库。

### 驱动或 Docker 升级后 GPU 不见了 {#gpu-gone}

**症状。** 健康检查 **gpu** 提示「没有检测到 GPU」；模型加载报 CUDA 错误；开发环境报 `could not select device driver "nvidia"`。

**判断。**

```bash
nvidia-smi                                                  # 宿主机上：驱动正常吗？
docker run --rm --gpus all ubuntu nvidia-smi                # Docker 能给容器分配 GPU 吗？
docker compose exec slotserve nvidia-smi                    # SlotServe 的容器能看到 GPU 吗？
```

**处理。**

- 宿主机上 `nvidia-smi` 失败：内核模块与新驱动不匹配；重启，或为当前内核重新安装驱动。
- 只有 Docker 失败：重新配置 NVIDIA Container Toolkit 并重启 Docker：

    ```bash
    sudo nvidia-ctk runtime configure --runtime=docker
    sudo systemctl restart docker
    ```

- 模型报 "CUDA driver version is insufficient"：推理引擎需要更新的驱动，或按已安装的 CUDA 重新编译；使用 Docker 时 `CUDA_IMAGE` 的 CUDA 主版本必须与 `llama-server` 编译时一致。
- 然后执行 `docker compose up -d`（重启 Docker 会停止 SlotServe，除非 `restart: unless-stopped` 把它拉起来）。

**预防。** 固定驱动软件包版本（`apt-mark hold`），在维护窗口内升级驱动和 Docker，之后运行 `slotserve doctor` 和 `tools/verify`。

### 怀疑账号或密钥泄露 {#leak}

**症状。** 陌生的登录、没人认得的 API 用量、密钥或密码被贴到了别处。

**判断。**

- **管理 → 审计日志**：按人、按操作（登录和登录失败用 `auth.*`，API 密钥用 `key.*`）和日期筛选；日志记录了地址（要看到真实地址见[反向代理](#reverse-proxy)）。把筛选结果导出为 CSV 留档。
- **管理 → 请求日志**：按人、密钥和来源（API 密钥、环境密钥、Playground）查看请求；每个 API 密钥都会显示最近一次使用的地址。

**处理。**

1. 账号被盗用时**停用账号**（管理 → 用户）：它会在所有地方退出登录，进行中的预约随之结束。
2. **吊销 API 密钥**（管理 → API 密钥，或本人在**访问凭据**中操作）。环境密钥随环境结束而失效。
3. **在所有设备上退出登录**：本人在**设置 → 已登录的设备 → 退出其他所有设备**；修改密码或管理员重置密码也有同样效果。
4. 删除本人不认识的 SSH 公钥（访问凭据），并设置新的环境密码。
5. 如果泄露的是管理员账号或服务器的 `.env`，还要[更换会话密钥](#secret-key-lost)，并更换邀请码、聊天平台令牌和 `SLOTSERVE_METRICS_TOKEN`。

**预防。** 给密钥设置有效期，用邀请链接代替共享的邀请码，管理员尽量少，并定期看看审计日志。

## 在真实硬件上验证 {#verify}

`tools/verify` 通过公开的 API、以大家实际使用的方式，端到端地检查一个运行中的安装：一次 GPU 预约和一次 OpenAI 风格的请求，每个内置开发环境模板的 SSH（密钥和密码）及网页界面，一个无 GPU 环境，一个存储，容器加固和安全防护。它会创建一个临时账号并在结束时删除；它不修改任何设置（关闭的功能记为跳过）。请在安装之后、升级 SlotServe、驱动或 Docker 之后，以及每个发布候选版本上运行它。

在能访问服务器的机器上，从 SlotServe 仓库的源码目录运行（Python 3.11+ 和 `httpx`、OpenSSH 客户端；密码登录还需要 `sshpass`）：

```bash
python -m tools.verify --url http://gpu-server:8000 --admin <管理员账号> --out reports/
```

管理员密码会提示输入（或使用 `SLOTSERVE_VERIFY_PASSWORD`）。常用选项：`--lane`、`--model`、`--templates pytorch,vscode`（只测一部分）、`--storage-id` 或 `--storage-host-path`、`--no-ssh`、`--no-web`、`--save-image`、`--destructive`（还会在无 GPU 环境里运行 fork 炸弹）、`--keep`（保留测试账号以便排查）、`--insecure`（自签名 TLS）。第一次拉取所有模板镜像需要一些时间。

它会写出 `verify-report-<主机>-<版本>.md` 和 `.json`。结果分为 **pass**、**fail**、**skip**（功能关闭或缺少工具）、**pending** 和 **manual**：manual 是工具无法完成的浏览器检查（报告旁会生成一个 CSRF 验证页面，以及两个环境网页之间的隔离检查），报告中附有操作说明。退出码 0 表示没有失败，1 表示有检查失败，2 表示无法访问服务器或无法登录。把报告附在发版 PR 上（见发版检查清单）。
