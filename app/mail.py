"""AgentMail: one shared inbox for every member.

- Outbound: verification codes, verdict replies and digests (send / reply).
- Inbound: a Svix-signed `message.received` webhook; the message is fetched and processed by
  `app.intake`. Only mail from a verified member address, or with the join code in the subject, is
  acted on; everything else is ignored without a reply.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from email.utils import parseaddr
from urllib.parse import quote

from pydantic import BaseModel, Field

from app.config import settings
from app.gateway import Gateway
from app.netguard import safe_client

MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024  # the connector gateway limit
SIGNATURE_TOLERANCE_S = 300


# --- Wire models -----------------------------------------------------------------------------------

class Attachment(BaseModel):
    attachment_id: str
    filename: str | None = None
    content_type: str | None = None
    size: int | None = None


class Message(BaseModel):
    message_id: str
    inbox_id: str | None = None
    thread_id: str | None = None
    from_: str = Field("", alias="from")
    subject: str | None = None
    text: str | None = None
    html: str | None = None
    attachments: list[Attachment] = Field(default_factory=list)

    model_config = {"populate_by_name": True}

    @property
    def sender(self) -> str:
        return parseaddr(self.from_)[1].strip().lower()

    @property
    def sender_name(self) -> str:
        return parseaddr(self.from_)[0].strip()


class Event(BaseModel):
    event_type: str = Field("", alias="event_type")
    type: str | None = None
    message: Message | None = None

    model_config = {"populate_by_name": True}


class DownloadLink(BaseModel):
    download_url: str


# --- Webhook authenticity --------------------------------------------------------------------------

def verify_signature(secret: str, msg_id: str, timestamp: str, body: bytes, signatures: str, now: float | None = None) -> bool:
    """Svix scheme: base64(HMAC-SHA256(secret, f"{id}.{timestamp}.{body}")) in a `v1,<sig>` list."""
    if not (secret and msg_id and timestamp and signatures):
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs((now if now is not None else time.time()) - ts) > SIGNATURE_TOLERANCE_S:
        return False
    key = base64.b64decode(secret.removeprefix("whsec_"))
    expected = base64.b64encode(hmac.new(key, f"{msg_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()).decode()
    for part in signatures.split():
        version, _, sig = part.partition(",")
        if version == "v1" and hmac.compare_digest(sig, expected):
            return True
    return False


# --- API -------------------------------------------------------------------------------------------

def _inbox() -> str:
    return f"v0/inboxes/{quote(settings.agentmail_inbox_id, safe='')}"


async def send(gw: Gateway, to: str, subject: str, text: str) -> None:
    await gw.post(f"{_inbox()}/messages/send", json={"to": [to], "subject": subject, "text": text})


async def reply(gw: Gateway, message_id: str, text: str) -> None:
    await gw.post(f"{_inbox()}/messages/{quote(message_id, safe='')}/reply", json={"text": text})


async def get_message(gw: Gateway, message_id: str) -> Message:
    return Message.model_validate(await gw.get(f"{_inbox()}/messages/{quote(message_id, safe='')}"))


async def download_attachment(gw: Gateway, message_id: str, attachment: Attachment) -> bytes | None:
    """Attachment bytes via its signed download URL (fetched through the public-address guard)."""
    if attachment.size and attachment.size > MAX_ATTACHMENT_BYTES:
        return None
    link = DownloadLink.model_validate(
        await gw.get(f"{_inbox()}/messages/{quote(message_id, safe='')}/attachments/{quote(attachment.attachment_id, safe='')}")
    )
    async with safe_client(timeout=60) as client:
        resp = await client.get(link.download_url)
        resp.raise_for_status()
        if len(resp.content) > MAX_ATTACHMENT_BYTES:
            return None
        return resp.content

