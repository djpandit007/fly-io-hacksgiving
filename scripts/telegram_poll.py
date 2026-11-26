"""Test the bot while the Sprite URL is still private.

Telegram can only push webhooks to a public URL, but a bot can also pull its messages
(getUpdates). This script runs on the Sprite, pulls each message and hands it to the running app
exactly as the webhook would, including the secret header.

    sprite exec -- bash -lc 'cd ~/fine-print && UV_PYTHON_PREFERENCE=only-managed uv run --no-sync python scripts/telegram_poll.py'

Stop with Ctrl+C. It deletes any registered webhook first, because Telegram refuses getUpdates
while one is set; register the webhook again when you go public.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.telegram import redact

APP = "http://127.0.0.1:8080"


async def main() -> None:
    token = settings.telegram_token.get_secret_value()
    secret = settings.shared_secret.get_secret_value()
    if not token or not secret:
        sys.exit("Set FP_TELEGRAM_TOKEN and FP_SHARED_SECRET in .env first, then redeploy.")
    api = f"https://api.telegram.org/bot{token}"

    async with httpx.AsyncClient(timeout=70) as client:
        me = (await client.get(f"{api}/getMe")).json()
        if not me.get("ok"):
            sys.exit(f"Telegram rejected the token: {me.get('description')}")
        await client.post(f"{api}/deleteWebhook")
        print(f"Polling as @{me['result']['username']}. Send it /start <claim code>. Ctrl+C to stop.", flush=True)

        offset: int | None = None
        while True:
            params: dict[str, object] = {"timeout": 50, "allowed_updates": '["message"]'}
            if offset is not None:
                params["offset"] = offset
            try:
                resp = await client.get(f"{api}/getUpdates", params=params)
                for update in resp.json().get("result", []):
                    offset = update["update_id"] + 1
                    text = (update.get("message") or {}).get("text") or "[file or photo]"
                    shown = "/start <code>" if text.startswith("/start ") else text[:60]
                    fwd = await client.post(
                        f"{APP}/telegram", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": secret}
                    )
                    print(f"-> {shown!r}: app answered {fwd.status_code}", flush=True)
            except httpx.HTTPError as exc:
                print("poll error:", redact(f"{type(exc).__name__}: {exc}"), flush=True)
                await asyncio.sleep(3)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
