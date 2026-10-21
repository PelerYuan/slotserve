# 通知与集成

SlotServe 可以在 Telegram、飞书 / Lark、钉钉、企业微信和 QQ 上给大家发消息、响应命令，把告警和公告发到群里，把预约发布成日历订阅，把用量导出为 CSV，并提供 Prometheus 指标。这些都是可选功能，在**管理 → 系统设置**中配置；**管理 → 聊天渠道**显示每个机器人是否已连接。

## 聊天平台

| 平台 | SlotServe 接收消息的方式 | 是否需要公网地址 | 按钮 |
| --- | --- | --- | --- |
| Telegram | 长轮询 | 否 | 内联按钮 |
| 飞书 / Lark | 长连接（WebSocket） | 否 | 消息卡片 |
| 钉钉 | Stream 模式（WebSocket） | 否 | 编号选项（点击即发送） |
| 企业微信 | 回调地址 | 仅接收命令时需要 | 编号选项 |
| QQ | WebSocket 网关 | 否 | 编号选项 |

所有平台提供相同的命令和通知，可以同时开启多个。一个人在绑定过的每个平台上都会收到通知；收哪些通知只需设置一次，对所有平台生效。

**绑定。** 每个人在**设置 → 聊天通知**中点平台旁的**绑定**。Telegram 会打开机器人，点 **Start** 即完成绑定。其他平台会显示一条类似 `bind K7P2QX` 的消息，30 分钟内在私聊里发给 SlotServe 机器人即可（也可以发 `绑定 K7P2QX`）。绑定码只能用一次；一个平台账号只能绑定一个 SlotServe 账号。**发送测试**检查连接，**通知**开关可以暂停某个平台，**解除绑定**（或在聊天里发 `/unlink`）取消绑定。

消息使用每个人界面的语言，时间按**时区**设置显示。群聊和尚未绑定的人收到的消息使用**群聊语言**。

| 通知 | 接收人 |
| --- | --- |
| 预约 10 分钟后开始 | 预约人 |
| 模型已加载完成 / 整卡预约的 GPU 已空出 | 预约人 |
| 预约 10 分钟后结束，附**延长 30 分钟**、**延长 1 小时**、**立即结束**按钮 | 预约人 |
| 管理员替你预约、修改、取消或结束了你的预约 | 预约人 |
| 预约的模型加载失败，暂由默认模型提供服务 | 预约人和管理员 |
| 你的预约申请已通过、被拒绝（附理由）或已过期 | 预约人 |
| 开发环境已就绪（附 ssh 命令）或没能启动 | 预约人 |
| 候补的时段空出来了，或已自动为你预约 | 候补人 |
| 你发起的下载完成或失败 | 下载发起人 |
| 有预约需要审批，附**通过**和**拒绝**按钮 | 管理员 |
| 模型加载失败、有新用户注册、模型目录磁盘不足 | 管理员 |

命令（与机器人私聊）：

| 命令 | 作用 |
| --- | --- |
| `/status` | 当前模型和谁在使用 GPU |
| `/book` | 用按钮预约：通道、模型（或整卡）、日期、开始时间、时长，最后确认 |
| `/free` | 各通道今天和明天的空闲时段 |
| `/cancel` | 选择一个未开始的预约并取消 |
| `/today` | 未来 24 小时的预约 |
| `/mine` | 我接下来的预约 |
| `/extend [分钟]` | 延长我正在进行的预约（默认 30） |
| `/release` | 立即结束我正在进行的预约 |
| `/unlink` | 解除与此聊天的绑定 |

用 `/book` 预约和网页遵守完全相同的规则：冲突检查、单次最长时长、最多提前天数、配额和审批。机器人提供整点开始时间（以及「现在」），其他时间、参数预设和每周重复请在网页上操作。如果确认时这个时间刚被别人约走，机器人会提供「加入候补」。

除 Telegram 外，命令也可以不带 `/`，或者用中文：`状态`、`预约`、`空闲`、`取消`、`今天`、`我的`、`延长`、`结束`、`解绑`。没有按钮的平台会把选项编号，回复编号即可；钉钉里的编号是链接，点一下就会发送。在群聊里（Telegram 除外），机器人只在被 @ 时回答 `/status`、`/free`、`/today` 和 `/help`；涉及个人预约的命令请在私聊里使用。

## Telegram

机器人使用长轮询，服务器只需要能访问 Telegram，不需要公网地址或开放端口。

