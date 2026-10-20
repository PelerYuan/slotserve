"""A tiny stand-in for llama.cpp's ``llama-server`` used for development without a GPU.

It accepts the same core flags (``-m``, ``--host``, ``--port``, ``--alias``), ignores the rest,
pretends to load for ``MOCK_LOAD_SECONDS`` seconds (default 5) and then serves an
OpenAI-compatible echo model with streaming and usage reporting.

    SLOTSERVE_LLAMA_SERVER_CMD="python tools/mock_llama_server.py"
"""

import argparse
import asyncio
import json
import os
import sys
import time
import uuid

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

parser = argparse.ArgumentParser()
parser.add_argument("-m", "--model", default="mock.gguf")
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=8081)
parser.add_argument("-a", "--alias", default=None)
# vLLM (`serve PATH --served-model-name`) and SGLang (`--model-path --served-model-name`) styles
parser.add_argument("positional", nargs="*")
parser.add_argument("--model-path", default=None)
parser.add_argument("--served-model-name", default=None)
parser.add_argument("--mock-fail", action="store_true", help="exit immediately, as if the model failed to load")
args, unknown = parser.parse_known_args()
oom_requested = "--mock-oom" in unknown
if args.mock_fail:
    sys.exit("mock: simulated load failure")
if oom_requested:
    print(
        "ggml_backend_cuda_buffer_type_alloc_buffer: allocating 20480.00 MiB on device 0: "
        "cudaMalloc failed: out of memory",
        file=sys.stderr,
    )
    sys.exit(1)

source = args.model_path or (args.positional[-1] if args.positional else None) or args.model
ALIAS = args.alias or args.served_model_name or os.path.basename(source)
# Like vLLM, a server started with --served-model-name rejects requests for any other model name.
STRICT_MODEL = args.served_model_name is not None
LOAD_SECONDS = float(os.environ.get("MOCK_LOAD_SECONDS", "5"))
TOKEN_DELAY = float(os.environ.get("MOCK_TOKEN_DELAY", "0.03"))
STARTED = time.monotonic()

app = FastAPI()
print(f"mock llama-server: model={args.model} alias={ALIAS} extra={unknown}", flush=True)


def loaded() -> bool:
    return time.monotonic() - STARTED >= LOAD_SECONDS


@app.get("/health")
async def health():
    if not loaded():
        return JSONResponse({"error": {"code": 503, "message": "Loading model"}}, status_code=503)
    return {"status": "ok"}


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": ALIAS, "object": "model", "owned_by": "mock"}]}


def _reply_text(prompt: str) -> str:
    return f"[{ALIAS}] You said: {prompt[:200]}"


def _usage(prompt: str, completion: str) -> dict:
    p, c = max(1, len(prompt.split())), max(1, len(completion.split()))
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


def _wrong_model(body: dict):
    if STRICT_MODEL and body.get("model") != ALIAS:
        return JSONResponse({"error": {"message": f"model {body.get('model')!r} does not exist"}}, status_code=404)
    return None


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    if (err := _wrong_model(body)) is not None:
        return err
    messages = body.get("messages") or []
    prompt = " ".join(str(m.get("content", "")) for m in messages if isinstance(m, dict))
    return await _complete(body, prompt, chat=True)


@app.post("/v1/completions")
async def completions(request: Request):
    body = await request.json()
    return await _complete(body, str(body.get("prompt", "")), chat=False)


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    body = await request.json()
    inputs = body.get("input")
    inputs = inputs if isinstance(inputs, list) else [inputs]
    return {
        "object": "list",
        "model": ALIAS,
        "data": [{"object": "embedding", "index": i, "embedding": [0.0] * 8} for i, _ in enumerate(inputs)],
        "usage": {"prompt_tokens": len(inputs), "total_tokens": len(inputs)},
    }


async def _complete(body: dict, prompt: str, chat: bool):
    text = _reply_text(prompt)
    rid = f"mock-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    obj = "chat.completion" if chat else "text_completion"
    usage = _usage(prompt, text)

    if not body.get("stream"):
        choice = (
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
            if chat
            else {"index": 0, "text": text, "finish_reason": "stop"}
        )
        return {"id": rid, "object": obj, "created": created, "model": ALIAS, "choices": [choice], "usage": usage}

    include_usage = bool((body.get("stream_options") or {}).get("include_usage"))

    async def events():
        for word in text.split(" "):
            delta = {"content": word + " "} if chat else None
            choice = {"index": 0, "delta": delta, "finish_reason": None} if chat else {"index": 0, "text": word + " "}
            chunk = {"id": rid, "object": obj + ".chunk", "created": created, "model": ALIAS, "choices": [choice]}
            yield f"data: {json.dumps(chunk)}\n\n"
            await asyncio.sleep(TOKEN_DELAY)
        final = (
            {"index": 0, "delta": {}, "finish_reason": "stop"}
            if chat
            else {"index": 0, "text": "", "finish_reason": "stop"}
        )
        head = {"id": rid, "object": obj + ".chunk", "created": created, "model": ALIAS}
        yield f"data: {json.dumps({**head, 'choices': [final]})}\n\n"
        if include_usage:
            yield f"data: {json.dumps({**head, 'choices': [], 'usage': usage})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


if __name__ == "__main__":
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
