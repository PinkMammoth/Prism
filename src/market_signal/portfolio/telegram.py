"""Telegram delivery for the daily brief (free Bot API; no paid infrastructure).

Setup: create a bot with @BotFather, put its token in ``.env`` as TELEGRAM_BOT_TOKEN,
send the bot any message, then run ``market telegram-setup`` to find your chat id and
store it as TELEGRAM_CHAT_ID. Messages go only to that chat.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from market_signal.config import Settings

API = "https://api.telegram.org"
MAX_LEN = 4096  # Telegram's message limit


class TelegramError(RuntimeError):
    pass


@dataclass
class TelegramClient:
    token: str
    chat_id: str | None = None
    timeout: float = 20.0
    transport: httpx.BaseTransport | None = None  # injectable for tests

    @classmethod
    def from_settings(cls, settings: Settings, require_chat: bool = True) -> TelegramClient:
        token = settings.secret("TELEGRAM_BOT_TOKEN")
        chat = settings.secret("TELEGRAM_CHAT_ID")
        if not token:
            raise TelegramError(
                "TELEGRAM_BOT_TOKEN is not set in .env (create a bot with @BotFather)"
            )
        if require_chat and not chat:
            raise TelegramError("TELEGRAM_CHAT_ID is not set in .env (run `market telegram-setup`)")
        return cls(token, chat)

    def _call(self, method: str, payload: dict | None = None) -> dict:
        try:
            with httpx.Client(timeout=self.timeout, transport=self.transport) as c:
                r = c.post(f"{API}/bot{self.token}/{method}", json=payload or {})
        except httpx.HTTPError as exc:  # never echo the URL: it contains the token
            raise TelegramError(f"Telegram unreachable: {type(exc).__name__}") from None
        try:
            body = r.json()
        except ValueError:
            raise TelegramError(f"Telegram returned HTTP {r.status_code}") from None
        if not body.get("ok"):
            raise TelegramError(
                f"Telegram error {r.status_code}: {body.get('description', 'unknown')}"
            )
        return body.get("result") or {}

    def send(self, html_text: str) -> None:
        """Send an HTML-formatted message (split if it exceeds Telegram's limit)."""
        if not self.chat_id:
            raise TelegramError("no chat id")
        for chunk in _chunks(html_text):
            self._call("sendMessage", {"chat_id": self.chat_id, "text": chunk, "parse_mode": "HTML",
                                       "disable_web_page_preview": True})  # fmt: skip

    def recent_chats(self) -> list[dict]:
        """Chats that have messaged the bot recently (for finding your chat id)."""
        seen: dict[str, dict] = {}
        for u in self._call("getUpdates") or []:
            msg = u.get("message") or u.get("channel_post") or {}
            chat = msg.get("chat") or {}
            if "id" in chat:
                name = chat.get("username") or chat.get("title") or chat.get("first_name") or ""
                seen[str(chat["id"])] = {
                    "id": str(chat["id"]),
                    "name": name,
                    "type": chat.get("type"),
                }
        return list(seen.values())


def _chunks(text: str, limit: int = MAX_LEN) -> list[str]:
    """Split on line boundaries so HTML tags (always within one line here) stay intact."""
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > limit and cur:
            out.append(cur)
            cur = ""
        cur += line[:limit]
    if cur:
        out.append(cur)
    return out
