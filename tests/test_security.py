import socket

import httpx
import pytest
import respx
from pydantic import SecretStr

from app import bot, db, netguard
from app.config import settings
from app.telegram import Incoming, Telegram, TelegramError, redact

TOKEN = "123456:ABC-def_ghi"


# --- SSRF guard ------------------------------------------------------------------------------------

def fake_dns(mapping):
    async def getaddrinfo(host, *args, **kwargs):
        if host not in mapping:
            raise socket.gaierror(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (mapping[host], 0))]
    return getaddrinfo


@pytest.fixture
def dns(monkeypatch):
    table = {"public.test": "93.184.215.14", "evil.test": "169.254.169.254", "lan.test": "10.0.0.5"}

    class Loop:
        getaddrinfo = staticmethod(fake_dns(table))

    monkeypatch.setattr(netguard.asyncio, "get_running_loop", lambda: Loop)
    return table


@pytest.mark.parametrize("host", ["evil.test", "lan.test", "nowhere.test"])
async def test_non_public_hosts_are_blocked(dns, host):
    with pytest.raises(netguard.BlockedURL):
        await netguard.check_host(host)


async def test_literal_loopback_and_metadata_ips_are_blocked():
    for ip in ("127.0.0.1", "169.254.169.254", "::1", "fdaa::3"):
        with pytest.raises(netguard.BlockedURL):
            await netguard.check_host(ip)


@respx.mock
async def test_redirect_to_private_address_is_blocked(dns):
    respx.get("https://public.test/terms").mock(
        return_value=httpx.Response(302, headers={"Location": "http://evil.test/latest/meta-data/"})
    )
    secret = respx.get("http://evil.test/latest/meta-data/").mock(return_value=httpx.Response(200, text="creds"))
    async with netguard.safe_client() as client:
        with pytest.raises(netguard.BlockedURL):
            await client.get("https://public.test/terms")
    assert not secret.called


@respx.mock
async def test_public_url_is_fetched(dns):
    respx.get("https://public.test/terms").mock(return_value=httpx.Response(200, text="ok"))
    async with netguard.safe_client() as client:
        assert (await client.get("https://public.test/terms")).text == "ok"


async def test_non_http_scheme_is_blocked():
    async with netguard.safe_client() as client:
        with pytest.raises((netguard.BlockedURL, httpx.UnsupportedProtocol)):
            await client.get("file:///etc/passwd")


# --- Owner claim -----------------------------------------------------------------------------------

class FakeTelegram:
    def __init__(self):
        self.sent = []

    async def send(self, chat, text):
        self.sent.append((chat, text))


@pytest.fixture
def claim(monkeypatch):
    patched = settings.model_copy(update={"owner_claim_code": SecretStr("s3cret-code"), "owner_chat_id": None})
    monkeypatch.setattr(bot, "settings", patched)


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "b.db")


async def test_bare_start_does_not_claim(conn, claim):
    tg = FakeTelegram()
    await bot.handle(conn, None, tg, Incoming(chat_id=7, text="/start"))
    assert bot.owner(conn) is None and tg.sent == []


async def test_wrong_code_does_not_claim(conn, claim):
    await bot.handle(conn, None, FakeTelegram(), Incoming(chat_id=7, text="/start nope"))
    assert bot.owner(conn) is None


async def test_right_code_claims_and_then_others_are_ignored(conn, claim):
    tg = FakeTelegram()
    await bot.handle(conn, None, tg, Incoming(chat_id=7, text="/start s3cret-code"))
    assert bot.owner(conn) == 7 and tg.sent[0][0] == 7
    await bot.handle(conn, None, tg, Incoming(chat_id=8, text="/start s3cret-code"))
    assert bot.owner(conn) == 7 and all(chat == 7 for chat, _ in tg.sent)


# --- Token redaction -------------------------------------------------------------------------------

def test_redact_strips_bot_token():
    msg = f"Client error '404' for url 'https://api.telegram.org/file/bot{TOKEN}/documents/x.pdf'"
    assert TOKEN not in redact(msg) and "bot<redacted>" in redact(msg)


@respx.mock
async def test_download_errors_do_not_contain_token():
    respx.post(f"https://api.telegram.org/bot{TOKEN}/getFile").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"file_path": "documents/x.pdf"}})
    )
    respx.get(f"https://api.telegram.org/file/bot{TOKEN}/documents/x.pdf").mock(return_value=httpx.Response(404))
    tg = Telegram(token=TOKEN)
    with pytest.raises(TelegramError) as exc:
        await tg.download("f1")
    await tg.aclose()
    assert TOKEN not in str(exc.value) and exc.value.__cause__ is None
