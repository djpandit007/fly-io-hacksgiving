"""HTTP entry points. The Sprite URL is public (Telegram and the cron must reach it), so every
route here except /health is guarded by a secret. The web pages in app/web.py are either public and
read-only over public data, or behind a Control Room session."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
import time
from contextlib import AbstractAsyncContextManager, asynccontextmanager, nullcontext
from typing import Annotated

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
)
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel

from app import (
    agent,
    bot,
    db,
    feedback,
    intake,
    ledger,
    mail,
    metrics,
    notify,
    sprite,
    taps,
    users,
    web,
)
from app.agent import Clients
from app.config import settings
from app.locks import locks
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
    recorder = metrics.Recorder(app.state.conn)
    app.state.clients = Clients(recorder)
    app.state.tg = Telegram()
    app.state.tg.recorder = recorder
    # Description and command menus: best-effort, in the background so a slow Telegram never delays startup.
    app.state.setup = asyncio.create_task(bot.setup(app.state.conn, app.state.tg)) if app.state.tg.configured else None
    yield
    if app.state.setup:
        app.state.setup.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await app.state.setup
    await app.state.clients.aclose()
    await app.state.tg.aclose()
    app.state.conn.close()


app = FastAPI(title="Fine Print", lifespan=lifespan)
# The public Terms Radar, change inspector and swarm stream, and the member Control Room (app/web.py).
app.include_router(web.router)


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
    undelivered: int


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/tick", dependencies=[Depends(require_secret)])
async def tick(request: Request) -> TickSummary:
    """Daily run. Called by the external cron, which keeps the connection open until done."""
    st = request.app.state
    async with locks.hold("tick"), stay_awake("tick", expire="10m"):
        started = time.monotonic()
        result = await agent.tick(st.conn, st.clients)
        delivered = await notify.deliver(st.conn, st.tg, st.clients, result.deliveries, "Fine Print: changes to terms you accepted")
        metrics.run(
            st.conn, duration_ms=metrics.elapsed_ms(started), checked=result.checked, failed=result.failed,
            changes=result.changes, nudges=result.nudges, sent=delivered.sent, undelivered=delivered.failed,
        )
        metrics.prune(st.conn)
        feedback.prune(st.conn)
    return TickSummary(
        checked=result.checked, failed=result.failed, changes=result.changes, nudges=result.nudges,
        members_notified=len(result.deliveries), sent=delivered.sent, undelivered=delivered.failed,
    )


async def _handle_message(app_: FastAPI, payload: dict, task: str) -> None:
    st = app_.state
    started = time.monotonic()
    claimed: tuple[int, int] | None = None
    try:
        msg = parse_update(payload)
        if not msg:
            return
        chat_lock: AbstractAsyncContextManager = locks.hold(f"chat:{msg.chat_id}")
        if msg.kind == "tap":
            member = users.active(users.by_chat(st.conn, msg.chat_id))
            if member is None or not taps.is_slow(msg.data):
                # Quick taps (a lookup or one write) don't wait behind a long job in the same chat, and
                # a non-member's tap is answered with the private-bot notice straight away.
                chat_lock = nullcontext()
            else:
                if msg.message_id:
                    if (msg.chat_id, msg.message_id) in _claimed:
                        # A second tap on the same button while the first is queued or running.
                        await _answer_quietly(st.tg, msg.callback_id, "Already on it.")
                        return
                    claimed = (msg.chat_id, msg.message_id)
                    _claimed.add(claimed)
                # Stop the spinner and remove the button now: this tap may queue behind a long check.
                await _answer_quietly(st.tg, msg.callback_id, "On it…")
                await _drop_buttons_quietly(st.tg, msg.chat_id, msg.message_id)
                msg = msg.model_copy(update={"callback_id": None, "buttons": []})
        # The hold was taken before the webhook answered; keep refreshing it while queued and working.
        async with stay_awake(task, expire="5m"), chat_lock:
            await bot.handle(st.conn, st.clients, st.tg, msg)
        metrics.count(st.conn, "job", "telegram", "ok", metrics.elapsed_ms(started))
    except Exception as exc:
        # bot.handle reports command errors itself; this is what escaped it.
        metrics.count(st.conn, "job", "telegram", "error", metrics.elapsed_ms(started))
        metrics.failure(st.conn, "telegram", exc)
        raise
    finally:
        if claimed:
            _claimed.discard(claimed)
        await sprite.release(task)


# Slow taps queued or running, so a double tap on [Yes, watch it] runs /watch once. A key is released
# when its tap finishes; by then the button has been removed from the message.
_claimed: set[tuple[int, int]] = set()


async def _answer_quietly(tg: Telegram, callback_id: str | None, text: str) -> None:
    try:
        await tg.answer_callback(callback_id or "", text)
    except Exception as exc:  # noqa: BLE001 — the work matters more than the toast
        log.info("answering a slow tap failed: %s", type(exc).__name__)


async def _drop_buttons_quietly(tg: Telegram, chat_id: int, message_id: int | None) -> None:
    if not message_id:
        return
    try:
        await tg.edit_buttons(chat_id, message_id, None)
    except Exception as exc:  # noqa: BLE001
        log.info("removing a slow tap's buttons failed: %s", type(exc).__name__)


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
    started = time.monotonic()
    try:
        async with stay_awake(task, expire="5m"), locks.hold(f"email:{sender}"):
            await intake.process(st.conn, st.clients, st.tg, message_id, thread_id)
        metrics.count(st.conn, "job", "mail", "ok", metrics.elapsed_ms(started))
    except Exception as exc:  # noqa: BLE001
        log.warning("mail %s failed: %s", task, exc)
        metrics.count(st.conn, "job", "mail", "error", metrics.elapsed_ms(started))
        metrics.failure(st.conn, "mail", exc)
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
