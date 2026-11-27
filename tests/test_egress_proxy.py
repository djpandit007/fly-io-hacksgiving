"""The workers' egress proxy: Chromium's only way out. Exact allowed hosts, CONNECT to the HTTPS port only,
every resolved address public, and the tunnel goes to the address that was checked (never a fresh lookup)."""

import asyncio

import pytest

from workers import egress_proxy as ep


@pytest.fixture
async def upstream():
    """A local stand-in for a site: echoes what it receives, prefixed, and records who connected."""
    seen = []

    async def handle(reader, writer):
        seen.append(writer.get_extra_info("sockname")[0])
        data = await reader.read(1024)
        writer.write(b"echo:" + data)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    yield port, seen
    server.close()
    await server.wait_closed()


def resolver(answers: dict[str, list[str]], calls: list[str]):
    async def resolve(host):
        calls.append(host)
        return answers.get(host, [])

    return resolve


async def connect(proxy, target: str) -> tuple[bytes, asyncio.StreamReader, asyncio.StreamWriter]:
    host, port = proxy.url.removeprefix("http://").split(":")
    reader, writer = await asyncio.open_connection(host, int(port))
    writer.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    await writer.drain()
    status = await reader.readuntil(b"\r\n\r\n")
    return status, reader, writer


async def test_allowed_host_is_tunnelled_to_the_checked_address(upstream):
    port, seen = upstream
    calls = []
    # The checked answer is 127.0.0.1 (a stand-in for a public address): the tunnel must go exactly there.
    async with ep.EgressProxy(["notion.so"], resolve=resolver({"notion.so": ["127.0.0.1"]}, calls), ports=[port]) as proxy:
        status, reader, writer = await connect(proxy, f"notion.so:{port}")
        assert status.startswith(b"HTTP/1.1 200")
        writer.write(b"hello")
        await writer.drain()
        assert await reader.read(1024) == b"echo:hello"
        writer.close()
    assert calls == ["notion.so"] and seen == ["127.0.0.1"] and proxy.refused == []


@pytest.mark.parametrize("target, reason", [
    ("evil.example:{port}", "host"),            # not on the job's list
    ("cdn.notion.so:{port}", "host"),           # sub-domains are not allowed implicitly
    ("notion.so.:{port}", "host"),              # dotted spelling of an allowed host
    ("notion.so:22", "port"),                   # only the HTTPS port
    ("private.notion.so:{port}", "address"),    # allowed name, but it resolves to a non-public address
])
async def test_everything_else_is_refused_without_connecting(upstream, target, reason):
    port, seen = upstream
    calls = []
    allow = ["notion.so", "private.notion.so"]
    async with ep.EgressProxy(allow, resolve=resolver({"notion.so": ["127.0.0.1"]}, calls), ports=[port]) as proxy:
        status, _, writer = await connect(proxy, target.format(port=port))
        writer.close()
    assert status.startswith(b"HTTP/1.1 403") and proxy.refused == [reason] and seen == []
    if reason in ("host", "port"):
        assert calls == []  # refused before any lookup


async def test_plain_http_and_wildcards_are_refused(upstream):
    port, seen = upstream
    async with ep.EgressProxy(["*.notion.so", "notion.so"], resolve=resolver({}, []), ports=[port]) as proxy:
        assert proxy.allow == ["notion.so"]  # wildcard entries are dropped
        host, p = proxy.url.removeprefix("http://").split(":")
        reader, writer = await asyncio.open_connection(host, int(p))
        writer.write(b"GET http://notion.so/ HTTP/1.1\r\nHost: notion.so\r\n\r\n")
        await writer.drain()
        assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 405")
        writer.close()
    assert proxy.refused == ["not_connect"] and seen == []


def test_chromium_is_sent_through_the_proxy_with_no_bypass():
    args = ep.chromium_args("http://127.0.0.1:4242")
    assert "--proxy-server=http://127.0.0.1:4242" in args
    assert "--proxy-bypass-list=<-loopback>" in args and "--disable-quic" in args


def test_only_exact_hosts_are_allowed():
    allow = ["notion.so", "www.notion.so"]
    assert ep.host_allowed("notion.so", allow) and ep.host_allowed("WWW.notion.so", allow)
    for host in ("cdn.notion.so", "notion.so.", ".notion.so", "", "evilnotion.so"):
        assert not ep.host_allowed(host, allow), host


async def test_public_addresses_refuse_any_private_answer_and_list_ipv4_first(monkeypatch):
    answers = {
        "public.test": ["1.1.1.1", "8.8.8.8"],
        "mixed.test": ["1.1.1.1", "10.0.0.5"],      # one inside address is enough to refuse
        "docs.test": ["203.0.113.7"],               # reserved documentation range: not global
        "loop.test": ["127.0.0.1"],
        "dual.test": ["2606:4700::1111", "1.1.1.1"],  # IPv6 answered first
    }

    class Loop:
        async def getaddrinfo(self, host, port, type=0):
            if host not in answers:
                raise OSError("nxdomain")
            return [(0, 0, 0, "", (ip, 0)) for ip in answers[host]]

    monkeypatch.setattr(ep.asyncio, "get_running_loop", lambda: Loop())
    assert await ep.public_addresses("public.test") == ["1.1.1.1", "8.8.8.8"]
    assert await ep.public_addresses("dual.test") == ["1.1.1.1", "2606:4700::1111"]
    for host in ("mixed.test", "docs.test", "loop.test", "missing.test"):
        assert await ep.public_addresses(host) == [], host


def test_runners_share_the_proxy_helpers():
    from workers import advocate_runner as ar
    from workers import scout_runner as sr

    assert sr.host_allowed is ep.host_allowed and ar.host_allowed is ep.host_allowed
    assert sr.EgressProxy is ep.EgressProxy and ar.EgressProxy is ep.EgressProxy
