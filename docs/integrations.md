# Notifications & integrations

SlotServe can message people and answer commands on Telegram, Feishu / Lark, DingTalk, WeCom and QQ, post alerts and announcements to group chats, publish bookings as calendar feeds, export usage as CSV and expose Prometheus metrics. Everything here is optional and configured in **Admin → System settings**; **Admin → Chat channels** shows whether each bot is connected.

## Chat platforms

| Platform | How SlotServe receives messages | Public URL needed | Buttons |
| --- | --- | --- | --- |
| Telegram | Long polling | No | Inline buttons |
| Feishu / Lark | Long connection (WebSocket) | No | Interactive cards |
| DingTalk | Stream mode (WebSocket) | No | Numbered choices (tap to send) |
| WeCom | Callback URL | Only to receive commands | Numbered choices |
| QQ | WebSocket gateway | No | Numbered choices |

Every platform offers the same commands and notifications. Turn on as many as you like: a person gets each notification on every platform they linked, and which notifications they get is chosen once for all platforms.

**Linking.** Each person opens **Settings → Chat notifications** and presses **Link** next to a platform. Telegram opens the bot and pressing **Start** links the account. On the other platforms SlotServe shows a message like `bind K7P2QX`; send it to the SlotServe bot in a private chat within 30 minutes (`绑定 K7P2QX` works too). A code works once; one platform account belongs to one SlotServe account. **Send test** checks the connection, the **Notify** switch pauses one platform, and **Unlink** (or `/unlink` in the chat) removes it.

Messages are sent in each person's interface language. Times use the **Time zone** setting. Group chats and people who have not linked yet get the **Group chat language**.

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

Bookings made with `/book` follow exactly the same rules as the web: conflicts, the longest booking, how far ahead, quotas and approval. The bot offers whole-hour start times (and **Now**); use the web for other times, presets or weekly repeats. If the time is taken by the moment you confirm, the bot offers to put you on the waitlist.

Outside Telegram, commands also work without the `/` and in Chinese (`状态`, `预约`, `空闲`, `取消`, `今天`, `我的`, `延长`, `结束`, `解绑`). Where a platform has no buttons, the choices are numbered and you reply with the number; on DingTalk the numbers are links that send themselves. In a group chat (not on Telegram) the bot only answers `/status`, `/free`, `/today` and `/help` when it is @mentioned; everything that touches one person's bookings works in the private chat.

## Telegram

The bot uses long polling, so the server needs outbound access to Telegram but no public address or open port.

