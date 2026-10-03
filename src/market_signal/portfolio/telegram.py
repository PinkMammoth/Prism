"""Telegram delivery for the daily brief (free Bot API; no paid infrastructure).

Setup: create a bot with @BotFather, put its token in ``.env`` as TELEGRAM_BOT_TOKEN,
send the bot any message, then run ``market telegram-setup`` to find your chat id and
store it as TELEGRAM_CHAT_ID. Messages go only to that chat.
"""

from __future__ import annotations

import re
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
        token = (settings.secret("TELEGRAM_BOT_TOKEN") or "").strip().strip("\"'")
        chat = (settings.secret("TELEGRAM_CHAT_ID") or "").strip().strip("\"'") or None
        if not token:
            raise TelegramError(
                "TELEGRAM_BOT_TOKEN is not set in .env (create a bot with @BotFather)"
            )
        check_token(token)
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
            raise TelegramError(_explain(r.status_code, str(body.get("description", "unknown"))))
        return body.get("result") or {}

    def get_me(self) -> dict:
        """The bot behind this token (verifies the token)."""
        return self._call("getMe")

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


TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
TOKEN_HELP = (
    "It should look like 123456789:AAH… (digits, a colon, then about 35 letters, digits, '_' or "
    "'-'), on one line, with no 'bot' prefix, brackets or spaces. BotFather shows it again under "
    "/mybots → your bot → API Token."
)


def check_token(token: str) -> None:
    """Reject malformed tokens before calling Telegram, without ever echoing the token."""
    if TOKEN_RE.match(token):
        return
    if token.lower().startswith("bot") and TOKEN_RE.match(token[3:]):
        hint = "it starts with 'bot'; remove that prefix (Prism adds it)"
    elif token.startswith("@") or ":" not in token:
        hint = "it has no ':'; this looks like a bot username, not the API token"
    elif any(c in token for c in "<> \t"):
        hint = "it contains brackets or spaces"
    else:
        hint = f"it doesn't have the expected shape (length {len(token)})"
    raise TelegramError(f"TELEGRAM_BOT_TOKEN in .env looks malformed: {hint}. {TOKEN_HELP}")


def _explain(status: int, description: str) -> str:
    if status == 404:
        return ("Telegram says this bot token doesn't exist (HTTP 404). The token in .env is probably "
                f"incomplete or mistyped. {TOKEN_HELP}")  # fmt: skip
    if status == 401:
        return ("Telegram rejected the bot token (HTTP 401 Unauthorized): it may have been revoked or "
                "regenerated. Copy the current one from BotFather: /mybots → your bot → API Token.")  # fmt: skip
    if status == 400 and "chat not found" in description.lower():
        return ("Telegram can't find that chat (HTTP 400: chat not found). Check TELEGRAM_CHAT_ID, and "
                "make sure you've pressed Start in your bot's chat.")  # fmt: skip
    if status == 403:
        return f"Telegram refused to deliver (HTTP 403: {description}). Did you block the bot? Press Start in its chat."
    return f"Telegram error {status}: {description}"


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
