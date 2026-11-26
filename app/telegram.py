"""Telegram Bot API: nudges out; links, files, photos and commands in."""

from __future__ import annotations

import html
import re
import time
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from app.config import settings
from app.gateway import Recorder

API = "https://api.telegram.org"
MAX_LEN = 4000

# Inline keyboard: rows of (label, callback data). Callback data is at most 64 bytes (Telegram's limit).
Buttons = list[list[tuple[str, str]]]
CALLBACK_DATA_MAX = 64


def keyboard(buttons: Buttons | None) -> dict:
    """Inline keyboard markup; an empty or missing keyboard removes a message's buttons."""
    return {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row] for row in buttons or []]}


def esc(text: str) -> str:
    return html.escape(text, quote=False)


_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_CODE = re.compile(r"`([^`]+)`")


def rich(text: str) -> str:
    """Model output (light Markdown) to Telegram HTML: escape first, then bold, code and bullets."""
    # Code spans are set aside first so bold never opens inside one and closes outside it: Telegram
    # rejects a whole message whose tags are mis-nested.
    spans: list[str] = []

    def hold(m: re.Match[str]) -> str:
        spans.append(f"<code>{m.group(1)}</code>")
        return f"\x00{len(spans) - 1}\x00"

    out = _CODE.sub(hold, esc(text))
    out = _BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", out)
    out = re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], out)
    return re.sub(r"^\s*[-*]\s+", "• ", out, flags=re.MULTILINE)


_TOKEN_IN_URL = re.compile(r"bot\d+:[A-Za-z0-9_-]+")


def redact(text: str) -> str:
    """Bot API URLs carry the token (/bot<token>/...); strip it from anything logged or shown."""
    return _TOKEN_IN_URL.sub("bot<redacted>", text)


def split(text: str, limit: int = MAX_LEN) -> list[str]:
    """Message-sized chunks, cut between paragraphs where possible so HTML tags stay whole.

    Paragraphs (separated by a blank line) are packed greedily; one longer than `limit` is cut by
    characters as a last resort.
    """
    chunks: list[str] = []
    for para in text.split("\n\n"):
        pieces = [para[i : i + limit] for i in range(0, len(para), limit)] or [""]
        for piece in pieces:
            if chunks and len(chunks[-1]) + 2 + len(piece) <= limit:
                chunks[-1] += "\n\n" + piece
            else:
                chunks.append(piece)
    return [c for c in chunks if c]


class TelegramError(RuntimeError):
    pass


class Telegram:
    def __init__(self, token: str | None = None, client: httpx.AsyncClient | None = None):
        self.token = token or settings.telegram_token.get_secret_value()
        self.client = client or httpx.AsyncClient(timeout=30)
        self.recorder: Recorder | None = None  # set by the app; counts calls and failures as provider "telegram"

    @property
    def configured(self) -> bool:
        return bool(self.token)

    async def call(self, method: str, **params) -> dict:
        started = time.monotonic()
        try:
            result = await self._call(method, **params)
        except TelegramError:
            self._count("error", started)
            raise
        self._count("ok", started)
        return result

    def _count(self, outcome: str, started: float) -> None:
        if self.recorder is not None:
            self.recorder.count("provider", "telegram", outcome, int((time.monotonic() - started) * 1000))

    async def _call(self, method: str, **params) -> dict:
        try:
            resp = await self.client.post(f"{API}/bot{self.token}/{method}", json=params)
            data = _Result.model_validate(resp.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise TelegramError(f"Telegram {method} failed: {type(exc).__name__}") from None
        if not data.ok:
            raise TelegramError(f"Telegram {method} failed: {data.description}")
        return data.result  # type: ignore[return-value]

    async def send(
        self,
        chat_id: int | str,
        text: str,
        *,
        buttons: Buttons | None = None,
        reply_to: int | None = None,
        markup: dict | None = None,
    ) -> list[int]:
        """Send text (split at MAX_LEN) and return the sent message ids.

        `reply_to` threads the first chunk under that message; `buttons` (or a raw `markup`, such as a
        force-reply prompt) goes on the last chunk only.
        """
        chunks = split(text)
        ids: list[int] = []
        for i, chunk in enumerate(chunks):
            params: dict = {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
            }
            if i == 0 and reply_to:
                params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
            if i == len(chunks) - 1:
                if buttons:
                    params["reply_markup"] = keyboard(buttons)
                elif markup:
                    params["reply_markup"] = markup
            result = await self.call("sendMessage", **params)
            if isinstance(result, dict) and isinstance(result.get("message_id"), int):
                ids.append(result["message_id"])
        return ids

    async def answer_callback(self, callback_id: str, text: str = "", alert: bool = False) -> None:
        """Stop a tapped button's spinner, optionally with a toast (or a pop-up when `alert`)."""
        params: dict = {"callback_query_id": callback_id}
        if text:
            params |= {"text": text[:200], "show_alert": alert}
        await self.call("answerCallbackQuery", **params)

    async def edit_buttons(self, chat_id: int, message_id: int, buttons: Buttons | None) -> None:
        """Replace a message's inline buttons; None or [] removes them."""
        await self.call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id, reply_markup=keyboard(buttons))

    async def react(self, chat_id: int, message_id: int, emoji: str) -> None:
        await self.call(
            "setMessageReaction", chat_id=chat_id, message_id=message_id,
            reaction=[{"type": "emoji", "emoji": emoji}],
        )

    async def draft(self, chat_id: int, draft_id: int, text: str = "") -> None:
        """A temporary preview while work runs; empty text shows Telegram's "Thinking..." placeholder."""
        await self.call("sendMessageDraft", chat_id=chat_id, draft_id=draft_id, text=text)

    async def typing(self, chat_id: int) -> None:
        await self.call("sendChatAction", chat_id=chat_id, action="typing")

    async def set_commands(self, commands: list[tuple[str, str]], scope: dict) -> None:
        await self.call(
            "setMyCommands", commands=[{"command": c, "description": d} for c, d in commands], scope=scope
        )

    async def set_description(self, description: str, short: str) -> None:
        await self.call("setMyDescription", description=description)
        await self.call("setMyShortDescription", short_description=short)

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
            "setWebhook", url=url, secret_token=secret, allowed_updates=["message", "callback_query"], drop_pending_updates=True
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


