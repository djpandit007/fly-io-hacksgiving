#!/usr/bin/env python3
"""Scout runner: render one legal page in headless Chromium on the Scout Sprite.

Run by the Gateway through the Sprites exec API. Reads a JSON job on stdin::

    {"url": "https://www.notion.so/terms", "allow_hosts": ["www.notion.so", "notion.so"], "screenshot": true}

and prints one result, base64-encoded JSON between ``<<FP-RESULT>>`` and ``<<FP-END>>``::

    {"ok": true, "final_url": ..., "title": ..., "html": ..., "text": ..., "sha256": ...,
     "links": [{"url": ..., "role": "privacy", "text": "Privacy Policy"}], "screenshot_png_b64": ...}

The Sprite holds no credentials and its DNS is locked to `allow_hosts` for this job; the runner also
aborts any request to a host outside that list or to a non-public address, as a second fence. The
Gateway restores the Sprite's base checkpoint afterwards, so nothing written here survives the job.

Standalone on purpose (standard library + Playwright, imported lazily) so the pure helpers can be
tested anywhere without a browser.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import re
import sys
import unicodedata
from typing import Any
from urllib.parse import urldefrag, urljoin, urlparse

try:  # run as a script on the worker (workers/ on sys.path) or imported as a package in tests
    from egress_proxy import EgressProxy, chromium_args, host_allowed
except ImportError:  # pragma: no cover - exercised by the test suite's package import
    from workers.egress_proxy import EgressProxy, chromium_args, host_allowed

RESULT_START, RESULT_END = "<<FP-RESULT>>", "<<FP-END>>"
NAV_TIMEOUT_MS = 25_000
SETTLE_MS = 800
MAX_HTML_CHARS = 2_000_000
MAX_TEXT_CHARS = 1_000_000
MAX_LINKS = 40
MAX_EXPAND = 60
MAX_SCREENSHOT_BYTES = 6 * 1024 * 1024
USER_AGENT = "FinePrint/0.1 (personal terms watcher; scout)"

# Companion legal documents, most specific first ("Data Processing Addendum" is not the privacy policy).
ROLE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ai_terms", re.compile(r"\b(ai|artificial intelligence|generative|machine learning)\b[^.]{0,30}\b(terms|polic(y|ies)|supplement|addendum)\b|\bai[-_ ]?(terms|policy)\b", re.IGNORECASE)),
    ("dpa", re.compile(r"\bdpa\b|data processing (agreement|addendum)", re.IGNORECASE)),
    ("billing", re.compile(r"\bbilling\b|payment terms|refund polic|subscription terms|\bpricing terms\b", re.IGNORECASE)),
    ("privacy", re.compile(r"\bprivacy\b|data protection notice", re.IGNORECASE)),
    ("terms", re.compile(r"terms of (service|use)|terms (and|&) conditions|user agreement|\btos\b|/terms\b|/legal\b", re.IGNORECASE)),
]

_TYPOGRAPHY = str.maketrans({" ": " ", "’": "'", "‘": "'", "“": '"', "”": '"', "\u200b": ""})

EXPAND_JS = """(limit) => {
  let n = 0;
  for (const d of document.querySelectorAll('details:not([open])')) { d.open = true; n++; }
  const toggles = document.querySelectorAll(
    'button[aria-expanded="false"], [role="button"][aria-expanded="false"], summary[aria-expanded="false"]');
  for (const el of Array.from(toggles).slice(0, limit)) { try { el.click(); n++; } catch (e) {} }
  return n;
}"""

ANCHORS_JS = """() => Array.from(document.querySelectorAll('a[href]')).slice(0, 2000)
  .map(a => ({href: a.getAttribute('href') || '', text: (a.innerText || a.textContent || '').trim().slice(0, 200)}))"""


# --- Pure helpers (unit-tested without a browser) --------------------------------------------------

def normalise_text(text: str) -> str:
    """Stable text for hashing: NFKC, straight quotes, single spaces, no blank lines."""
    text = unicodedata.normalize("NFKC", text).translate(_TYPOGRAPHY)
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def on_site(host: str, allow: list[str]) -> bool:
    """For *reporting* links only (never for fetching): the host is an allowed host or a sub-domain of
    one. The Gateway vets a reported link again, and any later fetch gets its own exact-host lock."""
    host = host.lower()
    if not host or host.startswith(".") or host.endswith("."):
        return False
    return any(host == h or host.endswith("." + h) for h in (a.lower() for a in allow))


def classify_link(text: str, url: str) -> str | None:
    """The legal document role a link points to, from its text and then its path."""
    path = urlparse(url).path.replace("-", " ").replace("_", " ")
    for candidate in (text, path, urlparse(url).path):
        for role, pattern in ROLE_PATTERNS:
            if candidate and pattern.search(candidate):
                return role
    return None


def extract_policy_links(anchors: list[dict[str, str]], base_url: str, allow: list[str]) -> list[dict[str, str]]:
    """Companion policy links on the locked site: absolute http(s) URLs, de-duplicated, capped."""
    out: list[dict[str, str]] = []
    seen = {urldefrag(base_url)[0]}
    for a in anchors:
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        url = urldefrag(urljoin(base_url, href))[0]
        parts = urlparse(url)
        if parts.scheme not in ("http", "https") or not on_site(parts.hostname or "", allow) or url in seen:
            continue
        text = " ".join((a.get("text") or "").split())[:120]
        if role := classify_link(text, url):
            seen.add(url)
            out.append({"url": url, "role": role, "text": text})
        if len(out) >= MAX_LINKS:
            break
    return out


def parse_job(raw: str | bytes) -> dict[str, Any]:
    job = json.loads(raw)
    if not isinstance(job, dict) or not isinstance(job.get("url"), str):
        raise TypeError("job needs a url")
    allow = job.get("allow_hosts")
    if not isinstance(allow, list) or not allow or not all(isinstance(h, str) and h and "*" not in h for h in allow):
        raise ValueError("job needs explicit allow_hosts")  # exact hosts only, never wildcards
    if urlparse(job["url"]).scheme not in ("http", "https"):
        raise ValueError("scheme not allowed")
    if not host_allowed(urlparse(job["url"]).hostname or "", allow):
        raise ValueError("url outside allow_hosts")
    return {"url": job["url"], "allow_hosts": allow, "screenshot": bool(job.get("screenshot"))}


def encode_result(result: dict[str, Any]) -> str:
    payload = base64.b64encode(json.dumps(result).encode()).decode()
    return f"{RESULT_START}{payload}{RESULT_END}"


def error_label(exc: BaseException) -> str:
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", type(exc).__name__).lower()
    return name if re.fullmatch(r"[a-z][a-z0-9_]{0,39}", name) else "error"


# --- Browser ---------------------------------------------------------------------------------------

async def crawl(job: dict[str, Any]) -> dict[str, Any]:
    """Render the page with every Chromium connection going through the egress proxy (egress_proxy.py):
    exact allowed hosts only, each resolved and checked by the proxy, which connects to that checked
    address. The Sprite's network policy (DNS allowlist; private ranges blocked) is the outer fence."""
    async with EgressProxy(job["allow_hosts"]) as proxy:
        try:
            return await _render(job, proxy)
        except Exception:
            if proxy.refused:  # a clearer label than Chromium's tunnel error
                return {"ok": False, "error": f"egress_{proxy.refused[0]}"}
            raise