1. In Telegram, talk to [@BotFather](https://t.me/BotFather), send `/newbot` and copy the token.
2. Paste it into **Admin → System settings → Telegram bot token** and save. If Telegram is blocked on your network, also set **Telegram proxy** (`http://…` or `socks5://…`).
3. People link with **Settings → Chat notifications → Link** next to Telegram. Typing `/` in Telegram shows the command list.

## Feishu / Lark {#feishu-lark}

A self-built app with the bot ability. Events and card button presses arrive over the long connection that SlotServe opens, so no public address is needed.

1. In the developer console ([open.feishu.cn/app](https://open.feishu.cn/app), for Lark [open.larksuite.com/app](https://open.larksuite.com/app)) create a **custom app** (self-built app) and add the **Bot** ability.
2. Under **Permissions & Scopes** add: read and send messages in private and group chats (`im:message`), send messages as the app (`im:message:send_as_bot`), receive private messages (`im:message.p2p_msg:readonly`) and receive group messages that mention the bot (`im:message.group_at_msg:readonly`).
3. Copy **App ID** and **App Secret** from **Credentials & Basic Info** into **Admin → System settings → Feishu / Lark bot**, switch it on and save. For Lark set **Open platform address** to `https://open.larksuite.com`. Wait until **Admin → Chat channels** shows **Connected**.
4. Under **Events & Callbacks**, choose **Receive events through persistent connection** for both events and callbacks (the console only saves this while SlotServe is connected), then add the event `im.message.receive_v1` (Receive messages) and the callback `card.action.trigger` (Card action).
5. Create a version, include the people who should use the bot in its availability, and publish it.

## DingTalk {#dingtalk}

An internal app robot in Stream mode: SlotServe opens a WebSocket to DingTalk, so no public address is needed.

1. In the [developer console](https://open-dev.dingtalk.com) create an **internal app** (H5 micro app or app with a robot) and add the **Robot** capability. Set the robot's **message receiving mode** to **Stream mode**.
2. Under **Permissions** make sure the app may send robot messages to people in the organisation (`qyapi_robot_sendmsg`), which notifications need.
3. Copy **Client ID** (AppKey) and **Client Secret** (AppSecret) from **Credentials & Basic Info** into **Admin → System settings → DingTalk bot**, switch it on and save. **Robot code** can stay empty: it defaults to the Client ID, as for most internal apps.
4. Publish the app version with the right visibility. People find the robot by name (or open it from the app) and send their bind message. In a group, add the robot to the group and @mention it.

DingTalk robot messages cannot carry callback buttons without a card template, so choices are numbered; the numbers are links that send themselves back to the robot.

## WeCom {#wecom}

A self-built app. Sending uses WeCom's server API and needs no public address. **Receiving commands, including the bind message, needs WeCom to reach SlotServe's callback URL** over the internet.

1. In the [WeCom admin console](https://work.weixin.qq.com/wework_admin/frame) create a self-built app under **App Management**. Note its **AgentId** and **Secret**, and set its visibility to the people who should use it.
2. Copy the **Corp ID** from **My Enterprise → Enterprise Information**.
3. In the app's settings add the SlotServe server's public outbound IP under **Trusted IP** (required for newer apps calling the API).
4. To receive messages, open the app's **Receive messages → Set API receiving**, generate a **Token** and an **EncodingAESKey**, and keep the page open.
5. Paste Corp ID, AgentId, Secret, Token and EncodingAESKey into **Admin → System settings → WeCom app**, switch it on and save.
6. Back in WeCom, enter the **callback URL** shown in **Admin → Chat channels** (`https://<your server>/api/channels/wecom/callback`) and save; WeCom checks it right away. WeCom may require the URL's domain to be a domain verified for your organisation.

Without the callback (step 4–6) the WeCom bot cannot receive the bind message, so people cannot link it; group webhooks (below) still work.

## QQ {#qq}

An official bot from the [QQ open platform](https://q.qq.com). SlotServe connects to QQ's WebSocket gateway and receives private (C2C) messages and group messages that @mention the bot.

1. Create a bot on the QQ open platform and copy **AppID** and **AppSecret** from **Development → Development settings**.
2. Add the SlotServe server's public outbound IP to the bot's **IP whitelist**.
3. Make sure the bot may be used in private chats and groups (message capabilities for single chat and group). While the bot is not published, add test users and groups in the sandbox configuration and switch on **Sandbox** in SlotServe.
4. Paste AppID and AppSecret into **Admin → System settings → QQ bot**, switch it on and save.

QQ only lets bots reply to a message for a limited time; answers to commands use that. Notifications are proactive messages, which QQ limits or refuses depending on the bot's permissions, so on QQ they may not arrive. QQ may also block links in messages unless the domain is registered with the bot.

## Group webhooks {#group-webhooks}

Group robots post into a group chat. They only send, need no public address, and are configured under **Admin → System settings → Group webhooks**:

- **Feishu / Lark**: in the group, **Settings → Bots → Add Bot → Custom Bot**. Copy the webhook (`https://open.feishu.cn/open-apis/bot/v2/hook/…`). If you turn on **Signature verification**, also paste its secret.
- **DingTalk**: in the group, **Group settings → Bots → Add Robot → Custom**. Choose the security setting **Additional signature** and paste its `SEC…` secret together with the webhook (`https://oapi.dingtalk.com/robot/send?access_token=…`). (Keyword security would reject most SlotServe messages.)
- **WeCom**: in the group chat, **Add group robot**, then copy its webhook (`https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…`). Its key is in the URL.

**Send to groups** chooses what the groups get: **Admin alerts** (load failures and fallbacks, new accounts, low disk), **Approval requests** (as text; approve on the web or in a private chat with the bot) and **Announcements**. **Admin → Chat channels** has a **Send test** button per webhook and a box to post an announcement.

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
