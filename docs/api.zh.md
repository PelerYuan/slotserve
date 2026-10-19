# 接口

SlotServe 在 `/v1` 提供 **OpenAI 兼容**接口。任何可以设置 base URL 的 OpenAI SDK 或工具都能使用。

| 设置 | 值 |
| --- | --- |
| Base URL | `http://<服务器>:8000/v1` |
| API Key | **API 密钥**页面创建的密钥（`sk-ss-...`） |
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
| `GET` | `/v1/models` | 当前已加载（或加载中）的模型 |
| `POST` | `/v1/chat/completions` | 支持流式 |
| `POST` | `/v1/completions` | 支持流式 |
| `POST` | `/v1/embeddings` | 模型需以 `--embedding` 启动 |
| `POST` | `/v1/*` | llama-server 的其他接口按同样规则转发 |

密钥可以放在 `Authorization: Bearer <key>` 或 `x-api-key: <key>` 请求头中。

## `model` 字段

同一时间只加载一个模型，因此：

- 留空、等于当前模型名、或 SlotServe 不认识的名字 → 转发给当前模型；
- **另一个已登记模型**的名字 → 返回 `409 model_not_loaded`，需要预约才能切换。

## 错误

错误采用 OpenAI 格式：`{"error": {"message", "type", "code"}}`。

| 状态码 | `code` | 含义 |
| --- | --- | --- |
| 401 | `missing_api_key` / `invalid_api_key` | 未提供密钥、密钥不存在或已吊销 |
| 403 | `gpu_reserved` | 当前 GPU 被其他人预约，消息中包含结束时间 |
| 409 | `model_not_loaded` | 请求了一个已登记但未加载的模型 |
| 503 | `model_unavailable` | 模型加载中或启动失败；请按 `Retry-After` 重试 |
| 502 | `upstream_error` | llama-server 没有响应 |

## 用量记录

每个请求都会记录用户、密钥、模型、token 数和耗时。对于流式的对话和补全请求，SlotServe 会加上 `stream_options.include_usage = true`，让最后一个数据块携带 token 数；客户端会多收到一个 `choices` 为空的数据块，与 OpenAI 行为一致。

## Web API

网页使用 `/api` 下独立的 JSON 接口（Cookie 会话），交互式文档位于 `/api/docs`。管理接口位于 `/api/admin` 下（`stats`、`users`、`keys`、`reservations`、`settings`、`audit`），需要管理员登录。