1. 在 Telegram 里找 [@BotFather](https://t.me/BotFather)，发送 `/newbot`，复制得到的 Token。
2. 粘贴到**管理 → 系统设置 → Telegram 机器人 Token** 并保存。如果网络无法直连 Telegram，同时填写 **Telegram 代理**（`http://…` 或 `socks5://…`）。
3. 大家在**设置 → 聊天通知**里点 Telegram 旁的**绑定**。在 Telegram 里输入 `/` 可以看到命令列表。

## 飞书 / Lark {#feishu-lark}

开启机器人能力的企业自建应用。事件和卡片按钮回调都通过 SlotServe 主动建立的长连接接收，不需要公网地址。

1. 在开发者后台（[open.feishu.cn/app](https://open.feishu.cn/app)，Lark 为 [open.larksuite.com/app](https://open.larksuite.com/app)）创建**企业自建应用**，添加**机器人**能力。
2. 在**权限管理**中开通：获取与发送单聊、群组消息（`im:message`）、以应用的身份发消息（`im:message:send_as_bot`）、读取用户发给机器人的单聊消息（`im:message.p2p_msg:readonly`）、获取群组中所有 @机器人 的消息（`im:message.group_at_msg:readonly`）。
3. 把**凭证与基础信息**里的 **App ID** 和 **App Secret** 填到**管理 → 系统设置 → 飞书 / Lark 机器人**，打开开关并保存。Lark 需要把**开放平台地址**改为 `https://open.larksuite.com`。等**管理 → 聊天渠道**显示**已连接**。
4. 在**事件与回调**中，事件和回调都选择**使用长连接接收**（只有 SlotServe 已连接时后台才能保存），然后添加事件 `im.message.receive_v1`（接收消息）和回调 `card.action.trigger`（卡片回传交互）。
5. 创建版本，可用范围包含要使用机器人的人，然后发布。

## 钉钉 {#dingtalk}

使用 Stream 模式的企业内部应用机器人：由 SlotServe 主动连接钉钉，不需要公网地址。

1. 在[开发者后台](https://open-dev.dingtalk.com)创建**企业内部应用**，添加**机器人**能力，机器人的**消息接收模式**选择 **Stream 模式**。
2. 在**权限管理**中确认已开通企业内机器人发送消息权限（`qyapi_robot_sendmsg`），发送通知需要它。
3. 把**凭证与基础信息**里的 **Client ID**（AppKey）和 **Client Secret**（AppSecret）填到**管理 → 系统设置 → 钉钉机器人**，打开开关并保存。**机器人 RobotCode** 可以留空，默认使用 Client ID（企业内部应用通常如此）。
4. 发布应用版本并设置可见范围。大家按名字搜索机器人（或从应用中打开）并发送绑定消息。在群里使用时，把机器人加入群并 @ 它。

钉钉机器人消息在没有卡片模板时无法带回调按钮，所以选项会编号；编号是链接，点一下就会把编号发回给机器人。

## 企业微信 {#wecom}

企业微信自建应用。发送消息使用企业微信服务端 API，不需要公网地址；**接收命令（包括绑定消息）需要企业微信能通过互联网访问 SlotServe 的回调地址**。

1. 在[企业微信管理后台](https://work.weixin.qq.com/wework_admin/frame)的**应用管理**中创建自建应用，记下 **AgentId** 和 **Secret**，把可见范围设为要使用的人。
2. 在**我的企业 → 企业信息**复制**企业 ID**。
3. 在应用设置的**企业可信 IP** 中加入 SlotServe 服务器的公网出口 IP（较新的应用调用 API 时必须设置）。
4. 要接收消息，打开应用的**接收消息 → 设置 API 接收**，随机生成 **Token** 和 **EncodingAESKey**，先不要关闭页面。
5. 把企业 ID、AgentId、Secret、Token 和 EncodingAESKey 填到**管理 → 系统设置 → 企业微信应用**，打开开关并保存。
6. 回到企业微信，填写**管理 → 聊天渠道**里显示的**回调地址**（`https://<你的服务器>/api/channels/wecom/callback`）并保存，企业微信会立即校验。企业微信可能要求回调地址的域名是本企业已验证的域名。

没有配置回调（第 4–6 步）时，企业微信机器人收不到绑定消息，大家就无法绑定；下面的群机器人不受影响。

## QQ {#qq}

[QQ 开放平台](https://q.qq.com)的官方机器人。SlotServe 连接 QQ 的 WebSocket 网关，接收单聊（C2C）消息和群里 @机器人 的消息。

1. 在 QQ 开放平台创建机器人，从**开发 → 开发设置**复制 **AppID** 和 **AppSecret**。
2. 把 SlotServe 服务器的公网出口 IP 加入机器人的 **IP 白名单**。
3. 确认机器人可以在单聊和群聊中使用（单聊、群聊消息能力）。机器人上线前，在沙箱配置中添加测试用户和测试群，并在 SlotServe 中打开**沙箱环境**。
4. 把 AppID 和 AppSecret 填到**管理 → 系统设置 → QQ 机器人**，打开开关并保存。

QQ 只允许机器人在一段时间内回复用户的消息，命令的回答就使用这种被动回复。通知属于主动消息，QQ 会根据机器人的权限限制或拒绝，因此在 QQ 上可能收不到通知。消息里的链接也可能被拦截，除非域名已在机器人处登记。

## 群机器人 Webhook {#group-webhooks}

群机器人把消息发到群聊里。它们只发送、不需要公网地址，在**管理 → 系统设置 → 群机器人 Webhook** 中配置：

- **飞书 / Lark**：在群里打开**设置 → 群机器人 → 添加机器人 → 自定义机器人**，复制 Webhook 地址（`https://open.feishu.cn/open-apis/bot/v2/hook/…`）。如果开启了**签名校验**，同时填写签名密钥。
- **钉钉**：在群里打开**群设置 → 机器人 → 添加机器人 → 自定义**，安全设置选择**加签**，把 `SEC` 开头的密钥和 Webhook 地址（`https://oapi.dingtalk.com/robot/send?access_token=…`）一起填入。（使用「自定义关键词」会拒收大部分 SlotServe 消息。）
- **企业微信**：在群聊中**添加群机器人**，复制 Webhook 地址（`https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…`），密钥包含在地址里。

**发到群里**决定群里收到什么：**管理员告警**（模型加载失败与回退、新用户注册、磁盘不足）、**审批申请**（纯文本；在网页或与机器人的私聊里审批）和**公告**。**管理 → 聊天渠道**中每个 Webhook 都有**发送测试**按钮，还可以在那里发布公告。

## 日历订阅

**设置 → 日历订阅**提供两个私有 iCalendar 链接：我的预约、所有人的预约。在 Apple 日历、Google 日历、Outlook 或 Thunderbird 中以「订阅日历 / 网络日历」方式添加，日历应用会定期刷新（通常 15 分钟到几小时）。

拿到链接的人都能看到其中的预约。**重新生成链接**会让旧链接失效。如果大家访问 SlotServe 的地址与浏览器里显示的不同，请设置**访问地址**。

## CSV 导出

**用量 → 导出 CSV** 可下载所选时间段的按天汇总（日期、用户、模型、请求数、tokens）或逐条请求（时间、用户、API 密钥、模型、接口、状态码、tokens、耗时）。文件为带 BOM 的 UTF-8，Excel 可直接打开。

## Prometheus

设置**监控 Token** 后开启 `GET /metrics`，抓取时把它作为 Bearer Token：

```yaml
scrape_configs:
  - job_name: slotserve
    metrics_path: /metrics
    authorization:
      credentials: <监控 Token>
    static_configs:
      - targets: ["gpu-server:8000"]
```

| 指标 | 类型 | 标签 |
| --- | --- | --- |
| `slotserve_runtime_state` | gauge | `state` |
| `slotserve_runtime_model_info` | gauge | `model`、`preset` |
| `slotserve_reservation_active` | gauge | `user` |
| `slotserve_reservations_upcoming` | gauge | |
| `slotserve_requests_total` | counter | `user`、`model`、`outcome`（`ok` / `error`） |
| `slotserve_tokens_total` | counter | `user`、`model`、`type` |
| `slotserve_request_seconds_total` | counter | `user`、`model` |
| `slotserve_model_loads_total` | counter | `model`、`result` |
| `slotserve_model_load_seconds` | gauge | `model` |
| `slotserve_downloads_pending` | gauge | |
| `slotserve_gpu_memory_used_bytes`、`…_total_bytes`、`slotserve_gpu_utilization_ratio`、`slotserve_gpu_temperature_celsius`、`slotserve_gpu_power_watts` | gauge | `gpu`、`name` |

未设置 Token 时该接口返回 404。
