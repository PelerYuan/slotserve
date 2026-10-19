# 配置参考

SlotServe 读取 `SLOTSERVE_*` 环境变量，或工作目录下的 `.env` 文件。Docker Compose 会自动加载 `.env`。

## Compose 变量

这些变量只在 `docker-compose.yml` 中使用，描述的是**宿主机**。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LLAMA_CPP_DIR` | *必填* | `llama-server` 所在目录，只读挂载到 `/opt/llama.cpp` |
| `MODELS_DIR` | *必填* | `.gguf` 模型目录，挂载到 `/models` |
| `DATA_DIR` | `./data` | SQLite 数据库与日志，挂载到 `/data` |
| `PORT` | `8000` | 对外端口 |
| `CUDA_IMAGE` | `nvidia/cuda:12.8.1-runtime-ubuntu24.04` | 基础镜像；CUDA 主版本需与 llama.cpp 编译时一致 |

## SlotServe 设置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SLOTSERVE_INVITE_CODE` | `change-me` | 注册所需邀请码 |
| `SLOTSERVE_REGISTRATION_OPEN` | `true` | 是否允许凭邀请码注册 |
| `SLOTSERVE_SECRET_KEY` | `change-me` | 会话 Cookie 签名密钥 |
| `SLOTSERVE_HOST` | `0.0.0.0` | 监听地址 |
| `SLOTSERVE_PORT` | `8000` | 监听端口 |
| `SLOTSERVE_COOKIE_SECURE` | `false` | Cookie 加 `Secure` 标记；在 HTTPS 后面时设为 `true` |
| `SLOTSERVE_DATA_DIR` | `data` | 数据库与日志目录 |
| `SLOTSERVE_MODELS_DIR` | `models` | 模型目录（扫描及下载目标） |
| `SLOTSERVE_STATIC_DIR` | *内置* | 前端构建产物目录 |
| `SLOTSERVE_LLAMA_SERVER_CMD` | `llama-server` | 启动 llama-server 的命令，可带前缀，如 `taskset -c 0-15 llama-server` |
| `SLOTSERVE_LLAMA_HOST` | `127.0.0.1` | llama-server 监听地址（请保持本地） |
| `SLOTSERVE_LLAMA_PORT` | `8081` | llama-server 监听端口 |
| `SLOTSERVE_DEFAULT_LLAMA_ARGS` | `-ngl 99 -c 8192` | 新登记模型的默认启动参数 |
| `SLOTSERVE_LOAD_TIMEOUT_SECONDS` | `1800` | 超过此时间仍未就绪则判定加载失败 |
| `SLOTSERVE_STOP_TIMEOUT_SECONDS` | `20` | 强制结束 llama-server 前的等待时间 |
| `SLOTSERVE_DISK_READ_MBPS` | `150` | 模型首次加载前用于估算加载时间 |
| `SLOTSERVE_SLOT_MINUTES` | `30` | 日历时间粒度 |
| `SLOTSERVE_MAX_RESERVATION_HOURS` | `8` | 单次预约最长时长 |
| `SLOTSERVE_MAX_ADVANCE_DAYS` | `30` | 最多可提前几天预约 |
| `SLOTSERVE_PRELOAD_BUFFER_SECONDS` | `60` | 预加载时额外预留的时间 |
| `SLOTSERVE_SCHEDULER_INTERVAL_SECONDS` | `5` | 调度器重新评估的间隔 |
| `SLOTSERVE_DOWNLOAD_MIN_FREE_GB` | `2` | 下载完成后磁盘至少保留的空间 |
| `SLOTSERVE_PUBLIC_URL` | *空* | 大家访问 SlotServe 的地址，用于日历订阅链接 |
| `SLOTSERVE_TIMEZONE` | `UTC` | IANA 时区，用于通知和每周配额 |
| `SLOTSERVE_QUOTA_WEEKLY_HOURS` | `0` | 每人每周可预约小时数（0 不限，管理员不受限） |
| `SLOTSERVE_QUOTA_UPCOMING` | `0` | 每人可持有的未结束预约数（0 不限） |
| `SLOTSERVE_REMINDER_MINUTES` | `10` | 开始和结束提醒的提前量（分钟） |
| `SLOTSERVE_HF_ENDPOINT` | `https://huggingface.co` | Hugging Face 地址或镜像 |
| `SLOTSERVE_HF_TOKEN` | *空* | 受限或私有仓库所需的 Token |
| `SLOTSERVE_TELEGRAM_BOT_TOKEN` | *空* | Telegram 机器人 Token，留空则不启用 |
| `SLOTSERVE_TELEGRAM_PROXY` | *空* | 连接 Telegram 的 `http://` 或 `socks5://` 代理 |
| `SLOTSERVE_METRICS_TOKEN` | *空* | `/metrics` 的 Bearer Token，留空则关闭 |

其中大部分可由管理员在**管理 → 系统设置**中直接修改，无需重启：注册、邀请码、预约限制与配额、默认启动参数、时区、访问地址、Hugging Face、Telegram 和监控设置。修改保存在数据库中，优先于 `.env`；点**恢复默认**即可取消覆盖。

## 每个模型的启动参数

每个模型有一行自由填写的参数，会追加到命令末尾：

```text
<SLOTSERVE_LLAMA_SERVER_CMD> -m <文件> --host 127.0.0.1 --port 8081 --alias <名称> <你的参数>
```

`-m`、`--host`、`--port`、`--alias` 始终由 SlotServe 设置，你参数里的同名项会被忽略。常用参数：

| 目的 | 参数 |
| --- | --- |
| 全部层放到 GPU | `-ngl 99` |
| 上下文长度 | `-c 16384` |
| 并发请求 | `-np 4`（上下文在各 slot 间平分） |
| Flash Attention | `--flash-attn on` |
| 视觉模型 | `--mmproj /models/mmproj-model-f16.gguf` |
| 向量模型 | `--embedding --pooling mean` |
| KV 缓存量化 | `-ctk q8_0 -ctv q8_0` |

完整列表见 `llama-server --help`。修改了当前已加载模型的参数后，在总览页点击**重新加载模型**即可生效。

## 管理员命令行

日常的账号管理在**管理 → 用户**中完成。命令行用于没有管理员能登录的情况：

```bash
docker compose exec slotserve slotserve reset-password <用户名>
docker compose exec slotserve slotserve set-admin <用户名>
docker compose exec slotserve slotserve set-admin <用户名> --revoke
```
