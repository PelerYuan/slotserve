# How scheduling works

SlotServe keeps exactly one `llama-server` process. Every few seconds - and immediately after a booking or model change - the scheduler decides which model *should* be loaded and moves the process towards it.

## Choosing the target model

1. **A booking is in progress** → that booking's model and preset. For a *whole GPU* booking: nothing (llama-server stopped).
2. **The next booking starts within its model's estimated load time + 60 s** → preload the next booking's model now. Before a whole-GPU booking, llama-server is stopped `SLOTSERVE_STOP_TIMEOUT_SECONDS` + 60 s early.
3. **The next booking is so close that loading the default model would only be usable briefly** (gap < default load + next load + 2 × buffer) → skip the default and load the next booking's model directly.
4. **An admin chose a model (or unloading) manually** → that, until its time is up or the next booking starts.
5. **Otherwise** → the default model (or nothing, if no default is set).

A model plus a preset counts as one target: switching presets of the same file reloads llama-server.

The process is only restarted when the target differs from what is loaded. Releasing a booking early triggers an immediate re-evaluation.

## Load time estimates

- After a model has loaded at least once, its estimate is the average of its last five successful loads.
- Before that, it is `file size ÷ SLOTSERVE_DISK_READ_MBPS + 15 s`.

Loads from spinning disks are slow and the page cache only helps when the host has more free RAM than the model's size. Keeping frequently used models on an SSD is the single biggest improvement.

## Who may call the API

| Situation | Booking owner | Everyone else |
| --- | --- | --- |
| No booking active | allowed (current model) | allowed (current model) |
| Booking active, its model ready | allowed | `403 gpu_reserved` |
| Booking active, its model still loading | `503` + `Retry-After` | `403 gpu_reserved` |
| Whole-GPU booking active | `503 gpu_exclusive` | `403 gpu_reserved` |

Free use of the idle GPU is best-effort: it may be interrupted when the next booking's model is preloaded.

## Back-to-back bookings

Loading can only start after the previous booking ends. If two adjacent bookings use different models, the second booking spends the start of its slot loading. When both use the same model there is no reload at all. The booking dialog points this out.

## Failures

- If llama-server exits or does not become healthy within `SLOTSERVE_LOAD_TIMEOUT_SECONDS`, the runtime is marked **failed** with the reason, and the scheduler retries with a growing back-off (30 s → 10 min).
- **Fallback**: while a booked model is waiting for its next retry, the default model serves the booking so the booker is not left without a model. The overview shows a banner, and the booker and admins are notified. When the retry succeeds, the booked model takes over again.
- The overview page shows the error, and **Server log** shows llama-server's own output.
- `-m`, `--host`, `--port` and `--alias` are managed by SlotServe and stripped from user arguments, so a typo there cannot break routing.
