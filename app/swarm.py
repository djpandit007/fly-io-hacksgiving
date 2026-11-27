"""The worker Sprites (Scout and Advocate), driven from the Gateway through the Sprites API.

Each job on a worker Sprite runs the same way (``Swarm.job``):

1. lock the worker's DNS egress to the hosts this job needs (``POST /v1/sprites/{name}/policy/network``,
   ending in a ``deny *`` rule: an empty rule list would mean *no* enforcement);
2. run a small runner script with the job as JSON on stdin (``POST /v1/sprites/{name}/exec``);
3. in a ``finally``, close egress again and restore the worker's clean base checkpoint
   (``POST /v1/sprites/{name}/checkpoints/{id}/restore``), timing the restore.

The runner prints its JSON result base64-encoded between two markers, so it can be found in the exec
output however that output is framed. Every step is recorded in ``swarm_events`` and ``metric_counts``
with labels only: never a URL, file name or member id.

When the Sprites API, a worker's name or its base checkpoint is not configured, ``available(role)`` is
False and callers keep the single-Sprite path (direct fetch, Exa, LlamaParse). Without a base
checkpoint there is nothing to restore, so a worker is never used without one.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
import sqlite3
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
import tldextract
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app import metrics
from app.config import Provider, settings
from app.db import DOC_ROLES, SwarmEvent, rows
from app.gateway import Gateway, Recorder
from app.netguard import BlockedURL, resolve_public, safe_client

log = logging.getLogger(__name__)

Role = Literal["scout", "advocate"]
SCOUT_TIMEOUT = 45.0
ADVOCATE_TIMEOUT = 120.0
TIMEOUTS: dict[str, float] = {"scout": SCOUT_TIMEOUT, "advocate": ADVOCATE_TIMEOUT}
RESTORE_TIMEOUT = 90.0   # measured on live Sprites: ~15 s warm, ~48 s when the worker was asleep
POLICY_TIMEOUT = 60.0    # the first call to a sleeping worker waits for it to wake
# Hosts beyond the target's own site a job may reach (asset CDNs). Kept tiny on purpose.
MAX_EXTRA_HOSTS = 3
# Where scripts/setup_swarm.sh puts the runners and their virtualenv on each worker Sprite.
WORKER_DIR = "/home/sprite/fine-print-worker"
PYTHON = f"{WORKER_DIR}/.venv/bin/python"
RUNNERS: dict[str, str] = {
    "scout": f"{WORKER_DIR}/workers/scout_runner.py",
    "advocate": f"{WORKER_DIR}/workers/advocate_runner.py",
}
# Egress a worker keeps between jobs. The Scout needs none; the Advocate reaches its send-scoped
# connectors through the Sprites connector gateway.
IDLE_HOSTS: dict[str, tuple[str, ...]] = {"scout": (), "advocate": ("api.sprites.dev",)}
RESULT_START, RESULT_END = "<<FP-RESULT>>", "<<FP-END>>"
_RESULT = re.compile(re.escape(RESULT_START) + r"([A-Za-z0-9+/=\s]*?)" + re.escape(RESULT_END))
# Stream-id bytes (0x00-0x04) that frame non-TTY exec output; never part of the base64 result.
_FRAMING = bytes(range(9))
_HOST = re.compile(r"(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
_PSL = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None, include_psl_private_domains=True)


class SwarmError(RuntimeError):
    """A worker step failed. The message is a generic label, never a URL."""


class SwarmUnavailable(SwarmError):
    pass


# --- Sprites API shapes ---------------------------------------------------------------------------

class NetworkRule(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: Literal["allow", "deny"]
    domain: str


class NetworkPolicy(BaseModel):
    rules: list[NetworkRule]


class StreamEvent(BaseModel):
    """One line of a checkpoint restore's NDJSON progress stream."""

    type: str
    data: str = ""
    error: str = ""


class ExecOutput(BaseModel):
    """What a runner printed, and whether the Sprites API reported an exit code."""

    stdout: str
    exit_code: int | None = None


