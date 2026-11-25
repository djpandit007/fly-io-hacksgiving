"""HTTP entry points. The Sprite URL is public (Telegram and the cron must reach it), so every
route except /health is guarded by a secret."""

from __future__ import annotations

import hashlib
import hmac
import logging
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel

from app import agent, bot, db, intake, ledger, mail, notify, users
from app.locks import locks
from app.agent import Clients
from app.config import settings
from app import sprite
from app.sprite import stay_awake
from app.telegram import Telegram, parse_update

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("fineprint")
# httpx logs every request URL at INFO, and Telegram URLs contain the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)

# One worker, one event loop, one SQLite connection. Jobs are ordered per member (chat:<id>) and per
# service (inside the agent), and only one daily run happens at a time ("tick").


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.conn = db.connect(settings.db_path)
    app.state.clients = Clients()
    app.state.tg = Telegram()
    yield
    await app.state.clients.aclose()
    await app.state.tg.aclose()
    app.state.conn.close()


app = FastAPI(title="Fine Print", lifespan=lifespan)


def _same(given: str | None, expected: str) -> bool:
    return bool(expected) and given is not None and hmac.compare_digest(given, expected)


def require_secret(x_fine_print_secret: Annotated[str | None, Header()] = None) -> None:
    if not _same(x_fine_print_secret, settings.shared_secret.get_secret_value()):
        raise HTTPException(status_code=401)


class TickSummary(BaseModel):
    checked: int
    failed: list[str]
    changes: int
    nudges: int
    members_notified: int
    sent: int


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/tick", dependencies=[Depends(require_secret)])
async def tick(request: Request) -> TickSummary:
    """Daily run. Called by the external cron, which keeps the connection open until done."""
    st = request.app.state
    async with locks.hold("tick"), stay_awake("tick", expire="10m"):
        result = await agent.tick(st.conn, st.clients)
        sent = await notify.deliver(st.conn, st.tg, st.clients, result.deliveries, "Fine Print: changes to terms you accepted")
    return TickSummary(
        checked=result.checked, failed=result.failed, changes=result.changes,
        nudges=result.nudges, members_notified=len(result.deliveries), sent=sent,
    )


async def _handle_message(app_: FastAPI, payload: dict, task: str) -> None:
    st = app_.state
    try:
        msg = parse_update(payload)
        if not msg:
            return
        # The hold was taken before the webhook answered; keep refreshing it while queued and working.
        async with stay_awake(task, expire="5m"), locks.hold(f"chat:{msg.chat_id}"):
            await bot.handle(st.conn, st.clients, st.tg, msg)
    finally:
        await sprite.release(task)


@app.post("/telegram")
async def telegram_webhook(
    request: Request,
    background: BackgroundTasks,
    x_telegram_bot_api_secret_token: Annotated[str | None, Header()] = None,
) -> dict[str, bool]:
    if not _same(x_telegram_bot_api_secret_token, settings.shared_secret.get_secret_value()):
        raise HTTPException(status_code=401)
    payload = await request.json()
    # Take the hold while this request still keeps the Sprite active, then answer Telegram quickly;
    # the work continues in the background under that hold.
    task = f"telegram-{payload.get('update_id', 'x')}"
    await sprite.hold(task, expire="5m")
    background.add_task(_handle_message, request.app, payload, task)
    return {"ok": True}


async def _handle_mail(app_: FastAPI, message_id: str, thread_id: str | None, sender: str, task: str) -> None:
    st = app_.state
    try:
        async with stay_awake(task, expire="5m"), locks.hold(f"email:{sender}"):
            await intake.process(st.conn, st.clients, st.tg, message_id, thread_id)
    except Exception as exc:
        log.warning("mail %s failed: %s", task, exc)
    finally:
        await sprite.release(task)


@app.post("/agentmail")
async def agentmail_webhook(
    request: Request,
    background: BackgroundTasks,
    svix_id: Annotated[str | None, Header(alias="svix-id")] = None,
    svix_timestamp: Annotated[str | None, Header(alias="svix-timestamp")] = None,
    svix_signature: Annotated[str | None, Header(alias="svix-signature")] = None,
) -> dict[str, bool]:
    """Inbound mail from AgentMail (Svix-signed). Accepts only message.received for our inbox."""
    body = await request.body()
    secret = settings.agentmail_webhook_secret.get_secret_value()
    if not mail.verify_signature(secret, svix_id or "", svix_timestamp or "", body, svix_signature or ""):
        raise HTTPException(status_code=401)
    event = mail.Event.model_validate_json(body)
    kind = event.event_type or event.type or ""
    msg = event.message
    if kind != "message.received" or msg is None:
        return {"ok": True}
    if msg.inbox_id and settings.agentmail_inbox_id and msg.inbox_id != settings.agentmail_inbox_id:
        return {"ok": True}
    conn = request.app.state.conn
    if conn.execute("INSERT OR IGNORE INTO mail_seen (message_id, seen_at) VALUES (?, ?)", (msg.message_id, db.now())).rowcount == 0:
        return {"ok": True}  # a retried delivery: processed already
    task = "mail-" + hashlib.sha256(msg.message_id.encode()).hexdigest()[:16]
    await sprite.hold(task, expire="5m")
    background.add_task(_handle_mail, request.app, msg.message_id, msg.thread_id, msg.sender, task)
    return {"ok": True}


@app.get("/calendar.ics")
async def calendar(request: Request, token: Annotated[str, Query()] = "") -> Response:
    user = users.by_calendar_token(request.app.state.conn, token)
    if user is None:
        raise HTTPException(status_code=401)
    body = ledger.to_ics(ledger.upcoming(request.app.state.conn, user.id), calendar_name=f"Fine Print — {user.label}")
    return PlainTextResponse(body, media_type="text/calendar; charset=utf-8")


@app.post("/admin/telegram-webhook", dependencies=[Depends(require_secret)])
async def register_webhook(request: Request) -> dict:
    if not settings.public_url:
        raise HTTPException(status_code=400, detail="FP_PUBLIC_URL is not set")
    await request.app.state.tg.set_webhook(f"{settings.public_url}/telegram", settings.shared_secret.get_secret_value())
    return {"webhook": f"{settings.public_url}/telegram"}
