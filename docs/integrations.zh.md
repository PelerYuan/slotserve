# 通知与集成

SlotServe 可以通过 Telegram 给大家发消息、把预约发布成日历订阅、把用量导出为 CSV，并提供 Prometheus 指标。这些都是可选功能，在**管理 → 系统设置**中配置。

## Telegram

机器人使用长轮询，服务器只需要能访问 Telegram，不需要公网地址或开放端口。

1. 在 Telegram 里找 [@BotFather](https://t.me/BotFather)，发送 `/newbot`，复制得到的 Token。
2. 粘贴到**管理 → 系统设置 → Telegram 机器人 Token** 并保存。如果网络无法直连 Telegram，同时填写 **Telegram 代理**（`http://…` 或 `socks5://…`）。
3. 每个人在**设置 → Telegram** 中点**绑定 Telegram**，Telegram 会打开机器人，点 **Start** 即完成绑定。
4. 用**发送测试**检查连接。每类通知都可以按人单独开关。

消息使用每个人界面的语言，时间按**时区**设置显示。

| 通知 | 接收人 |
| --- | --- |
| 预约 10 分钟后开始 | 预约人 |
| 模型已加载完成 / 整卡预约的 GPU 已空出 | 预约人 |
| 预约 10 分钟后结束，附**延长 30 分钟**、**延长 1 小时**、**立即结束**按钮 | 预约人 |
| 管理员替你预约、修改、取消或结束了你的预约 | 预约人 |
| 预约的模型加载失败，暂由默认模型提供服务 | 预约人和管理员 |
| 你的预约申请已通过、被拒绝（附理由）或已过期 | 预约人 |
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

用 `/book` 预约和网页遵守完全相同的规则：冲突检查、单次最长时长、最多提前天数、配额和审批。机器人提供整点开始时间（以及「现在」），其他时间、参数预设和每周重复请在网页上操作。如果确认时这个时间刚被别人约走，机器人会提供「加入候补」。在 Telegram 里输入 `/` 可以看到命令列表。

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
