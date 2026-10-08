"""Telegram over long polling: free, and no public URL or webhook needed.

The bot only ever talks to its paired owner. Every other sender is ignored,
which is the trust boundary: anyone can find a bot's username.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

API = "https://api.telegram.org"
POLL_SECONDS = 50
MESSAGE_LIMIT = 4096


class TelegramError(RuntimeError):
    pass


@dataclass(frozen=True)
class Inbound:
    update_id: int
    chat_id: str
    user_id: str
    text: str = ""
    message_id: str = ""
    callback_id: str = ""
    callback_data: str = ""
    user_name: str = ""

    @property
    def is_button(self) -> bool:
        return bool(self.callback_id)


def parse_update(update: dict[str, Any]) -> Inbound | None:
    update_id = int(update["update_id"])
    callback = update.get("callback_query")
    if callback:
        message = callback.get("message") or {}
        return Inbound(
            update_id=update_id,
            chat_id=str((message.get("chat") or {}).get("id") or ""),
            user_id=str((callback.get("from") or {}).get("id") or ""),
            message_id=str(message.get("message_id") or ""),
            callback_id=str(callback.get("id") or ""),
            callback_data=str(callback.get("data") or ""),
        )
    message = update.get("message")
    if not message:
        return None
    sender = message.get("from") or {}
    return Inbound(
        update_id=update_id,
        chat_id=str((message.get("chat") or {}).get("id") or ""),
        user_id=str(sender.get("id") or ""),
        text=str(message.get("text") or message.get("caption") or ""),
        message_id=str(message.get("message_id") or ""),
        user_name=str(sender.get("username") or sender.get("first_name") or ""),
    )


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    chunks: list[str] = []
    remaining = text.strip()
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = remaining.rfind(" ", 0, limit)
        if cut < 1:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


class TelegramBot:
    def __init__(self, token: str, *, client: httpx.AsyncClient | None = None) -> None:
        if not token:
            raise TelegramError("no Telegram bot token; run `gutsy setup`")
        self._base = f"{API}/bot{token}"
        self._client = client or httpx.AsyncClient(timeout=POLL_SECONDS + 15)

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, payload: dict[str, Any]) -> Any:
        response = await self._client.post(f"{self._base}/{method}", json=payload)
        body = response.json()
        if not body.get("ok"):
            raise TelegramError(f"{method}: {body.get('description') or response.status_code}")
        return body["result"]

    async def me(self) -> dict[str, Any]:
        return await self._call("getMe", {})

    async def updates(self, offset: int) -> list[dict[str, Any]]:
        return await self._call(
            "getUpdates",
            {"offset": offset, "timeout": POLL_SECONDS, "allowed_updates": ["message", "callback_query"]},
        )

    async def send(self, chat_id: str, text: str, *, buttons: list[tuple[str, str]] | None = None) -> str:
        """Send text (split at Telegram's limit); buttons attach to the last part."""
        chunks = split_message(text) or ["(empty)"]
        message_id = ""
        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk, "link_preview_options": {"is_disabled": True}}
            if buttons and index == len(chunks) - 1:
                payload["reply_markup"] = {
                    "inline_keyboard": [[{"text": label, "callback_data": data} for label, data in buttons]]
                }
            result = await self._call("sendMessage", payload)
            message_id = str(result["message_id"])
        return message_id

    async def typing(self, chat_id: str) -> None:
        await self._call("sendChatAction", {"chat_id": chat_id, "action": "typing"})

    async def answer_button(self, callback_id: str, text: str = "") -> None:
        await self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    async def settle_buttons(self, chat_id: str, message_id: str, outcome: str) -> None:
        """Replace an approval card's buttons with what was decided."""
        await self._call(
            "editMessageReplyMarkup",
            {
                "chat_id": chat_id,
                "message_id": int(message_id),
                "reply_markup": {"inline_keyboard": [[{"text": outcome, "callback_data": "noop"}]]},
            },
        )