# --- Job payloads ---------------------------------------------------------------------------------

class ScoutJob(BaseModel):
    url: str
    allow_hosts: list[str]
    screenshot: bool = False  # only for public service pages; private checks capture nothing


class PolicyLink(BaseModel):
    url: str
    role: str
    text: str = ""


class ScoutResult(BaseModel):
    ok: bool
    error: str = ""
    final_url: str = ""
    title: str = ""
    html: str = ""
    text: str = ""
    sha256: str = ""
    links: list[PolicyLink] = Field(default_factory=list)
    screenshot_png_b64: str = ""


class PublicPage(BaseModel):
    """A public service page whose evidentiary screenshot may be kept. Private checks pass None."""

    model_config = ConfigDict(frozen=True)

    service_id: int
    doc_role: str = "terms"


# --- Egress policy --------------------------------------------------------------------------------

def registrable(host: str) -> str:
    host = host.lower().strip(".")
    return _PSL(host).top_domain_under_public_suffix or host


def egress_hosts(url: str, extra: Sequence[str] = ()) -> list[str]:
    """The DNS allowlist for one job: exactly the target host and its registrable site, plus at most
    MAX_EXTRA_HOSTS explicit extra hosts. Never wildcards: every host must be one the worker can
    resolve, check and pin before it starts."""
    host = (urlparse(url).hostname or "").lower().strip(".")
    if not _HOST.fullmatch(host):
        raise BlockedURL("target is not a domain name")
    site = registrable(host)
    hosts = [host, site]
    extras = [h.lower().strip(".") for h in extra]
    if len(extras) > MAX_EXTRA_HOSTS or not all(_HOST.fullmatch(h) for h in extras):
        raise ValueError("too many or invalid extra hosts")
    return list(dict.fromkeys(hosts + extras))


def lock_rules(hosts: Sequence[str]) -> NetworkPolicy:
    """Allow exactly these hosts (no wildcards) and refuse every other lookup."""
    for h in hosts:
        if not _HOST.fullmatch(h):
            raise ValueError("invalid egress host")
    return NetworkPolicy(
        rules=[NetworkRule(action="allow", domain=h) for h in hosts] + [NetworkRule(action="deny", domain="*")]
    )


# --- Exec output ----------------------------------------------------------------------------------

def decode_exec(resp: httpx.Response) -> ExecOutput:
    """Exec output as text, whether the API answered JSON ({stdout, exit_code}) or raw (framed) bytes."""
    if "json" in resp.headers.get("content-type", ""):
        try:
            data = resp.json()
        except ValueError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("stdout"), str):
            code = data.get("exit_code", data.get("exitCode"))
            return ExecOutput(stdout=data["stdout"], exit_code=code if isinstance(code, int) else None)
    raw = resp.content.translate(None, _FRAMING)
    return ExecOutput(stdout=raw.decode("utf-8", errors="replace"))


def extract_result(stdout: str) -> dict[str, Any]:
    """The runner's JSON result from between the markers (the last one wins)."""
    found = _RESULT.findall(stdout)
    if not found:
        raise SwarmError("no_result")
    try:
        data = json.loads(base64.b64decode("".join(found[-1].split()), validate=True))
    except (binascii.Error, ValueError) as exc:
        raise SwarmError("bad_result") from exc
    if not isinstance(data, dict):
        raise SwarmError("bad_result")
    return data


# --- Client ---------------------------------------------------------------------------------------

