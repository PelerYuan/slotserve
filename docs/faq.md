# FAQ

**A model takes minutes to load. Can SlotServe make it faster?**
Loading is bound by disk speed. Put frequently used models on an SSD (point `MODELS_DIR` at it, or symlink individual files). Linux caches recently read files in free RAM, so on a host with more RAM than the model size a second load is much faster. SlotServe hides the wait by preloading before bookings.

**My client gets `503 model_unavailable`.**
The model is loading or failed to start. The `Retry-After` header says roughly how long to wait. If it keeps failing, open **Models → Server log** to see llama-server's error (often an out-of-memory error from a too large `-c` or a bad argument).

**My client gets `409 model_not_loaded`.**
The request named another registered model. Either omit `model`/use the loaded one, or book a slot with the model you want.

**Can several people share the GPU at the same time?**
Yes, when nobody has booked it: every valid key can use the default model concurrently (raise `-np` in its launch arguments for more parallel slots). During a booking the GPU is exclusive.

**Does it support multiple GPUs or multiple models at once?**
Not yet. SlotServe manages one llama-server process. You can still pass llama.cpp's multi-GPU flags (e.g. `--split-mode`, `--tensor-split`) to that one process.

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

**Where do llama-server logs go?**
`DATA_DIR/logs/llama-server.log` (rotated at 20 MB), also visible in the UI.
