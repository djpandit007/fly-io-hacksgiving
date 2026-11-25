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


class AuthResults(BaseModel):
    spf: str | None = None
    dkim: str | None = None
    dmarc: str | None = None
    spf_domain: str | None = None
    dkim_domain: str | None = None
    dmarc_domain: str | None = None


class Message(BaseModel):
    message_id: str
    inbox_id: str | None = None
    thread_id: str | None = None
    from_: str = Field("", alias="from")
    subject: str | None = None
    text: str | None = None
    html: str | None = None
    attachments: list[Attachment] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    authentication_results: AuthResults | None = None

    model_config = {"populate_by_name": True}

    @property
    def authenticated(self) -> bool:
        """The From address really sent this: DMARC, DKIM or SPF passed for the sender's own domain.

        From lines are trivially forged, so mail without an aligned pass is not acted on (fail closed
        when the provider gives no results)."""
        from app.discover import registrable_domain

        r = self.authentication_results
        if r is None or "@" not in self.sender:
            return False
        sender = registrable_domain(self.sender.rsplit("@", 1)[1])

        def aligned(result: str | None, domain: str | None) -> bool:
            return (result or "").lower() == "pass" and bool(domain) and registrable_domain(domain) == sender

        return aligned(r.dmarc, r.dmarc_domain) or aligned(r.dkim, r.dkim_domain) or aligned(r.spf, r.spf_domain)

    @property
    def suspicious(self) -> bool:
        """AgentMail marks mail that failed sender authentication or looks like spam."""
        return any(l.lower() in {"spam", "unauthenticated", "blocked", "suspicious"} for l in self.labels)

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


class Thread(BaseModel):
    thread_id: str
    messages: list[Message] = Field(default_factory=list)


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


async def respond(gw: Gateway, msg: Message, text: str) -> None:
    """Answer a received message. The connector refuses message-scoped endpoints (including reply) for
    many real Message-IDs (e.g. Gmail's, which contain + and =), so this sends a new "Re:" email."""
    subject = (msg.subject or "your email").strip()
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    await send(gw, msg.sender, subject, text)


async def get_message(gw: Gateway, message_id: str, thread_id: str | None) -> Message:
    """Fetch a received message via its thread (thread ids are UUIDs, which the connector accepts)."""
    if thread_id:
        thread = Thread.model_validate(await gw.get(f"{_inbox()}/threads/{quote(thread_id, safe='')}"))
        for m in thread.messages:
            if m.message_id == message_id:
                return m
        if thread.messages:
            return thread.messages[-1]
    return Message.model_validate(await gw.get(f"{_inbox()}/messages/{quote(message_id, safe='')}"))


async def download_attachment(gw: Gateway, msg: Message, attachment: Attachment) -> bytes | None:
    """Attachment bytes via its signed download URL (fetched through the public-address guard).

    None when it is too large or the connector refuses every path to it."""
    if attachment.size and attachment.size > MAX_ATTACHMENT_BYTES:
        return None
    aid = quote(attachment.attachment_id, safe="")
    paths = []
    if msg.thread_id:
        paths.append(f"{_inbox()}/threads/{quote(msg.thread_id, safe='')}/attachments/{aid}")
    paths.append(f"{_inbox()}/messages/{quote(msg.message_id, safe='')}/attachments/{aid}")
    link = None
    for path in paths:
        try:
            link = DownloadLink.model_validate(await gw.get(path))
            break
        except Exception:
            continue
    if link is None:
        return None
    async with safe_client(timeout=60) as client:
        resp = await client.get(link.download_url)
        resp.raise_for_status()
        if len(resp.content) > MAX_ATTACHMENT_BYTES:
            return None
        return resp.content

