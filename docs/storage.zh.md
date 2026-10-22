# 存储

管理员在**管理 → 存储**中添加开发环境可使用的存储：Docker 主机上的目录、NFS 导出或 SMB/CIFS 共享。每个存储有一个用途，规定谁可以使用，并会定期检查。

不添加任何存储时一切照旧：`/workspace` 和保留目录保存在 Docker 命名卷中（或旧的**存储目录**设置下，见[开发环境](dev-environments.md)）。

## 类型 {#types}

| 类型 | 需要填写 | 容器如何使用 |
| --- | --- | --- |
| 主机目录 | 运行 Docker 的机器上的绝对路径，例如 `/srv/data` | 绑定挂载 |
| NFS | 服务器（主机名或 IP）、导出路径（`/export/data`）、可选的挂载选项（默认 `nfsvers=4`） | `local` 驱动、`type=nfs` 的 Docker 命名卷 |
| SMB / CIFS | 服务器、共享（`share` 或 `share/目录`；也可以直接粘贴 `//server/share/目录`）、用户名和密码、可选的挂载选项（默认 `vers=3.0`） | `local` 驱动、`type=cifs` 的 Docker 命名卷 |

NFS 和 SMB 在容器启动时由 Docker 自己挂载，不需要写 `/etc/fstab`，以后也可以在其他机器上使用同一个存储。Docker 主机只需要安装常用的挂载工具：

- NFS：`nfs-common`（Debian、Ubuntu）或 `nfs-utils`（RHEL、Fedora、openSUSE）；
- SMB：`cifs-utils`。

Docker Desktop、rootless Docker 和 Podman 可能无法挂载 NFS 或 SMB 卷，这时请改用主机目录（用其他方式挂载到主机上）。

SMB 密码用 `SLOTSERVE_SECRET_KEY` 加密保存，之后不会再显示（留空即保持不变）。Docker 需要把它写进卷的选项，所以能在主机上执行 `docker volume inspect` 的人都能读到它——请为 SlotServe 单独建一个共享账号。挂载选项是一个用逗号分隔的列表，因此密码不能包含逗号。挂载选项中不能出现 `addr`、`username` 或 `password`，这些由 SlotServe 自己设置。

## 用途

| 用途 | 在环境中 | 在存储上 |
| --- | --- | --- |
| **共享数据** | 挂载在其挂载路径（例如 `/data/team`），勾选后只读 | 整个存储 |
| **个人空间** | 每个人的目录就是自己的 `/workspace`，代替 Docker 卷 | `users/u<用户 ID>` |
| **项目数据** | 保留的环境和模板的保留目录 | `projects/p<项目 ID>/<路径>`、`templates/u<用户 ID>-t<模板 ID>/<路径>` |
| **模型库** | 设置了挂载路径时挂载在那里（例如 `/mnt/models/team`），通常只读 | 模型文件 |

- 个人目录（以及每个保留目录）在第一次需要时创建，并像新的 Docker 卷一样填入镜像中的内容。这由一个使用 `SLOTSERVE_STORAGE_HELPER_IMAGE`、用完即删的辅助容器完成（从不使用环境自己的镜像；镜像中的内容通过 Docker 的归档 API 复制出来），因此 SlotServe 本身不需要访问存储。对于宿主机目录，辅助容器只挂载它要处理的目录的上一级；NFS 导出和 SMB 共享只能从根目录挂载，因为还不存在的子目录无法挂载。
- 空闲时的共享环境（见[开发环境](dev-environments.md)）会挂载所有面向**所有人**的存储，并把个人空间整体挂载在 `/workspaces`：每个人的目录 `/workspaces/u<用户 ID>` 属于他在那里的账号，只对他开放。在预约的环境中以 `root` 写入的文件，可能需要 `chown` 后才能在共享环境中修改。
- **项目数据**取代**存储目录**设置来存放保留目录；两者都没有时使用 Docker 卷。共享环境的主目录仍由该设置决定。
- **模型库**：SlotServe 会扫描其中的 GGUF 文件（**模型 → 扫描目录**），下载时也可以选它作为目标（`POST /api/downloads` 和 `POST /api/downloads/hf` 的 `storage_id`）。SlotServe 通过**SlotServe 读取它的路径**来读写模型库：主机目录默认就是该目录（SlotServe 运行在主机上时）。NFS 或 SMB 需要先挂载到 SlotServe 所在机器（或 SlotServe 容器）上，再填写那个路径。`SLOTSERVE_MODELS_DIR` 仍是默认模型库，并照旧挂载在 `/models`。
- 同一用途有多个存储时：个人空间和项目数据使用此人有权使用的第一个（最早添加的），共享数据和模型库则全部挂载。

