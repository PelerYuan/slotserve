# Notifications & integrations

SlotServe can message people on Telegram, publish bookings as calendar feeds, export usage as CSV and expose Prometheus metrics. Everything here is optional and configured in **Admin → System settings**.

## Telegram

The bot uses long polling, so the server needs outbound access to Telegram but no public address or open port.

1. In Telegram, talk to [@BotFather](https://t.me/BotFather), send `/newbot` and copy the token.
2. Paste it into **Admin → System settings → Telegram bot token** and save. If Telegram is blocked on your network, also set **Telegram proxy** (`http://…` or `socks5://…`).
3. Each person opens **Settings → Telegram → Link Telegram**. Telegram opens the bot; pressing **Start** links the account.
4. **Send test** checks the connection. Every notification type can be switched on or off per person.

Messages are sent in each person's interface language. Times use the **Time zone** setting.

| Notification | Who gets it |
| --- | --- |
| Booking starts in 10 minutes | Booker |
| Model loaded and ready / GPU free for an exclusive booking | Booker |
| Booking ends in 10 minutes, with **+30 min**, **+1 hour** and **End now** buttons | Booker |
| An admin booked, changed, cancelled or ended your booking | Booker |
| The booked model failed to load and the default model is serving instead | Booker and admins |
| Your booking request was approved, rejected (with the reason) or expired | Booker |
| Your dev environment is ready (with the ssh command) or could not start | Booker |
| Time you waited for is free, or was booked for you | Waitlisted person |
| Your download finished or failed | Person who started it |
| A booking needs approval, with **Approve** and **Reject** buttons | Admins |
| A model failed to load, a new account was created, the models disk is low | Admins |

Commands (private chat with the bot):

| Command | What it does |
| --- | --- |
| `/status` | Loaded model and who has the GPU |
| `/book` | Book GPU time with buttons: lane, model (or whole GPU), day, start, length, confirm |
| `/free` | Free time today and tomorrow, per lane |
| `/cancel` | Pick one of your upcoming bookings and cancel it |
| `/today` | Bookings in the next 24 hours |
| `/mine` | Your upcoming bookings |
| `/extend [minutes]` | Extend your running booking (default 30) |
| `/release` | End your running booking now |
| `/unlink` | Stop notifications to this chat |

Bookings made with `/book` follow exactly the same rules as the web: conflicts, the longest booking, how far ahead, quotas and approval. The bot offers whole-hour start times (and **Now**); use the web for other times, presets or weekly repeats. If the time is taken by the moment you confirm, the bot offers to put you on the waitlist. Typing `/` in Telegram shows the command list.

## Calendar subscription

**Settings → Calendar subscription** shows two private iCalendar links: your own bookings, and everyone's. Add them as a *subscribed* or *internet* calendar in Apple Calendar, Google Calendar, Outlook or Thunderbird. Calendar apps refresh them periodically (usually every 15 minutes to a few hours).

Anyone with a link can read the bookings in it. **New link** invalidates the old ones. Set **Public URL** if people reach SlotServe through an address different from the one the browser shows.

## CSV export

**Usage → Export CSV** downloads, for the selected period and scope (everyone or just you):

| Export | Columns |
| --- | --- |
| Per person | person, bookings, booked hours, booked GPU-hours, used GPU-hours, average utilization % while booked, peak memory (MB), energy (Wh), requests, prompt and completion tokens |
| GPU time | date, lane, GPU, kind, person, GPU-hours, used GPU-hours, average utilization %, peak memory (MB), energy (Wh); one row per day, lane, GPU, kind and person |
| Daily summary | date, person, model, requests, prompt and completion tokens |
| Every request | time, person, API key, model, endpoint, stream, status, tokens, duration |

Kinds are `model`, `gpu` (whole-GPU booking), `devenv`, `idle_model` and `idle_devenv` (shared use while nobody books the lane) and `free`. The GPU column is empty for a lane without GPU metrics. The same files are available at `GET /api/usage/export?kind=people|compute|summary|requests&days=30&scope=all|mine`. Files are UTF-8 with a BOM so Excel opens them correctly.

## Prometheus

Set **Metrics token** to enable `GET /metrics`. Scrape it with the token as a bearer token:

```yaml
scrape_configs:
  - job_name: slotserve
    metrics_path: /metrics
    authorization:
      credentials: <metrics token>
    static_configs:
      - targets: ["gpu-server:8000"]
```

| Metric | Type | Labels |
| --- | --- | --- |
| `slotserve_runtime_state` | gauge | `state` |
| `slotserve_runtime_model_info` | gauge | `model`, `preset` |
| `slotserve_reservation_active` | gauge | `user` |
| `slotserve_reservations_upcoming` | gauge | |
| `slotserve_requests_total` | counter | `user`, `model`, `outcome` (`ok` / `error`) |
| `slotserve_tokens_total` | counter | `user`, `model`, `type` |
| `slotserve_request_seconds_total` | counter | `user`, `model` |
| `slotserve_model_loads_total` | counter | `model`, `result` |
| `slotserve_model_load_seconds` | gauge | `model` |
| `slotserve_downloads_pending` | gauge | |
| `slotserve_gpu_memory_used_bytes`, `…_total_bytes`, `slotserve_gpu_utilization_ratio`, `slotserve_gpu_temperature_celsius`, `slotserve_gpu_power_watts` | gauge | `gpu`, `name`, `lane` |
| `slotserve_lane_occupant` | gauge | `lane`, `kind`, `user` (what the lane is doing now) |
| `slotserve_booked_gpu_hours_total` | counter | `user`, `lane`, `kind` (elapsed booked time × GPUs of the lane) |
| `slotserve_gpu_seconds_total` | counter | `lane`, `gpu`, `kind` (recorded GPU time) |
| `slotserve_gpu_busy_seconds_total` | counter | `lane`, `gpu`, `kind` (time at or above the busy threshold) |
| `slotserve_gpu_energy_joules_total` | counter | `lane`, `gpu` |
| `slotserve_user_gpu_busy_seconds_total` | counter | `user`, `kind` |

Without a token the endpoint returns 404.
