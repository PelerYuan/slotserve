"""Chat platforms: notifications, commands and booking for Telegram, Feishu/Lark, DingTalk, WeCom and QQ.

``commands`` and ``booking`` know nothing about any platform: they answer with text and buttons
(``Reply``). Each channel turns those into its own messages - inline keyboards, interactive cards,
or numbered choices on platforms without buttons.
"""
