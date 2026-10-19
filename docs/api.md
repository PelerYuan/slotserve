# API

SlotServe exposes an **OpenAI-compatible** API at `/v1`. Any OpenAI SDK or tool that lets you set a base URL works.

| Setting | Value |
| --- | --- |
| Base URL | `http://<server>:8000/v1` |
| API key | a key from the **API keys** page (`sk-ss-...`) |
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
| `GET` | `/v1/models` | The model that is loaded (or loading) right now |
| `POST` | `/v1/chat/completions` | Streaming supported |
| `POST` | `/v1/completions` | Streaming supported |
| `POST` | `/v1/embeddings` | The model must be started with `--embedding` |
| `POST` | `/v1/*` | Any other llama-server route is forwarded under the same rules |

The key can be sent as `Authorization: Bearer <key>` or `x-api-key: <key>`.

## The `model` field

Only one model is loaded at a time, so the field works like this:

- empty, equal to the loaded model, or a name SlotServe does not know → the request goes to the loaded model;
- the name of **another registered model** → `409 model_not_loaded`. Book a slot to switch.

## Errors

Errors use the OpenAI shape: `{"error": {"message", "type", "code"}}`.

| Status | `code` | Meaning |
| --- | --- | --- |
| 401 | `missing_api_key` / `invalid_api_key` | No key, unknown key or revoked key |
| 403 | `gpu_reserved` | Someone else has booked the GPU right now; the message says until when |
| 409 | `model_not_loaded` | You asked for a registered model that is not loaded |
| 503 | `gpu_exclusive` | Your own whole-GPU booking is running, so no model is served |
| 503 | `model_unavailable` | The model is loading or failed to start; honour `Retry-After` |
| 502 | `upstream_error` | llama-server did not answer |

## Usage accounting

Every request is logged with its user, key, model, token counts and duration. For streaming chat and completion requests SlotServe adds `stream_options.include_usage = true` so the final chunk carries token counts; clients see one extra chunk with an empty `choices` list, as with OpenAI.

## Web API

The browser UI uses a separate JSON API under `/api` with cookie sessions. Its interactive reference is served at `/api/docs`. Admin endpoints live under `/api/admin` (`stats`, `users`, `keys`, `reservations`, `settings`, `audit`) and require an admin session.