## 谁可以使用

**所有人**、**指定的人**或**仅管理员**。无权使用的人不会挂载该存储；个人空间和项目数据会依次退回到下一个存储或 Docker 卷。每个人都能在**开发环境**页面看到自己的环境会挂载哪些存储、挂载在哪里以及自己的用量。

挂载路径必须是绝对路径，不能是系统目录、`/workspace`、`/workspaces`、`/models`，也不能与其他存储的挂载路径重叠。

## 检查、用量和软上限

**检查**会在一个 `SLOTSERVE_STORAGE_HELPER_IMAGE`（默认 `busybox:1.37`，缺少时自动拉取）的临时容器中挂载存储，显示是否可访问、失败时的错误、可用和总空间（`df`）以及是否可写。个人空间还会统计每个人的目录大小（`du`）；**用量**按从大到小列出。

开发环境开启时，SlotServe 每隔 `SLOTSERVE_STORAGE_CHECK_MINUTES`（默认 30；0 表示只在管理员点击**检查**时）自动检查所有存储。原本正常的存储无法使用时会通知管理员。

个人空间可以设置**每人软上限**：超出的人会收到通知（管理员也会收到），在回到上限以下之前只通知一次。不会阻止写入。

## 修改或移除存储

修改对之后启动的容器生效。保留的环境会比较创建时的存储：下次预约时如果不同，容器会先做快照，再用新的挂载重新创建（已安装的软件会保留）。修改 NFS/SMB 存储的挂载方式会创建新的 Docker 卷；旧卷在没有容器使用后删除。

移除存储只是把它从 SlotServe 中移除，其中的数据不会被删除。连同数据删除保留的环境时，也会删除它在项目存储上的目录。

## 示例

NFS 服务器（Linux），把 `/srv/nfs/homes` 导出给 Docker 主机 `10.0.0.20`：

```bash
sudo apt install nfs-kernel-server
sudo mkdir -p /srv/nfs/homes
echo '/srv/nfs/homes 10.0.0.20(rw,sync,no_subtree_check,no_root_squash)' | sudo tee -a /etc/exports
sudo exportfs -ra
```

`no_root_squash` 让辅助容器可以创建目录并设置属主。在 SlotServe 中：类型 **NFS**，服务器 `10.0.0.15`，导出路径 `/srv/nfs/homes`，用途**个人空间**。NFSv3 请使用选项 `nfsvers=3,nolock`。

NAS 或 Samba 服务器上的 SMB 共享（`[team]`，`path = /srv/samba/team`，用 `smbpasswd -a slotserve` 创建的用户 `slotserve`）：类型 **SMB / CIFS**，服务器 `nas.local`，共享 `team`（或 `team/datasets`），用户 `slotserve` 及其密码，用途**共享数据**，挂载路径 `/data/team`，勾选**只读**。CIFS 没有 Unix 属主：除非添加例如 `uid=0,gid=0,file_mode=0660,dir_mode=0770`（共享环境中的账号不是 root，可用 `0777`），容器中的文件都属于 `root`。其他常用选项：旧服务器用 `vers=2.1`，以及 `domain=STUDIO`。

在 Docker 主机上手动试一下存储：

```bash
docker volume create --driver local --opt type=nfs --opt o=addr=10.0.0.15,nfsvers=4 --opt device=:/srv/nfs/homes test-nfs
docker run --rm -v test-nfs:/s busybox:1.37 df -h /s
docker volume rm test-nfs
```
