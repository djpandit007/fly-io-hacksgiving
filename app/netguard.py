"""Outbound fetches of untrusted URLs (user links, links found on pages, domains from email).

Every request, including each redirect hop, must use http(s) and resolve only to public
addresses, so a crafted link cannot make the Sprite fetch loopback, link-local (cloud metadata)
or private-network services.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket

import httpx

from app.watch import USER_AGENT


class BlockedURL(httpx.RequestError):
    pass


async def check_host(host: str) -> None:
    if not host:
        raise BlockedURL("URL has no host")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BlockedURL(f"cannot resolve {host}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global or ip.is_multicast:
            raise BlockedURL(f"{host} resolves to a non-public address")


async def _guard(request: httpx.Request) -> None:
    if request.url.scheme not in ("http", "https"):
        raise BlockedURL(f"scheme {request.url.scheme!r} is not allowed")
    await check_host(request.url.host)


def safe_client(timeout: float = 30) -> httpx.AsyncClient:
    """httpx calls request hooks for every redirect hop too, so each hop is checked."""
    return httpx.AsyncClient(
        follow_redirects=True,
        max_redirects=5,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT},
        event_hooks={"request": [_guard]},
    )
