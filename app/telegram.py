"""Telegram Bot API: nudges out; links, files, photos and commands in."""

from __future__ import annotations

import html
import re

import httpx
from pydantic import BaseModel, Field

from app.config import settings

API = "https://api.telegram.org"
MAX_LEN = 4000


def esc(text: str) -> str:
    return html.escape(text, quote=False)


_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_CODE = re.compile(r"`([^`]+)`")


def rich(text: str) -> str:
    """Model output (light Markdown) to Telegram HTML: escape first, then bold, code and bullets."""
    out = esc(text)
    out = _BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", out)
    out = _CODE.sub(r"<code>\1</code>", out)
    return re.sub(r"^\s*[-*]\s+", "• ", out, flags=re.MULTILINE)


_TOKEN_IN_URL = re.compile(r"bot\d+:[A-Za-z0-9_-]+")


def redact(text: str) -> str:
    """Bot API URLs carry the token (/bot<token>/...); strip it from anything logged or shown."""
    return _TOKEN_IN_URL.sub("bot<redacted>", text)


class TelegramError(RuntimeError):
    pass


class Telegram:
    def __init__(self, token: str | None = None, client: httpx.AsyncClient | None = None):
        self.token = token or settings.telegram_token.get_secret_value()
        self.client = client or httpx.AsyncClient(timeout=30)

    @property
    def configured(self) -> bool:
        return bool(self.token)

    async def call(self, method: str, **params) -> dict:
        try:
            resp = await self.client.post(f"{API}/bot{self.token}/{method}", json=params)
            data = _Result.model_validate(resp.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise TelegramError(f"Telegram {method} failed: {type(exc).__name__}") from None
        if not data.ok:
            raise TelegramError(f"Telegram {method} failed: {data.description}")
        return data.result  # type: ignore[return-value]

    async def send(self, chat_id: int | str, text: str) -> None:
        for start in range(0, len(text), MAX_LEN):
            await self.call(
                "sendMessage",
                chat_id=chat_id,
                text=text[start : start + MAX_LEN],
                parse_mode="HTML",
                link_preview_options={"is_disabled": True},
            )

    async def download(self, file_id: str) -> tuple[str, bytes]:
        info = await self.call("getFile", file_id=file_id)
        path = info["file_path"]
        try:
            resp = await self.client.get(f"{API}/file/bot{self.token}/{path}")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise TelegramError(f"Telegram file download failed: {type(exc).__name__}") from None
        return path.rsplit("/", 1)[-1], resp.content

    async def set_webhook(self, url: str, secret: str) -> dict:
        return await self.call(
            "setWebhook", url=url, secret_token=secret, allowed_updates=["message"], drop_pending_updates=True
        )

    async def aclose(self) -> None:
        await self.client.aclose()


# --- Bot API wire models (only the fields we use) ------------------------------------------------

class Chat(BaseModel):
    id: int


class Sender(BaseModel):
    id: int
    first_name: str = ""
    last_name: str = ""
    username: str | None = None

    @property
    def display(self) -> str:
        name = " ".join(p for p in (self.first_name, self.last_name) if p)
        return f"{name} (@{self.username})" if self.username and name else name or (f"@{self.username}" if self.username else "")


class Document(BaseModel):
    file_id: str
    file_name: str | None = None
    mime_type: str | None = None


class PhotoSize(BaseModel):
    file_id: str
    width: int
    height: int


class Message(BaseModel):
    model_config = {"populate_by_name": True}

    message_id: int
    chat: Chat
    sender: Sender | None = Field(default=None, alias="from")
    text: str | None = None
    caption: str | None = None
    document: Document | None = None
    photo: list[PhotoSize] = Field(default_factory=list)


class Update(BaseModel):
    update_id: int
    message: Message | None = None
    edited_message: Message | None = None


class _Result(BaseModel):
    ok: bool
    description: str | None = None
    result: object | None = None


# --- What the agent acts on ----------------------------------------------------------------------

class Incoming(BaseModel):
    chat_id: int
    name: str = ""
    text: str = ""
    file_id: str | None = None
    file_name: str | None = None


def parse_update(payload: dict) -> Incoming | None:
    update = Update.model_validate(payload)
    msg = update.message or update.edited_message
    if not msg:
        return None
    text = msg.text or msg.caption or ""
    name = msg.sender.display if msg.sender else ""
    if msg.document:
        return Incoming(chat_id=msg.chat.id, name=name, text=text, file_id=msg.document.file_id, file_name=msg.document.file_name)
    if msg.photo:
        largest = max(msg.photo, key=lambda p: p.width * p.height)
        return Incoming(chat_id=msg.chat.id, name=name, text=text, file_id=largest.file_id, file_name="screenshot.jpg")
    return Incoming(chat_id=msg.chat.id, name=name, text=text)
