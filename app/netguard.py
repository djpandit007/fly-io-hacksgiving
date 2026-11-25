"""Outbound fetches of untrusted URLs (user links, links found on pages, domains from email).

The check happens where the socket is opened: the hostname is resolved once, every address is
checked, and the connection goes to exactly that checked address. Resolving again at connect time
would let a DNS server answer "public" for the check and "private" for the connection
(DNS rebinding). TLS still uses the original hostname for SNI and certificate checks, and the
Host header is unchanged, because httpcore starts TLS on the stream after connect_tcp.
Proxies from the environment are ignored so they cannot route around the check.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import typing

import httpcore
import httpx
from httpcore._backends.auto import AutoBackend

from app.watch import USER_AGENT


class BlockedURL(httpx.RequestError):
    pass


async def resolve_public(host: str) -> list[str]:
    """All addresses for host, or BlockedURL if any of them is not a public address."""
    if not host:
        raise BlockedURL("URL has no host")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BlockedURL(f"cannot resolve {host}") from exc
    addresses: list[str] = []
    for info in infos:
        ip = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        if not ip.is_global or ip.is_multicast:
            raise BlockedURL(f"{host} resolves to a non-public address")
        if (text := str(ip)) not in addresses:
            addresses.append(text)
    if not addresses:
        raise BlockedURL(f"cannot resolve {host}")
    return addresses


# Kept for callers that only want the check.
async def check_host(host: str) -> None:
    await resolve_public(host)


class PinnedBackend(httpcore.AsyncNetworkBackend):
    """Resolve, check, then connect to the checked IP itself."""

    def __init__(self, inner: httpcore.AsyncNetworkBackend | None = None):
        self.inner = inner or AutoBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Iterable[typing.Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        last: Exception | None = None
        for ip in await resolve_public(host):
            try:
                return await self.inner.connect_tcp(
                    ip, port, timeout=timeout, local_address=local_address, socket_options=socket_options
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last = exc
        assert last is not None
        raise last

    async def connect_unix_socket(self, *args: typing.Any, **kwargs: typing.Any) -> httpcore.AsyncNetworkStream:
        raise BlockedURL("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self.inner.sleep(seconds)


def pinned_transport() -> httpx.AsyncHTTPTransport:
    transport = httpx.AsyncHTTPTransport(retries=0)
    pool = transport._pool
    # httpx does not expose network_backend; set it on the pool and fail loudly if internals move.
    if not isinstance(pool, httpcore.AsyncConnectionPool) or not hasattr(pool, "_network_backend"):
        raise RuntimeError("httpx/httpcore internals changed; netguard cannot pin connections")
    pool._network_backend = PinnedBackend()
    return transport


async def _scheme_guard(request: httpx.Request) -> None:
    if request.url.scheme not in ("http", "https"):
        raise BlockedURL(f"scheme {request.url.scheme!r} is not allowed")


def safe_client(timeout: float = 30) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=pinned_transport(),
        trust_env=False,  # no proxies from the environment
        follow_redirects=True,  # every hop opens a new connection through PinnedBackend
        max_redirects=5,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT},
        event_hooks={"request": [_scheme_guard]},
    )
