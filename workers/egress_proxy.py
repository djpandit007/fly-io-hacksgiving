"""Egress proxy for the worker runners: Chromium's only way out.

Chromium can't do its own DNS behind a Sprite's egress lock (the lock only lets a connection through to
an address the Sprite's resolver just handed out, and Chromium's lookups fail there), and pinning it with
--host-resolver-rules is refused for the same reason. So each runner starts this proxy on loopback and
launches Chromium with ``--proxy-server`` pointing at it. Chromium then never resolves a name itself:

* it may only open ``CONNECT host:443`` tunnels (HTTPS and WebSockets); anything else is refused;
* the host must be one of the job's exact allowed names (no wildcards, no leading or trailing dot);
* the proxy resolves it with the system resolver, as curl does, and refuses it unless *every* address is
  public (no private, loopback, link-local, multicast or reserved address);
* it then connects to one of those same checked addresses, IPv4 first, so a second DNS answer
  (rebinding) can never redirect the tunnel.

TLS stays end to end between Chromium and the site: the proxy only splices bytes.

Standalone (standard library only) so it runs on the worker Sprites and imports anywhere for tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Sequence
from typing import Self

HEADER_LIMIT = 8 * 1024
CONNECT_TIMEOUT = 15.0
HTTPS_PORTS = (443,)

Resolver = Callable[[str], Awaitable[list[str]]]


def host_allowed(host: str, allow: Sequence[str]) -> bool:
    """Exact match only. A host with a leading or trailing dot (`notion.so.`) is refused rather than
    normalised, so it can't be resolved separately from the allowed `notion.so`."""
    host = host.lower()
    if not host or host.startswith(".") or host.endswith("."):
        return False
    return host in {h.lower() for h in allow}


async def public_addresses(host: str) -> list[str]:
    """The host's addresses if every one is public, else [] (unresolvable or any non-public address).
    IPv4 addresses come first: tunnels try them in this order."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return []
    ips = [ipaddress.ip_address(str(i[4][0]).split("%")[0]) for i in infos]
    if not ips or not all(ip.is_global and not ip.is_multicast for ip in ips):
        return []
    unique = list(dict.fromkeys(ips))
    return [str(ip) for ip in sorted(unique, key=lambda ip: ip.version)]


def chromium_args(proxy_url: str) -> list[str]:
    """Launch flags sending every Chromium connection, loopback included, through the proxy."""
    return [
        f"--proxy-server={proxy_url}",
        "--proxy-bypass-list=<-loopback>",  # no implicit bypass for localhost either
        "--disable-quic",                   # QUIC (UDP) would not go through an HTTP proxy
    ]


class EgressProxy:
    """``async with EgressProxy(allow) as proxy:`` then launch Chromium with ``chromium_args(proxy.url)``."""

    def __init__(
        self,
        allow: Sequence[str],
        *,
        resolve: Resolver = public_addresses,
        ports: Sequence[int] = HTTPS_PORTS,
    ) -> None:
        self.allow = [h.lower() for h in allow if h and "*" not in h]
        self.resolve = resolve
        self.ports = tuple(ports)
        self.refused: list[str] = []  # reason labels only (never hosts), for the runner's error label
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task] = set()
        self.url = ""

    async def __aenter__(self) -> Self:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0, limit=HEADER_LIMIT)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *exc) -> None:
        if self._server is not None:
            self._server.close()
            for task in list(self._tasks):
                task.cancel()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            await self._serve(reader, writer)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, ValueError):
            pass
        finally:
            if task is not None:
                self._tasks.discard(task)
            with contextlib.suppress(Exception):
                writer.close()

    async def _refuse(self, writer: asyncio.StreamWriter, reason: str, status: str = "403 Forbidden") -> None:
        self.refused.append(reason)
        writer.write(f"HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
        with contextlib.suppress(Exception):
            await writer.drain()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        request_line = head.split(b"\r\n", 1)[0].decode("latin-1")
        parts = request_line.split(" ")
        if len(parts) != 3 or parts[0].upper() != "CONNECT":
            await self._refuse(writer, "not_connect", "405 Method Not Allowed")
            return
        host, sep, port_text = parts[1].rpartition(":")
        host = host.strip("[]").lower()
        if not sep or not port_text.isdigit() or int(port_text) not in self.ports:
            await self._refuse(writer, "port")
            return
        if not host_allowed(host, self.allow):
            await self._refuse(writer, "host")
            return
        addresses = await self.resolve(host)
        if not addresses:
            await self._refuse(writer, "address")
            return
        upstream = None
        for address in addresses:  # the checked addresses only, never a fresh lookup
            try:
                upstream = await asyncio.wait_for(asyncio.open_connection(address, int(port_text)), CONNECT_TIMEOUT)
                break
            except (OSError, TimeoutError):
                continue
        if upstream is None:
            await self._refuse(writer, "unreachable", "502 Bad Gateway")
            return
        up_reader, up_writer = upstream
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        try:
            await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
        finally:
            with contextlib.suppress(Exception):
                up_writer.close()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        with contextlib.suppress(Exception):
            if writer.can_write_eof():
                writer.write_eof()