class Swarm:
    """Typed Sprites API calls for the worker Sprites, plus the lock → run → restore job wrapper."""

    def __init__(
        self,
        provider: Provider | None = None,
        *,
        conn: sqlite3.Connection | None = None,
        recorder: Recorder | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sprites: dict[str, str] | None = None,
        checkpoints: dict[str, str] | None = None,
    ) -> None:
        provider = provider or settings.sprites
        self.gw = Gateway(provider, timeout=ADVOCATE_TIMEOUT + 15, transport=transport, recorder=recorder)
        self.conn = conn
        self.sprites = sprites if sprites is not None else {
            "scout": settings.scout_sprite, "advocate": settings.advocate_sprite,
        }
        self.checkpoints = checkpoints if checkpoints is not None else {
            "scout": settings.scout_base_checkpoint, "advocate": settings.advocate_base_checkpoint,
        }
        # Workers whose last clean-up (egress close or checkpoint restore) failed. Fail closed: no job
        # runs on a quarantined worker until a fresh restore succeeds.
        self.quarantined: set[str] = set()

    @classmethod
    def from_settings(cls, recorder: Recorder | None = None) -> Swarm | None:
        """A client when the Sprites API is configured, else None (single-Sprite mode)."""
        if not settings.sprites.configured:
            return None
        return cls(conn=getattr(recorder, "conn", None), recorder=recorder)

    def available(self, role: str) -> bool:
        return bool(self.sprites.get(role) and self.checkpoints.get(role))

    async def aclose(self) -> None:
        await self.gw.aclose()

    # Telemetry: labels and durations only.
    def _event(self, role: str, event: str, detail: str = "", ms: int | None = None) -> None:
        if self.conn is not None:
            metrics.swarm_event(self.conn, role, event, detail, ms)

    def _count(self, role: str, step: str, outcome: str, ms: int = 0) -> None:
        if self.conn is not None:
            metrics.swarm_count(self.conn, role, step, outcome, ms)

    def _path(self, role: str, tail: str) -> str:
        return f"v1/sprites/{self.sprites[role]}/{tail}"

    # --- Sprites API calls ---

    async def set_network_policy(self, role: str, policy: NetworkPolicy) -> None:
        await self.gw.request(
            "POST", self._path(role, "policy/network"), json=policy.model_dump(), log_body=False, timeout=POLICY_TIMEOUT
        )

    async def restore_checkpoint(self, role: str) -> None:
        resp = await self.gw.request(
            "POST", self._path(role, f"checkpoints/{self.checkpoints[role]}/restore"),
            log_body=False, timeout=RESTORE_TIMEOUT,
        )
        for line in resp.text.splitlines():
            if not line.strip():
                continue
            try:
                event = StreamEvent.model_validate_json(line)
            except ValidationError:
                continue
            if event.type == "error":
                raise SwarmError("restore_failed")

    async def exec(self, role: str, cmd: Sequence[str], stdin: bytes, timeout: float) -> ExecOutput:
        params = [("cmd", c) for c in cmd] + [("path", cmd[0]), ("stdin", "true")]
        resp = await self.gw.request(
            "POST", self._path(role, "exec"), params=params, content=stdin,
            headers={"Content-Type": "application/octet-stream"}, retries=0, log_body=False, timeout=timeout,
        )
        return decode_exec(resp)

    # --- Job lifecycle ---

    @asynccontextmanager
    async def job(self, role: str, allow_hosts: Sequence[str] | None) -> AsyncIterator[None]:
        """Lock egress (when hosts are given), run the body, then always close egress and restore.

        A failed lock aborts the job before anything runs: a worker never crawls unlocked. A failed
        clean-up fails closed: the job's result is discarded (SwarmError replaces it) and the worker is
        quarantined until a later restore succeeds, so no job runs on a dirty worker.
        """
        if not self.available(role):
            raise SwarmUnavailable("worker_unconfigured")
        if role in self.quarantined:
            await self._reset(role, close_egress=True)  # raises while the worker stays dirty
        try:
            if allow_hosts is not None:
                rules = lock_rules(allow_hosts)
                started = time.monotonic()
                try:
                    await self.set_network_policy(role, rules)
                except Exception:
                    self._count(role, "egress_lock", "error", metrics.elapsed_ms(started))
                    self._event(role, "error", "egress_lock_failed")
                    raise
                ms = metrics.elapsed_ms(started)
                self._count(role, "egress_lock", "ok", ms)
                self._event(role, "egress_lock", "dns_allowlist", ms)
            yield
        finally:
            await self._reset(role, close_egress=allow_hosts is not None)

    async def _reset(self, role: str, close_egress: bool) -> None:
        """Close egress and restore the base checkpoint. On any failure the worker is quarantined and
        SwarmError is raised (fail closed); on success any quarantine is lifted."""
        egress_closed = True
        if close_egress:
            try:
                await self.set_network_policy(role, lock_rules(IDLE_HOSTS.get(role, ())))
            except Exception as exc:  # noqa: BLE001 — the restore below must still run
                egress_closed = False
                log.warning("swarm %s: closing egress failed: %s", role, type(exc).__name__)
                self._event(role, "error", "egress_close_failed")
        started = time.monotonic()
        try:
            await self.restore_checkpoint(role)
        except Exception as exc:  # recorded, then re-raised as SwarmError below
            ms = metrics.elapsed_ms(started)
            log.error("swarm %s: base checkpoint restore failed: %s", role, type(exc).__name__)
            self._count(role, "restore", "error", ms)
            self._event(role, "error", "checkpoint_restore_failed", ms)
            self.quarantined.add(role)
            raise SwarmError("restore_failed") from exc
        ms = metrics.elapsed_ms(started)
        self._count(role, "restore", "ok", ms)
        self._event(role, "checkpoint_restore", "base_checkpoint", ms)
        if not egress_closed:
            self.quarantined.add(role)
            raise SwarmError("egress_close_failed")
        self.quarantined.discard(role)

    async def run_job(
        self,
        role: str,
        payload: BaseModel | dict[str, Any],
        *,
        allow_hosts: Sequence[str] | None,
        detail: str = "",
        args: Sequence[str] = (),
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Run the role's runner with `payload` as JSON on stdin; return its JSON result.

        `allow_hosts=None` leaves the worker's egress policy as it is. `detail` is a generic label for
        telemetry (e.g. ``playwright_crawl``). Raises SwarmError (or an httpx error) on failure; the base
        checkpoint is restored either way.
        """
        limit = timeout or TIMEOUTS.get(role, SCOUT_TIMEOUT)
        body = (payload.model_dump_json() if isinstance(payload, BaseModel) else json.dumps(payload)).encode()
        started = time.monotonic()
        outcome = "error"
        try:
            async with self.job(role, allow_hosts):
                self._event(role, "run", detail or "job")
                try:
                    async with asyncio.timeout(limit):
                        out = await self.exec(role, [PYTHON, RUNNERS[role], *args], body, limit)
                    result = extract_result(out.stdout)
                except TimeoutError:
                    outcome = "timeout"
                    self._event(role, "error", "timeout")
                    raise SwarmError("timeout") from None
                except Exception as exc:
                    # SwarmError messages are fixed labels; anything else is named by its type only.
                    self._event(role, "error", str(exc) if isinstance(exc, SwarmError) else type(exc).__name__)
                    raise
                outcome = "ok"
                return result
        except SwarmUnavailable:
            outcome = "unavailable"
            raise
        finally:
            if outcome != "unavailable":
                self._count(role, "job", outcome, metrics.elapsed_ms(started))

    # --- Scout ---

    async def scout(self, url: str, *, screenshot: bool = False, extra_hosts: Sequence[str] = ()) -> ScoutResult:
        """Render `url` on the Scout Sprite under a DNS lock to the URL's own site.

        The URL must resolve to public addresses (checked here first); BlockedURL otherwise.
        """
        parts = urlparse(url)
        if parts.scheme not in ("http", "https"):
            raise BlockedURL("scheme not allowed")
        await resolve_public(parts.hostname or "")
        # Worker egress allows exact host names only, so crawl where the URL lands after its HTTP
        # redirects (each hop vetted here), not where it starts: www.notion.so/terms -> app.notion.com/...
        url = await landing_url(url)
        hosts = egress_hosts(url, extra_hosts)
        job = ScoutJob(url=url, allow_hosts=hosts, screenshot=screenshot)
        data = await self.run_job("scout", job, allow_hosts=hosts, detail="playwright_crawl")
        try:
            result = ScoutResult.model_validate(data)
        except ValidationError as exc:
            raise SwarmError("bad_result") from exc
        if not result.ok:
            raise SwarmError("scout_failed")
        final = (urlparse(result.final_url).hostname or "").lower() if result.final_url else ""
        if final and not host_allowed(final, hosts):
            raise SwarmError("outside_lock")  # the runner refuses this itself; never trust it
        if not screenshot:
            result = result.model_copy(update={"screenshot_png_b64": ""})
        return result


LANDING_TIMEOUT = 15.0


async def landing_url(url: str, client: httpx.AsyncClient | None = None) -> str:
    """Where `url` ends up after its HTTP redirects, followed by netguard's client (every hop must be
    public, at most 5 hops). A redirect to a non-public address raises BlockedURL; any other failure
    (bot wall, timeout) leaves the URL as it was. Only the response headers are read."""
    own = client is None
    client = client or safe_client(timeout=LANDING_TIMEOUT)
    try:
        async with client.stream("GET", url) as resp:
            landed = str(resp.url)
        return landed if urlparse(landed).scheme in ("http", "https") else url
    except BlockedURL:
        raise
    except httpx.HTTPError:
        return url
    finally:
        if own:
            await client.aclose()


def host_allowed(host: str, allow: Sequence[str]) -> bool:
    """Exact match only; a dotted or empty host is refused rather than normalised."""
    host = host.lower()
    if not host or host.startswith(".") or host.endswith("."):
        return False
    return host in {h.lower() for h in allow}


# --- Evidence -------------------------------------------------------------------------------------

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MAX_EVIDENCE_BYTES = 8 * 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{64}")


def evidence_relpath(service_id: int, doc_role: str, sha: str) -> str:
    """`<service_id>/<doc_role>/<sha>.png`, relative to settings.evidence_dir."""
    if doc_role not in DOC_ROLES or not _SHA.fullmatch(sha) or int(service_id) <= 0:
        raise ValueError("invalid evidence key")
    return f"{int(service_id)}/{doc_role}/{sha}.png"


def evidence_for(service_id: int, doc_role: str, sha: str, root: Path | None = None) -> str | None:
    """The stored screenshot's relative path for this exact text, if the Scout captured one."""
    rel = evidence_relpath(service_id, doc_role, sha)
    return rel if ((root or settings.evidence_dir) / rel).is_file() else None


