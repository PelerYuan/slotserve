# API

SlotServe exposes an **OpenAI-compatible** API at `/v1`. Any OpenAI SDK or tool that lets you set a base URL works.

| Setting | Value |
| --- | --- |
| Base URL | `http://<server>:8000/v1` |
| API key | a key from the **Access credentials** page (`sk-ss-...`) |
| Model | the loaded model's name, or anything else (see below) |

## Examples

=== "Python"

    ```python
    from openai import OpenAI

    client = OpenAI(base_url="http://<server>:8000/v1", api_key="sk-ss-...")
    stream = client.chat.completions.create(
        model="Qwen3-32B-Q4_K_M",
        messages=[{"role": "user", "content": "Explain KV caching in one paragraph."}],
        stream=True,
    )
    for chunk in stream:
        if chunk.choices:
            print(chunk.choices[0].delta.content or "", end="", flush=True)
    ```

=== "curl"

    ```bash
    curl http://<server>:8000/v1/chat/completions \
      -H "Authorization: Bearer sk-ss-..." \
      -H "Content-Type: application/json" \
      -d '{"model": "Qwen3-32B-Q4_K_M", "messages": [{"role": "user", "content": "Hello"}]}'
    ```

=== "Environment"

    ```bash
    export OPENAI_BASE_URL=http://<server>:8000/v1
    export OPENAI_API_KEY=sk-ss-...
    ```

## Endpoints

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/v1/models` | Models you can use right now (loaded or loading on a lane you may use) |
| `POST` | `/v1/chat/completions` | Streaming supported |
| `POST` | `/v1/completions` | Streaming supported |
| `POST` | `/v1/embeddings` | The model must be started with `--embedding` |
| `POST` | `/v1/*` | Any other route of the model server is forwarded under the same rules |

The key can be sent as `Authorization: Bearer <key>` or `x-api-key: <key>`.

## The `model` field

Each lane serves one model at a time. The field picks the lane:

- a model that a lane you may use is serving → that lane (your booked lane first);
- empty or a name SlotServe does not know → your booked lane, otherwise a free lane;
- a registered model that is only served on a lane someone else booked → `403 gpu_reserved`;
- a registered model that no lane is serving → `409 model_not_loaded`. Book a slot to load it.

The request always reaches the model server with the served model's name, so engines that check the name (vLLM, SGLang) accept it. See [GPUs, lanes & engines](gpus-and-engines.md#which-lane-serves-an-api-request) for the full routing.

## Errors

Errors use the OpenAI shape: `{"error": {"message", "type", "code"}}`.

| Status | `code` | Meaning |
| --- | --- | --- |
| 401 | `missing_api_key` / `invalid_api_key` | No key, unknown key or revoked key |
| 403 | `gpu_reserved` | Someone else has booked the GPU (or the lane serving that model) right now; the message says until when |
| 409 | `model_not_loaded` | You asked for a registered model that no lane you may use is serving |
| 503 | `gpu_exclusive` | Your own whole-GPU booking is running, so no model is served |
| 503 | `model_switching` | Running requests are finishing before a model switch; honour `Retry-After` |
| 503 | `model_unavailable` | The model is loading or failed to start; honour `Retry-After` |
| 502 | `upstream_error` | The model server did not answer |

## Usage accounting

Every request is logged with its user, key, model, token counts and duration. For streaming chat and completion requests SlotServe adds `stream_options.include_usage = true` so the final chunk carries token counts; clients see one extra chunk with an empty `choices` list, as with OpenAI.

## Web API

The browser UI uses a separate JSON API under `/api` with cookie sessions. After 10 failed sign-ins for one account (or 30 from one address) within 15 minutes, sign-in returns `429 too_many_attempts` until the window passes. Its interactive reference is served at `/api/docs`. Admin endpoints live under `/api/admin` (`stats`, `users`, `keys`, `reservations` with `approve` and `reject`, `lanes`, `runtime` (details, load history, log download and a live `stream` of server-sent events), `requests`, `runtime/manual`, `settings`, `backups`, `audit`) and require an admin session. Storage: `GET/POST /api/admin/storage`, `PUT/DELETE /api/admin/storage/{id}`, `POST /api/admin/storage/{id}/check` and `GET /api/admin/storage/{id}/usage` (passwords are write-only: `has_password` tells whether one is stored); `GET /api/storage` lists the storages of the signed-in person. The model catalog is under `/api/catalog` (see [Model catalog](model-catalog.md#api)).
