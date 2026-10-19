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
| Time you waited for is free, or was booked for you | Waitlisted person |
| Your download finished or failed | Person who started it |
| A model failed to load, a new account was created, the models disk is low | Admins |

Commands (private chat with the bot):

| Command | What it does |
| --- | --- |
| `/status` | Loaded model and who has the GPU |
| `/today` | Bookings in the next 24 hours |
| `/mine` | Your upcoming bookings |
| `/extend [minutes]` | Extend your running booking (default 30) |
| `/release` | End your running booking now |
| `/unlink` | Stop notifications to this chat |

New bookings are made on the web.

## Calendar subscription

**Settings → Calendar subscription** shows two private iCalendar links: your own bookings, and everyone's. Add them as a *subscribed* or *internet* calendar in Apple Calendar, Google Calendar, Outlook or Thunderbird. Calendar apps refresh them periodically (usually every 15 minutes to a few hours).

Anyone with a link can read the bookings in it. **New link** invalidates the old ones. Set **Public URL** if people reach SlotServe through an address different from the one the browser shows.

## CSV export

**Usage → Export CSV** downloads either a daily summary (date, person, model, requests, tokens) or every request (time, person, API key, model, endpoint, status, tokens, duration) for the selected period. Files are UTF-8 with a BOM so Excel opens them correctly.

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
| `slotserve_gpu_memory_used_bytes`, `…_total_bytes`, `slotserve_gpu_utilization_ratio`, `slotserve_gpu_temperature_celsius`, `slotserve_gpu_power_watts` | gauge | `gpu`, `name` |

Without a token the endpoint returns 404.
