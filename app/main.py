"""HTTP entry points. The Sprite URL is public (Telegram and the cron must reach it), so every
route except /health is guarded by a secret."""

from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel

from app import agent, bot, db, ledger
from app.agent import Clients
from app.config import settings
from app.gmail import Gmail
from app import sprite
from app.sprite import stay_awake
from app.telegram import Telegram, parse_update

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("fineprint")
# httpx logs every request URL at INFO, and Telegram URLs contain the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)

# One worker, one event loop: a single SQLite connection is safe; the lock keeps long jobs in order.
work_lock = asyncio.Lock()


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
    inbox: int
    sent: int


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/tick", dependencies=[Depends(require_secret)])
async def tick(request: Request) -> TickSummary:
    """Daily run. Called by the external cron, which keeps the connection open until done."""
    st = request.app.state
    async with work_lock, stay_awake("tick", expire="10m"):
        gmail = Gmail(settings.gmail_token_path)
        try:
            result = await agent.tick(st.conn, st.clients, gmail)
        finally:
            await gmail.aclose()
        sent = 0
        if (chat := bot.owner(st.conn)) and st.tg.configured:
            for msg in agent.tick_messages(result):
                await st.tg.send(chat, msg)
                sent += 1
    return TickSummary(
        checked=result.checked, failed=result.failed, changes=len(result.reports),
        nudges=len(result.nudges), inbox=len(result.inbox), sent=sent,
    )


async def _handle_message(app_: FastAPI, payload: dict, task: str) -> None:
    st = app_.state
    try:
        msg = parse_update(payload)
        if not msg:
            return
        # The hold was taken before the webhook answered; keep refreshing it while queued and working.
        async with stay_awake(task, expire="5m"), work_lock:
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


@app.get("/calendar.ics")
async def calendar(request: Request, token: Annotated[str, Query()] = "") -> Response:
    if not _same(token, settings.calendar_token.get_secret_value()):
        raise HTTPException(status_code=401)
    body = ledger.to_ics(ledger.upcoming(request.app.state.conn))
    return PlainTextResponse(body, media_type="text/calendar; charset=utf-8")


@app.post("/admin/telegram-webhook", dependencies=[Depends(require_secret)])
async def register_webhook(request: Request) -> dict:
    if not settings.public_url:
        raise HTTPException(status_code=400, detail="FP_PUBLIC_URL is not set")
    await request.app.state.tg.set_webhook(f"{settings.public_url}/telegram", settings.shared_secret.get_secret_value())
    return {"webhook": f"{settings.public_url}/telegram"}