async def _render(job: dict[str, Any], proxy: EgressProxy) -> dict[str, Any]:
    from playwright.async_api import async_playwright  # only on the Scout Sprite

    allow: list[str] = job["allow_hosts"]

    async def guard(route, request) -> None:  # second fence, before a request reaches the proxy
        parts = urlparse(request.url)
        if parts.scheme in ("data", "blob"):
            await route.continue_()
            return
        if parts.scheme != "https" or not host_allowed(parts.hostname or "", allow):
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=chromium_args(proxy.url))
        try:
            context = await browser.new_context(
                user_agent=USER_AGENT, accept_downloads=False, java_script_enabled=True,
                viewport={"width": 1280, "height": 1600}, service_workers="block",
            )
            await context.route("**/*", guard)

            async def no_websockets(ws) -> None:  # `route` does not see WebSockets: refuse them all
                await ws.close()

            await context.route_web_socket("**/*", no_websockets)
            page = await context.new_page()
            await page.goto(job["url"], wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            with contextlib.suppress(Exception):  # busy pages never go idle; render what is there
                await page.wait_for_load_state("networkidle", timeout=8_000)
            if not host_allowed(urlparse(page.url).hostname or "", allow):
                return {"ok": False, "error": "redirect_outside_lock"}
            await page.evaluate(EXPAND_JS, MAX_EXPAND)
            await page.wait_for_timeout(SETTLE_MS)
            html = (await page.content())[:MAX_HTML_CHARS]
            text = normalise_text(await page.inner_text("body"))[:MAX_TEXT_CHARS]
            anchors = await page.evaluate(ANCHORS_JS)
            shot = ""
            if job["screenshot"]:
                png = await page.screenshot(full_page=True, type="png")
                if len(png) > MAX_SCREENSHOT_BYTES:
                    png = await page.screenshot(full_page=False, type="png")
                if len(png) <= MAX_SCREENSHOT_BYTES:
                    shot = base64.b64encode(png).decode()
            return {
                "ok": True,
                "final_url": page.url,
                "title": (await page.title())[:300],
                "html": html,
                "text": text,
                "sha256": digest(text),
                "links": extract_policy_links(anchors, page.url, allow),
                "screenshot_png_b64": shot,
            }
        finally:
            await browser.close()


def main() -> int:
    try:
        job = parse_job(sys.stdin.buffer.read())
    except (ValueError, TypeError) as exc:
        print(encode_result({"ok": False, "error": f"bad_job_{error_label(exc)}"}), flush=True)
        return 0
    try:
        result = asyncio.run(crawl(job))
    except Exception as exc:  # noqa: BLE001 — every failure becomes a result the Gateway can read
        result = {"ok": False, "error": error_label(exc)}
    print(encode_result(result), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
