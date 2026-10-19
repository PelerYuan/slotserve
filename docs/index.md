# SlotServe

**SlotServe turns one GPU into a shared, bookable service.** People book time slots in a web calendar, get exclusive use of the GPU during their slot, and call models through an OpenAI-compatible API with their own key. SlotServe runs [llama.cpp](https://github.com/ggml-org/llama.cpp)'s `llama-server` for you and switches models automatically before each booking.

![Overview](assets/screenshots/overview.png)

## Why

A lab or small team often shares a single big GPU. Without coordination people step on each other: one person's 30B model is unloaded by someone else's experiment, nobody knows who is using the card, and long model loads from spinning disks waste everyone's time. SlotServe fixes that with three rules:

1. **Bookings are exclusive.** During your slot only your API keys are accepted.
2. **Idle time is shared.** When nobody has booked the GPU, every key can use the default model.
3. **Switching models requires a booking.** That lets the scheduler load your model *before* your slot starts.

## Features

| | |
| --- | --- |
| Calendar booking | Drag across a week view to book; first come, first served; no approvals |
| OpenAI-compatible gateway | `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings` and any other llama-server route, with streaming |
| Per-user API keys | Create, name and revoke keys; only a SHA-256 hash is stored |
| Managed llama-server | Started, health-checked, restarted and switched by SlotServe; per-model launch arguments edited in the browser |
| Preloading | Load times are learned per model; the next booking's model is loaded ahead of time |
| Model downloads | Paste a direct link (e.g. a Hugging Face `resolve` URL); resumable, one at a time |
| Usage | Requests, prompt/completion tokens and booked hours per person and per model |
| Admin panel | Manage users, bookings and API keys, change settings at runtime, audit log |
| GPU status | Memory, utilization, temperature and power from `nvidia-smi` |
| Bilingual UI | English and Simplified Chinese |
| Simple deployment | One container, SQLite, three bind mounts |

## Next steps

- [Getting started](getting-started.md) - deploy with Docker Compose in a few minutes.
- [How scheduling works](scheduling.md) - what happens around a booking.
- [API](api.md) - point your OpenAI client at SlotServe.
