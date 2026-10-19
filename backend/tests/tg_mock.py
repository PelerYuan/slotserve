"""In-process stand-in for the Telegram Bot API."""

import asyncio

from fastapi import FastAPI, Request


class FakeTelegram:
    def __init__(self):
        self.app = FastAPI()
        self.updates: list[dict] = []
        self.sent: list[dict] = []
        self.answers: list[dict] = []
        self._next_id = 1

        @self.app.post("/bot{token}/{method}")
        async def call(token: str, method: str, request: Request):
            params = await request.json() if await request.body() else {}
            if method == "getMe":
                return {"ok": True, "result": {"id": 1, "is_bot": True, "username": "slot_test_bot"}}
            if method == "getUpdates":
                offset = params.get("offset", 0)
                pending = [u for u in self.updates if u["update_id"] >= offset]
                if not pending:
                    await asyncio.sleep(0.05)
                return {"ok": True, "result": pending}
            if method == "sendMessage":
                self.sent.append(params)
                return {"ok": True, "result": {"message_id": len(self.sent)}}
            if method == "answerCallbackQuery":
                self.answers.append(params)
                return {"ok": True, "result": True}
            return {"ok": False, "description": f"unknown method {method}"}

    def _push(self, update: dict) -> None:
        update["update_id"] = self._next_id
        self._next_id += 1
        self.updates.append(update)

    def message(self, chat_id: int, text: str, username: str = "tguser") -> None:
        self._push(
            {
                "message": {
                    "message_id": self._next_id,
                    "chat": {"id": chat_id, "type": "private"},
                    "from": {"id": chat_id, "username": username},
                    "text": text,
                }
            }
        )

    def button(self, chat_id: int, data: str) -> None:
        self._push(
            {"callback_query": {"id": str(self._next_id), "data": data, "message": {"chat": {"id": chat_id}}}}
        )

    def texts(self, chat_id: int) -> list[str]:
        return [m["text"] for m in self.sent if m["chat_id"] == chat_id]
