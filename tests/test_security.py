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

class SequenceDNS:
    """Fake resolver; each lookup of a host returns the next answer (to simulate rebinding)."""

    def __init__(self, answers):
        self.answers = {h: list(v) for h, v in answers.items()}
        self.calls = []

    async def getaddrinfo(self, host, *args, **kwargs):
        self.calls.append(host)
        if not self.answers.get(host):
            raise socket.gaierror(host)
        ip = self.answers[host][0] if len(self.answers[host]) == 1 else self.answers[host].pop(0)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]


@pytest.fixture
def dns(monkeypatch):
    resolver = SequenceDNS({
        "public.test": ["93.184.215.14"],
        "evil.test": ["169.254.169.254"],
        "lan.test": ["10.0.0.5"],
        "rebind.test": ["93.184.215.14", "127.0.0.1"],
    })
    monkeypatch.setattr(netguard.asyncio, "get_running_loop", lambda: resolver)
    return resolver


class RecordingBackend:
    def __init__(self):
        self.hosts = []

    async def connect_tcp(self, host, port, **kwargs):
        self.hosts.append(host)
        return "stream"


@pytest.mark.parametrize("host", ["evil.test", "lan.test", "nowhere.test"])
async def test_non_public_hosts_are_blocked_before_connecting(dns, host):
    inner = RecordingBackend()
    with pytest.raises(netguard.BlockedURL):
        await netguard.PinnedBackend(inner).connect_tcp(host, 443)
    assert inner.hosts == []


async def test_connects_to_the_checked_ip_not_the_hostname(dns):
    inner = RecordingBackend()
    await netguard.PinnedBackend(inner).connect_tcp("public.test", 443)
    assert inner.hosts == ["93.184.215.14"]


async def test_dns_rebinding_cannot_swap_the_address(dns):
    # First answer is public, a second lookup would say 127.0.0.1: there is no second lookup.
    inner = RecordingBackend()
    await netguard.PinnedBackend(inner).connect_tcp("rebind.test", 443)
    assert inner.hosts == ["93.184.215.14"] and dns.calls == ["rebind.test"]


async def test_literal_loopback_and_metadata_ips_are_blocked():
    for ip in ("127.0.0.1", "169.254.169.254", "::1", "fdaa::3"):
        with pytest.raises(netguard.BlockedURL):
            await netguard.resolve_public(ip)


async def test_real_client_refuses_loopback():
    async with netguard.safe_client(timeout=5) as client:
        with pytest.raises(netguard.BlockedURL):
            await client.get("http://127.0.0.1:9/")


async def test_redirect_hop_to_private_address_is_blocked(monkeypatch):
    """Real HTTP: a 'public' server redirects to a private host; the second hop is refused."""
    import http.server
    import threading

    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://evil.test:{self.server.server_port}/meta-data")
            self.end_headers()

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    async def fake_resolve(host):
        if host == "public.test":
            return ["127.0.0.1"]  # pretend the local server is a public site
        raise netguard.BlockedURL(f"{host} resolves to a non-public address")

    monkeypatch.setattr(netguard, "resolve_public", fake_resolve)
    try:
        async with netguard.safe_client(timeout=5) as client:
            with pytest.raises(netguard.BlockedURL, match="evil.test"):
                await client.get(f"http://public.test:{server.server_port}/terms")
    finally:
        server.shutdown()


async def test_non_http_scheme_is_blocked():
    async with netguard.safe_client() as client:
        with pytest.raises((netguard.BlockedURL, httpx.UnsupportedProtocol)):
            await client.get("file:///etc/passwd")


def test_proxies_from_environment_are_ignored(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://10.0.0.1:3128")
    client = netguard.safe_client()
    assert client._trust_env is False


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


def test_empty_settings_in_env_file_mean_unset(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("FP_OWNER_CHAT_ID", "")
    monkeypatch.setenv("FP_LLAMA_BASE", "")
    s = Settings()
    assert s.owner_chat_id is None and s.llama_base == "https://api.cloud.llamaindex.ai"
