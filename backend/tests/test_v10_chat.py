"""Chat platforms besides Telegram: Feishu/Lark, DingTalk, WeCom, QQ and group webhooks, against in-process fakes."""

import asyncio
import base64
import hashlib
import hmac
import json
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from conftest import make_gguf
from slotserve.db import SessionLocal, utcnow
from slotserve.orm import OutboxItem, User
from slotserve.services.chat.feishu import Frame, decode_frame, encode_frame
from slotserve.services.chat.wecom import WeComCrypto, WeComCryptoError

# The URL verification sample from WeCom's message encryption documentation.
WECOM_TOKEN = "QDG6eK"
WECOM_CORP = "wx5823bf96d3bd56c7"
WECOM_AES = "jWmYm7qr5nMoAUwZRjGtBxmz3KA1tkAj3ykkR6q2B2C"
WECOM_ECHO = "P9nAzCzyDtyTWESHep1vC5X9xho/qYX3Zpb4yKa9SKld1DsH3Iyt3tP3zNdtp+4RPcs8TgAE7OaBO+FZXvnaqQ=="
WECOM_ECHO_QUERY = {"msg_signature": "5c45ff5e21c57e6ad56bac8758b79b1d9ac89fd3", "timestamp": "1409659589"}


async def _wait(predicate, timeout=10.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return result
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met")


async def _setup(make_client, models_dir):
    admin = await make_client("root")
    bob = await make_client("bob")
    make_gguf(models_dir, "small.gguf")
    make_gguf(models_dir, "large.gguf")
    await admin.post("/api/models/scan")
    await bob.put("/api/auth/preferences", json={"locale": "en"})
    return admin, bob


async def _code(client, channel: str) -> str:
    r = await client.post(f"/api/channels/{channel}/link")
    assert r.status_code == 200, r.text
    return r.json()["code"]


def _card_buttons(card: dict) -> dict[str, str]:
    return {
        a["text"]["content"]: a["value"]["d"] for el in card["elements"] if el["tag"] == "action" for a in el["actions"]
    }


# ---- protocol pieces ------------------------------------------------------------------------


def test_feishu_frames_match_the_sdk_encoding():
    # Bytes produced by the official SDK's generated pbbp2.Frame for the same frames.
    event = bytes.fromhex(
        "08001000180720012a0d0a047479706512056576656e742a100a0a6d6573736167655f696412026d312a080a0373756d1201312a"
        "080a037365711201302a0e0a0874726163655f69641202743142077b2261223a317d"
    )
    ping = bytes.fromhex("08001000180720002a0c0a0474797065120470696e67")
    headers = [("type", "event"), ("message_id", "m1"), ("sum", "1"), ("seq", "0"), ("trace_id", "t1")]
    assert encode_frame(Frame(service=7, method=1, headers=headers, payload=b'{"a":1}')) == event
    assert encode_frame(Frame(service=7, method=0, headers=[("type", "ping")])) == ping
    decoded = decode_frame(event)
    assert decoded.service == 7 and decoded.method == 1 and decoded.payload == b'{"a":1}'
    assert decoded.header("message_id") == "m1" and decoded.headers == headers
    big = Frame(seq_id=2**40, log_id=300, service=1, method=1, payload=b"x" * 300, log_id_new="n")
    assert decode_frame(encode_frame(big)) == big


def test_wecom_crypto_official_vector():
    crypto = WeComCrypto(WECOM_TOKEN, WECOM_AES, WECOM_CORP)
    assert crypto.verify(WECOM_ECHO_QUERY["msg_signature"], "1409659589", "263014780", WECOM_ECHO)
    assert not crypto.verify(WECOM_ECHO_QUERY["msg_signature"], "1409659589", "263014781", WECOM_ECHO)
    assert crypto.decrypt(WECOM_ECHO) == "1616140317555161061"
    encrypted = crypto.encrypt("<xml><Content>你好</Content></xml>", random16=b"0123456789abcdef")
    assert crypto.decrypt(encrypted) == "<xml><Content>你好</Content></xml>"
    with pytest.raises(WeComCryptoError):
        WeComCrypto(WECOM_TOKEN, WECOM_AES, "another-corp").decrypt(WECOM_ECHO)


# ---- Feishu / Lark ----------------------------------------------------------------------------


async def test_feishu_link_commands_cards_and_booking(app, make_client, models_dir):
    fs = app.state.fake_chat.feishu
    admin, bob = await _setup(make_client, models_dir)
    assert (await bob.post("/api/channels/feishu/link")).json()["detail"]["code"] == "channel_unavailable"
    r = await admin.put(
        "/api/admin/settings",
        json={"feishu_enabled": True, "feishu_app_id": "cli_test", "feishu_app_secret": "fs-secret"},
    )
    assert r.status_code == 200
    await fs.wait_connected()
    await _wait(lambda: app.state.channels.get("feishu").connected)
    await _wait(lambda: fs.pings >= 1)  # the client pings right away
    listed = (await bob.get("/api/channels")).json()["channels"]
    assert [c["id"] for c in listed] == ["feishu"] and listed[0]["bot_name"] == "SlotBot"

    # unknown chat, wrong code, right code
    await fs.message("ou_bob", "/status")
    await _wait(lambda: any("not linked" in t for t in fs.texts("ou_bob")))
    await fs.message("ou_bob", "bind ZZZZZZ")
    await _wait(lambda: any("not valid" in t for t in fs.texts("ou_bob")))
    link = (await bob.post("/api/channels/feishu/link")).json()
    assert link["link_url"] == "https://applink.feishu.cn/client/bot/open?appId=cli_test"
    await fs.message("ou_bob", f"bind {link['code'].lower()}")
    await _wait(lambda: any("Linked to SlotServe account bob" in t for t in fs.texts("ou_bob")))
    me = next(c for c in (await bob.get("/api/channels")).json()["channels"] if c["id"] == "feishu")
    assert me["linked"] and me["notify"]

    # a command; long payloads arrive in several frames
    await fs.event(
        "im.message.receive_v1",
        {
            "sender": {"sender_id": {"open_id": "ou_bob"}},
            "message": {"message_type": "text", "content": json.dumps({"text": "status"}), "chat_type": "p2p"},
        },
        parts=3,
    )
    await _wait(lambda: any(t.startswith("Model:") for t in fs.texts("ou_bob")))

    # /book sends a card; pressing its buttons edits the card until the booking is made
    await fs.message("ou_bob", "/book")
    first = await _wait(lambda: next((m for m in fs.sent if m["msg_type"] == "interactive"), None))
    buttons = _card_buttons(first["content"])
    assert "Whole GPU (no model)" in buttons
    answer = await fs.press("ou_bob", buttons["large"])
    buttons = _card_buttons(answer["card"]["data"])
    answer = await fs.press("ou_bob", next(d for label, d in buttons.items() if label.startswith("Tomorrow")))
    answer = await fs.press("ou_bob", _card_buttons(answer["card"]["data"])["10:00"])
    answer = await fs.press("ou_bob", _card_buttons(answer["card"]["data"])["1 h"])
    answer = await fs.press("ou_bob", _card_buttons(answer["card"]["data"])["Book"])
    assert answer["card"]["data"]["elements"][0]["text"]["content"].startswith("Booked: large")
    mine = (await bob.get("/api/reservations?mine=true")).json()
    assert len(mine) == 1 and mine[0]["model_name"] == "large"

    # notifications reach Feishu
    await admin.delete(f"/api/reservations/{mine[0]['id']}")
    await _wait(lambda: any("cancelled your booking" in t for t in fs.texts("ou_bob")))

    # groups: read-only commands are answered in the group, personal ones are not
    await fs.message("ou_bob", "@_user_1 /mine", chat_type="group", chat_id="oc_team")
    await _wait(lambda: any("private chat" in t for t in fs.texts("oc_team")))
    await fs.message("ou_someone", "@_user_1 /today", chat_type="group", chat_id="oc_team")
    await _wait(lambda: any("No bookings today" in t for t in fs.texts("oc_team")))
    assert all(m["receive_id_type"] == "chat_id" for m in fs.sent if m["receive_id"] == "oc_team")

    # unlink from the web
    r = await bob.delete("/api/channels/feishu/link")
    assert r.json()["linked"] is False
    status = (await admin.get("/api/admin/channels")).json()
    feishu = next(c for c in status["channels"] if c["id"] == "feishu")
    assert feishu["connected"] and feishu["linked_count"] == 0 and feishu["bot_name"] == "SlotBot"


async def test_feishu_bad_credentials_show_in_overview(app, make_client):
    admin = await make_client("root")
    await admin.put(
        "/api/admin/settings", json={"feishu_enabled": True, "feishu_app_id": "cli_test", "feishu_app_secret": "nope"}
    )

    async def failed():
        channels = (await admin.get("/api/admin/channels")).json()["channels"]
        feishu = next(c for c in channels if c["id"] == "feishu")
        return feishu["error"] and not feishu["connected"]

    await _wait(failed)


# ---- DingTalk -------------------------------------------------------------------------------------


async def test_dingtalk_stream_link_numbered_choices_and_notify(app, make_client, models_dir):
    dt = app.state.fake_chat.dingtalk
    admin, bob = await _setup(make_client, models_dir)
    await admin.put(
        "/api/admin/settings",
        json={"dingtalk_enabled": True, "dingtalk_client_id": "ding-id", "dingtalk_client_secret": "ding-secret"},
    )
    await dt.wait_connected()
    assert dt.ticket == "tk/1+2"
    await dt.ping()
    await _wait(lambda: any(a["headers"]["messageId"] == "p1" and a["code"] == 200 for a in dt.acks))

    code = await _code(bob, "dingtalk")
    await dt.bot_message("staff-bob", f"绑定 {code}", nick="Bob D")
    await _wait(lambda: any("Linked to SlotServe" in r.get("text", {}).get("content", "") for r in dt.replies))
    ack = next(a for a in dt.acks if a["headers"]["messageId"].startswith("m"))
    assert ack["code"] == 200 and json.loads(ack["data"]) == {"response": None}
    me = next(c for c in (await bob.get("/api/channels")).json()["channels"] if c["id"] == "dingtalk")
    assert me["linked"] and me["account"] == "Bob D"

    # buttons become numbered markdown links that send the number back
    await dt.bot_message("staff-bob", "预约")
    reply = await _wait(lambda: next((r for r in dt.replies if r["msgtype"] == "markdown"), None))
    text = reply["markdown"]["text"]
    assert "dtmd://dingtalkclient/sendMessage?content=1" in text and "Whole GPU" in text
    whole = next(line for line in text.split("  \n") if "Whole GPU" in line)
    number = whole.split("[", 1)[1].split(")", 1)[0]
    before = len(dt.replies)
    await dt.bot_message("staff-bob", number)
    await _wait(lambda: len(dt.replies) > before and "which day" in dt.replies[-1]["markdown"]["text"])

    # in a group, personal commands are refused and nothing is numbered
    await dt.bot_message("staff-bob", "/mine", group=True)
    await _wait(lambda: any(r["conversation"] == "grp" for r in dt.replies))
    assert "private chat" in [r for r in dt.replies if r["conversation"] == "grp"][-1]["text"]["content"]

    # notifications use the robot's one-to-one API
    uid = (await bob.get("/api/auth/me")).json()["id"]
    app.state.notifier.emit("download_done", user_id=uid, filename="x.gguf")
    sent = await _wait(lambda: next((m for m in dt.sent if "x.gguf" in json.dumps(m)), None))
    assert sent["userIds"] == ["staff-bob"] and sent["robotCode"] == "ding-id" and sent["msgKey"] == "sampleText"


# ---- WeCom ----------------------------------------------------------------------------------------


async def _wecom_post(client, user_id: str, content: str, msg_id: str):
    crypto = WeComCrypto(WECOM_TOKEN, WECOM_AES, WECOM_CORP)
    inner = (
        f"<xml><ToUserName><![CDATA[{WECOM_CORP}]]></ToUserName><FromUserName><![CDATA[{user_id}]]></FromUserName>"
        f"<CreateTime>1700000000</CreateTime><MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{content}]]></Content><MsgId>{msg_id}</MsgId><AgentID>1000002</AgentID></xml>"
    )
    encrypted = crypto.encrypt(inner)
    signature = crypto.signature("1700000001", "nonce1", encrypted)
    body = f"<xml><ToUserName><![CDATA[{WECOM_CORP}]]></ToUserName><Encrypt><![CDATA[{encrypted}]]></Encrypt></xml>"
    params = {"msg_signature": signature, "timestamp": "1700000001", "nonce": "nonce1"}
    return await client.post("/api/channels/wecom/callback", params=params, content=body)


async def test_wecom_callback_link_command_and_reminder_choice(app, make_client, models_dir):
    wc = app.state.fake_chat.wecom
    admin, bob = await _setup(make_client, models_dir)
    anon = await make_client()
    query = {**WECOM_ECHO_QUERY, "nonce": "263014780", "echostr": WECOM_ECHO}
    assert (await anon.get("/api/channels/wecom/callback", params=query)).status_code == 403  # not set up yet
    await admin.put(
        "/api/admin/settings",
        json={
            "wecom_enabled": True,
            "wecom_corp_id": WECOM_CORP,
            "wecom_agent_id": "1000002",
            "wecom_secret": "wc-secret",
            "wecom_token": WECOM_TOKEN,
            "wecom_aes_key": WECOM_AES,
        },
    )
    await _wait(lambda: app.state.channels.get("wecom").connected)

    # URL verification with WeCom's sample values
    r = await anon.get("/api/channels/wecom/callback", params=query)
    assert r.status_code == 200 and r.text == "1616140317555161061"
    r = await anon.get("/api/channels/wecom/callback", params={**query, "nonce": "1"})
    assert r.status_code == 403
    bad = await anon.post(
        "/api/channels/wecom/callback",
        params={"msg_signature": "x", "timestamp": "1", "nonce": "1"},
        content="<xml><Encrypt>abc</Encrypt></xml>",
    )
    assert bad.status_code == 403

    code = await _code(bob, "wecom")
    r = await _wecom_post(anon, "bob.w", f"bind {code}", "1")
    assert r.status_code == 200 and r.text == ""
    await _wait(lambda: any("Linked to SlotServe account bob" in t for t in wc.texts("bob.w")))
    assert wc.sent[-1]["agentid"] == 1000002 and wc.sent[-1]["msgtype"] == "text"
    await _wecom_post(anon, "bob.w", f"bind {code}", "1")  # a retry of the same message is ignored
    await _wecom_post(anon, "bob.w", "状态", "2")
    await _wait(lambda: any(t.startswith("Model:") for t in wc.texts("bob.w")))

    # the reminder's buttons become numbered choices; "1" extends
    start = utcnow()
    r = await bob.post(
        "/api/reservations",
        json={"model_id": None, "start_at": start.isoformat(), "end_at": (start + timedelta(minutes=25)).isoformat()},
    )
    assert r.status_code == 201, r.text
    await app.state.housekeeper.tick(utcnow() + timedelta(minutes=16))
    reminder = await _wait(lambda: next((t for t in wc.texts("bob.w") if "ends at" in t), None))
    assert "1) +30 min" in reminder and "3) End now" in reminder and "Reply with a number" in reminder
    await _wecom_post(anon, "bob.w", "1", "3")
    await _wait(lambda: any(t.startswith("Extended until") for t in wc.texts("bob.w")))


# ---- QQ -------------------------------------------------------------------------------------------


async def test_qq_gateway_c2c_and_group(app, make_client, models_dir):
    qq = app.state.fake_chat.qq
    admin, bob = await _setup(make_client, models_dir)
    await admin.put(
        "/api/admin/settings", json={"qq_enabled": True, "qq_app_id": "qq-app", "qq_app_secret": "qq-secret"}
    )
    await _wait(lambda: app.state.channels.get("qq").connected)
    assert qq.identify["intents"] == 1 << 25 and qq.identify["token"] == "QQBot qq-token"
    assert app.state.channels.get("qq").bot_name == "SlotQQ"

    code = await _code(bob, "qq")
    await qq.c2c("u-bob", f"/bind {code}", msg_id="m1")
    await _wait(lambda: any("Linked to SlotServe account bob" in t for t in qq.texts("user:u-bob")))
    assert qq.sent[-1]["msg_id"] == "m1" and qq.sent[-1]["msg_seq"] == 1
    await qq.c2c("u-bob", "/mine", msg_id="m2")
    await _wait(lambda: any("no upcoming bookings" in t for t in qq.texts("user:u-bob")))
    await qq.group_at("g1", "member-x", "/book")
    await _wait(lambda: any("private chat" in t for t in qq.texts("group:g1")))
    await qq.group_at("g1", "member-x", "/status", msg_id="g2")
    await _wait(lambda: any(t.startswith("Model:") for t in qq.texts("group:g1")))

    uid = (await bob.get("/api/auth/me")).json()["id"]
    app.state.notifier.emit("download_done", user_id=uid, filename="q.gguf")
    sent = await _wait(lambda: next((m for m in qq.sent if "q.gguf" in m["content"]), None))
    assert sent["to"] == "user:u-bob" and "msg_id" not in sent


# ---- several channels, switches and webhooks ----------------------------------------------------------


async def test_notifications_go_to_every_linked_channel_unless_switched_off(app, make_client, models_dir):
    tg, wc = app.state.fake_telegram, app.state.fake_chat.wecom
    admin, bob = await _setup(make_client, models_dir)
    await admin.put(
        "/api/admin/settings",
        json={
            "telegram_bot_token": "1:x",
            "wecom_enabled": True,
            "wecom_corp_id": WECOM_CORP,
            "wecom_agent_id": "5",
            "wecom_secret": "wc-secret",
        },
    )
    await _wait(lambda: app.state.telegram.username and app.state.channels.get("wecom").connected)
    async with SessionLocal() as db:
        await db.execute(update(User).where(User.username == "bob").values(telegram_chat_id=321))
        await db.commit()
    code = await _code(bob, "wecom")
    async with SessionLocal() as db:
        user = (await db.execute(select(User).where(User.username == "bob"))).scalar_one()
        channel = app.state.channels.get("wecom")
        assert await channel.redeem(db, code, "bob.w", "bob.w") is not None
        await db.commit()
    channels = {c["id"]: c for c in (await bob.get("/api/channels")).json()["channels"]}
    assert channels["telegram"]["linked"] and channels["wecom"]["linked"]
    assert channels["wecom"]["receives"] is False  # no token / AES key: sending only

    app.state.notifier.emit("download_done", user_id=user.id, filename="a.gguf")
    await _wait(lambda: any("a.gguf" in t for t in tg.texts(321)) and any("a.gguf" in t for t in wc.texts("bob.w")))

    r = await bob.put("/api/channels/wecom", json={"notify": False})
    assert r.json()["notify"] is False
    app.state.notifier.emit("download_done", user_id=user.id, filename="b.gguf")
    await _wait(lambda: any("b.gguf" in t for t in tg.texts(321)))
    await asyncio.sleep(0.2)
    assert not any("b.gguf" in t for t in wc.texts("bob.w"))
    await bob.post("/api/channels/wecom/test", json={})
    assert any("WeCom" in t for t in wc.texts("bob.w"))

    # messages queued for a retry before chat channels existed were all Telegram messages
    async with SessionLocal() as db:
        payload = json.dumps({"chat_id": 321, "text": "legacy retry", "buttons": [[["+30 min", "ext:1:30"]]]})
        db.add(OutboxItem(kind="message", payload=payload, attempts=1, next_attempt_at=utcnow()))
        await db.commit()
    async with SessionLocal() as db:
        await app.state.notifier.retry_due(db)
    assert tg.sent[-1]["text"] == "legacy retry" and tg.sent[-1]["reply_markup"]["inline_keyboard"]


async def test_group_webhooks_signing_and_announcements(app, make_client):
    hook, dt, wc = app.state.fake_chat.feishu_hook, app.state.fake_chat.dingtalk, app.state.fake_chat.wecom
    admin = await make_client("root")
    r = await admin.put("/api/admin/settings", json={"group_events": "admin_alerts,nope"})
    assert r.json()["detail"]["code"] == "invalid_setting"
    r = await admin.put("/api/admin/settings", json={"dingtalk_webhook_url": "http://insecure"})
    assert r.json()["detail"]["code"] == "invalid_setting"
    await admin.put(
        "/api/admin/settings",
        json={
            "feishu_webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/fs-hook",
            "feishu_webhook_secret": "fs-sign",
            "dingtalk_webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=dt-hook",
            "dingtalk_webhook_secret": "SEC123",
            "wecom_webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=wc-hook",
            "group_events": "admin_alerts, approvals",
            "chat_default_locale": "zh-CN",
        },
    )
    await make_client("newbie")  # user_registered is an admin alert
    await _wait(lambda: hook.posts and dt.group and wc.group)
    assert "新用户注册：newbie" in hook.posts[0]["content"]["text"]
    assert "newbie" in dt.group[0]["text"]["content"] and wc.group[0]["key"] == "wc-hook"

    # Feishu: sign = base64(HMAC-SHA256(key="timestamp\nsecret", msg=""))
    ts = hook.posts[0]["timestamp"]
    expected = base64.b64encode(hmac.new(f"{ts}\nfs-sign".encode(), b"", hashlib.sha256).digest()).decode()
    assert hook.posts[0]["sign"] == expected
    # DingTalk: sign = urlencode(base64(HMAC-SHA256(key=secret, msg="timestamp\nsecret")))
    query = dt.group[0]["query"]
    assert query["access_token"] == "dt-hook"
    digest = hmac.new(b"SEC123", f"{query['timestamp']}\nSEC123".encode(), hashlib.sha256).digest()
    assert query["sign"] == base64.b64encode(digest).decode()  # sent URL-encoded, decoded by the server

    # announcements are off until selected
    r = await admin.post("/api/admin/channels/announce", json={"text": "Maintenance at 6pm"})
    assert r.json()["detail"]["code"] == "announcements_off"
    await admin.put("/api/admin/settings", json={"group_events": "announcements"})
    r = await admin.post("/api/admin/channels/announce", json={"text": "Maintenance at 6pm"})
    assert r.status_code == 202 and set(r.json()["targets"]) == {"feishu", "dingtalk", "wecom"}
    await _wait(lambda: any("Maintenance at 6pm" in p["content"]["text"] for p in hook.posts))

    r = await admin.post("/api/admin/channels/webhooks/dingtalk/test")
    assert r.json() == {"error": None}
    overview = (await admin.get("/api/admin/channels")).json()
    hooks = {h["id"]: h for h in overview["webhooks"]}
    assert hooks["dingtalk"]["signed"] and hooks["wecom"]["configured"] and hooks["feishu"]["last_ok_at"]
    assert overview["group_events"] == ["announcements"]
    assert (await (await make_client("eve")).get("/api/admin/channels")).status_code == 403
