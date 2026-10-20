# FAQ

**A model takes minutes to load. Can SlotServe make it faster?**
Loading is bound by disk speed. Put frequently used models on an SSD (point `MODELS_DIR` at it, or symlink individual files). Linux caches recently read files in free RAM, so on a host with more RAM than the model size a second load is much faster. SlotServe hides the wait by preloading before bookings.

**My client gets `503 model_unavailable`.**
The model is loading or failed to start. The `Retry-After` header says roughly how long to wait. If it keeps failing, open **Models → Server log** to see the model server's error (often an out-of-memory error from a too large `-c` or a bad argument).

**My client gets `409 model_not_loaded`.**
The request named another registered model. Either omit `model`/use the loaded one, or book a slot with the model you want.

**Can several people share the GPU at the same time?**
Yes, when nobody has booked it: every valid key can use the default model concurrently (raise `-np` in its launch arguments for more parallel slots). During a booking the GPU is exclusive.

**Does it support multiple GPUs or multiple models at once?**
Yes. In **Admin → GPUs & lanes** split the GPUs into lanes; each lane runs its own model and is booked separately, so several models are loaded at once. To use several GPUs for one model, put them in one lane and pass the engine's multi-GPU flags (for llama.cpp `--split-mode`/`--tensor-split`, for vLLM `--tensor-parallel-size`). See [GPUs, lanes & engines](gpus-and-engines.md).

**Can I use vLLM or SGLang instead of llama.cpp?**
Yes, per model: **Models → Add model** and pick the engine. The engine must be installed where SlotServe runs; `Dockerfile.vllm` builds an image with vLLM.

**I forgot my password.**
An admin can run `docker compose exec slotserve slotserve reset-password <username>`.

**How do I put it behind HTTPS / a reverse proxy?**
Proxy everything to port 8000 and disable buffering so streaming works. For nginx:

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
    proxy_read_timeout 1h;
}
```

Then set `SLOTSERVE_COOKIE_SECURE=true`.

**How do I back up?**
Copy `DATA_DIR` (it contains `slotserve.db`). Models live in `MODELS_DIR` and are not touched except by downloads and explicit deletes.

**Where do the model server logs go?**
`DATA_DIR/logs/llama-server.log` for the first lane and `llama-server-lane<N>.log` for the others (rotated at 20 MB), also visible in the UI. The file name is the same for every engine.