def save_evidence(page: PublicPage, sha: str, png_b64: str, root: Path | None = None) -> str | None:
    """Keep a public page's screenshot as evidence for the text with this digest. Returns its
    relative path, or None when there is nothing valid to keep."""
    if not png_b64:
        return None
    try:
        png = base64.b64decode(png_b64, validate=True)
        rel = evidence_relpath(page.service_id, page.doc_role, sha)
    except (binascii.Error, ValueError):
        return None
    if not png.startswith(PNG_MAGIC) or len(png) > MAX_EVIDENCE_BYTES:
        return None
    path = (root or settings.evidence_dir) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(png)
        tmp.replace(path)
    return rel


# --- Reading telemetry ----------------------------------------------------------------------------

def events_after(conn: sqlite3.Connection, last_id: int = 0, limit: int = 50) -> list[SwarmEvent]:
    """Sanitized swarm events newer than `last_id`, oldest first."""
    return rows(
        SwarmEvent,
        conn.execute(
            "SELECT * FROM swarm_events WHERE id > ? ORDER BY id LIMIT ?", (int(last_id), max(1, min(int(limit), 500)))
        ),
    )


def recent_events(conn: sqlite3.Connection, limit: int = 20) -> list[SwarmEvent]:
    """The newest events, oldest first (to seed the activity strip before streaming)."""
    latest = rows(
        SwarmEvent, conn.execute("SELECT * FROM swarm_events ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 500)),))
    )
    return latest[::-1]
