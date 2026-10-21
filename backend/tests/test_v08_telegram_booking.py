"""Booking, free time and cancelling from Telegram, with the same rules as the web UI."""

import asyncio

from sqlalchemy import update

from conftest import make_gguf
from slotserve.db import SessionLocal
from slotserve.orm import User, WaitlistEntry
from slotserve.services.telegram_booking import Reply


async def _link(username: str, chat_id: int) -> None:
    async with SessionLocal() as db:
        await db.execute(update(User).where(User.username == username).values(telegram_chat_id=chat_id, locale="en"))
        await db.commit()


def _data(reply: Reply, label: str) -> str:
    for row in reply.buttons:
        for text, data in row:
            if text == label:
                return data
    raise AssertionError(f"no button {label!r} in {reply.buttons}")


def _labels(reply: Reply) -> list[str]:
    return [text for row in reply.buttons for text, _ in row]


async def test_book_free_cancel_and_waitlist(app, make_client, models_dir):
    bot = app.state.telegram
    admin = await make_client("root")
    bob = await make_client("bob")
    await make_client("eve")
    make_gguf(models_dir, "small.gguf")
    make_gguf(models_dir, "large.gguf")
    await admin.post("/api/models/scan")
    await _link("bob", 900)
    await _link("eve", 901)

    # /book: one lane, so it starts with the model
    r = await bot.on_command(900, None, "/book", "")
    assert isinstance(r, Reply) and "Whole GPU (no model)" in _labels(r) and "large" in _labels(r)
    r = await bot.on_button(900, _data(r, "large"))
    day_label = next(label for label in _labels(r) if label.startswith("Tomorrow"))
    r = await bot.on_button(900, _data(r, day_label))
    assert "10:00" in _labels(r) and "Now" not in _labels(r)
    r = await bot.on_button(900, _data(r, "10:00"))
    assert "2 h" in _labels(r)
    r = await bot.on_button(900, _data(r, "2 h"))
    assert "10:00–12:00" in r.text and "approve" not in r.text
    payload = _data(r, "Book")
    r = await bot.on_button(900, payload)
    assert r.text.startswith("Booked: large") and r.edit

    mine = (await bob.get("/api/reservations?mine=true")).json()
    assert len(mine) == 1 and mine[0]["model_name"] == "large" and mine[0]["status"] == "booked"

    # /free shows the gap around it
    free = await bot.on_command(901, None, "/free", "")
    tomorrow_line = next(line for line in free.text.splitlines() if line.startswith("Tomorrow"))
    assert "00:00–10:00" in tomorrow_line and "12:00–24:00" in tomorrow_line

    # eve: the booked hours are not offered, and a stale button offers the waitlist
    r = await bot.on_button(901, payload.replace("bko", "bk").rsplit(":", 2)[0])
    assert "10:00" not in _labels(r) and "11:00" not in _labels(r) and "12:00" in _labels(r)
    taken = await bot.on_button(901, payload)
    assert "just taken" in taken.text and "bob" in taken.text
    r = await bot.on_button(901, _data(taken, "Join waitlist"))
    assert "waitlist" in r.text
    async with SessionLocal() as db:
        entries = (await db.execute(WaitlistEntry.__table__.select())).all()
    assert len(entries) == 1

    # approval mode: the bot says so and files a request
    await admin.put("/api/admin/settings", json={"approval_mode": "all"})
    r = await bot.on_button(901, payload.replace("bko", "bk").replace("1000", "1400"))
    assert "admin has to approve" in r.text
    r = await bot.on_button(901, _data(r, "Book"))
    assert r.text.startswith("Requested")

    # /cancel
    r = await bot.on_command(900, None, "/cancel", "")
    [[(label, data)]] = r.buttons
    assert "large" in label and data.startswith("cx:")
    r = await bot.on_button(900, data)
    assert "Cancel large" in r.text
    r = await bot.on_button(900, _data(r, "Yes, cancel"))
    assert r.text.startswith("Cancelled")
    assert (await bob.get("/api/reservations?mine=true")).json() == []
    none = await bot.on_command(900, None, "/cancel", "")
    assert "no upcoming bookings" in none.text

    # unlinked chats get nothing
    assert "not linked" in await bot.on_button(999, payload)


async def test_buttons_edit_the_message(app, make_client, models_dir):
    tg = app.state.fake_telegram
    admin = await make_client("root")
    await make_client("bob")
    make_gguf(models_dir, "small.gguf")
    await admin.post("/api/models/scan")
    await admin.put("/api/admin/settings", json={"telegram_bot_token": "123:abc"})
    await _link("bob", 900)
    bot = app.state.telegram

    async def menu_set():
        for _ in range(100):
            if tg.commands:
                return
            await asyncio.sleep(0.05)

    await menu_set()
    assert {c["command"] for c in tg.commands[""]} >= {"book", "free", "cancel", "status"}
    assert any(c["description"] == "预约 GPU 时段" for c in tg.commands["zh"])
    await bot.handle({"message": {"chat": {"id": 900, "type": "private"}, "text": "/book"}})
    assert tg.sent[-1]["reply_markup"]["inline_keyboard"]
    first = tg.sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    await bot.handle({"callback_query": {"id": "1", "data": first, "message": {"chat": {"id": 900}, "message_id": 7}}})
    assert tg.edits and tg.edits[-1]["message_id"] == 7 and "which day" in tg.edits[-1]["text"]