class RepliedTo(BaseModel):
    """The message a member's message replies to (only what we use: its text)."""

    message_id: int
    text: str | None = None


class Message(BaseModel):
    model_config = {"populate_by_name": True}

    message_id: int
    chat: Chat
    sender: Sender | None = Field(default=None, alias="from")
    text: str | None = None
    caption: str | None = None
    document: Document | None = None
    photo: list[PhotoSize] = Field(default_factory=list)
    reply_to_message: RepliedTo | None = None


class _Button(BaseModel):
    text: str = ""
    callback_data: str | None = None


class _Markup(BaseModel):
    inline_keyboard: list[list[_Button]] = Field(default_factory=list)


class TappedMessage(BaseModel):
    """The message a tapped button sits on. Old messages arrive "inaccessible", with only id and chat."""

    message_id: int
    chat: Chat
    reply_markup: _Markup | None = None

    def buttons(self) -> Buttons:
        rows = self.reply_markup.inline_keyboard if self.reply_markup else []
        return [[(b.text, b.callback_data) for b in row if b.callback_data is not None] for row in rows]


class CallbackQuery(BaseModel):
    model_config = {"populate_by_name": True}

    id: str
    sender: Sender = Field(alias="from")
    message: TappedMessage | None = None
    data: str | None = None


class Update(BaseModel):
    update_id: int
    message: Message | None = None
    edited_message: Message | None = None
    callback_query: dict | None = None


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
    kind: Literal["message", "tap"] = "message"
    message_id: int | None = None      # the member's message, or the message the tapped button is on
    callback_id: str | None = None     # taps only: answered exactly once
    data: str = ""                     # taps only: untrusted button data
    reply_to_text: str | None = None   # messages only: text of the message this one replies to
    buttons: Buttons = Field(default_factory=list)  # taps only: the tapped message's current buttons


def _tap(raw: dict) -> Incoming | None:
    try:
        q = CallbackQuery.model_validate(raw)
    except ValueError:
        return None
    # A button in a private chat sits in the tapper's own chat, whose id is the tapper's user id.
    chat_id = q.message.chat.id if q.message else q.sender.id
    return Incoming(
        kind="tap", chat_id=chat_id, name=q.sender.display, callback_id=q.id, data=q.data or "",
        message_id=q.message.message_id if q.message else None,
        buttons=q.message.buttons() if q.message else [],
    )


def parse_update(payload: dict) -> Incoming | None:
    update = Update.model_validate(payload)
    if update.callback_query is not None:
        return _tap(update.callback_query)
    msg = update.message or update.edited_message
    if not msg:
        return None
    base = {
        "chat_id": msg.chat.id,
        "name": msg.sender.display if msg.sender else "",
        "text": msg.text or msg.caption or "",
        "message_id": msg.message_id,
        "reply_to_text": msg.reply_to_message.text if msg.reply_to_message else None,
    }
    if msg.document:
        return Incoming(**base, file_id=msg.document.file_id, file_name=msg.document.file_name)
    if msg.photo:
        largest = max(msg.photo, key=lambda p: p.width * p.height)
        return Incoming(**base, file_id=largest.file_id, file_name="screenshot.jpg")
    return Incoming(**base)
