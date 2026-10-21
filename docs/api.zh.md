# 接口

SlotServe 在 `/v1` 提供 **OpenAI 兼容**接口。任何可以设置 base URL 的 OpenAI SDK 或工具都能使用。

| 设置 | 值 |
| --- | --- |
| Base URL | `http://<服务器>:8000/v1` |
| API Key | **访问凭据**页面创建的密钥（`sk-ss-...`） |
| Model | 当前加载的模型名，或其他任意值（见下文） |

## 示例

=== "Python"

    ```python
    from openai import OpenAI

    client = OpenAI(base_url="http://<服务器>:8000/v1", api_key="sk-ss-...")
    stream = client.chat.completions.create(
        model="Qwen3-32B-Q4_K_M",
        messages=[{"role": "user", "content": "用一段话解释 KV 缓存。"}],
        stream=True,
    )
    for chunk in stream:
        if chunk.choices:
            print(chunk.choices[0].delta.content or "", end="", flush=True)
    ```

=== "curl"

    ```bash
    curl http://<服务器>:8000/v1/chat/completions \
      -H "Authorization: Bearer sk-ss-..." \
      -H "Content-Type: application/json" \
      -d '{"model": "Qwen3-32B-Q4_K_M", "messages": [{"role": "user", "content": "你好"}]}'
    ```

=== "环境变量"

    ```bash
    export OPENAI_BASE_URL=http://<服务器>:8000/v1
    export OPENAI_API_KEY=sk-ss-...
    ```

## 接口列表

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/v1/models` | 你此刻能用的模型（在你可用的通道上已加载或正在加载） |
| `POST` | `/v1/chat/completions` | 支持流式 |
| `POST` | `/v1/completions` | 支持流式 |
| `POST` | `/v1/embeddings` | 模型需以 `--embedding` 启动 |
| `POST` | `/v1/*` | 推理服务的其他接口按同样规则转发 |

密钥可以放在 `Authorization: Bearer <key>` 或 `x-api-key: <key>` 请求头中。

## `model` 字段

每个通道同一时间只提供一个模型，这个字段决定请求发往哪个通道：

- 你可用的通道正在提供的模型 → 发往该通道（优先你自己预约的通道）；
- 留空或 SlotServe 不认识的名字 → 你预约的通道，否则是空闲通道；
- 已登记、但只在别人预约的通道上提供的模型 → `403 gpu_reserved`；
- 已登记、但没有任何通道在提供的模型 → `409 model_not_loaded`，需要预约才能加载。

请求总是以实际服务的模型名转发给推理进程，所以会校验模型名的后端（vLLM、SGLang）也能正常接收。完整的路由规则见[多 GPU 与推理后端](gpus-and-engines.md#接口请求由哪个通道处理)。

## 错误

错误采用 OpenAI 格式：`{"error": {"message", "type", "code"}}`。

| 状态码 | `code` | 含义 |
| --- | --- | --- |
| 401 | `missing_api_key` / `invalid_api_key` | 未提供密钥、密钥不存在或已吊销 |
| 403 | `gpu_reserved` | 当前 GPU（或提供该模型的通道）被其他人预约，消息中包含结束时间 |
| 409 | `model_not_loaded` | 请求的已登记模型不在任何你可用的通道上运行 |
| 503 | `gpu_exclusive` | 你自己的整卡预约正在进行，不提供模型服务 |
| 503 | `model_switching` | 即将切换模型，正在等待进行中的请求完成；请按 `Retry-After` 重试 |
| 503 | `model_unavailable` | 模型加载中或启动失败；请按 `Retry-After` 重试 |
| 502 | `upstream_error` | 推理进程没有响应 |

## 用量记录

每个请求都会记录用户、密钥、模型、token 数和耗时。对于流式的对话和补全请求，SlotServe 会加上 `stream_options.include_usage = true`，让最后一个数据块携带 token 数；客户端会多收到一个 `choices` 为空的数据块，与 OpenAI 行为一致。

## Web API

网页使用 `/api` 下独立的 JSON 接口（Cookie 会话），交互式文档位于 `/api/docs`。同一账号 15 分钟内登录失败 10 次（或同一地址 30 次）后，登录接口返回 `429 too_many_attempts`，需等待一段时间。管理接口位于 `/api/admin` 下（`stats`、`users`、`keys`、`reservations` 及其 `approve` 和 `reject`、`lanes`、`runtime`（运行详情、加载记录、日志下载和服务器推送的实时日志 `stream`）、`requests`、`runtime/manual`、`settings`、`backups`、`audit`），需要管理员登录。存储：`GET/POST /api/admin/storage`、`PUT/DELETE /api/admin/storage/{id}`、`POST /api/admin/storage/{id}/check` 和 `GET /api/admin/storage/{id}/usage`（密码只写不读，`has_password` 表示是否已保存）；`GET /api/storage` 列出当前用户可用的存储。
