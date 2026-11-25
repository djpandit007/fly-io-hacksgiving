"""Find the terms-of-service page for a service, from a name ("notion") or a domain ("notion.so").

Web search (Exa) first; the service's own home page links and common paths as fallback.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app import exa
from app.netguard import safe_client

if TYPE_CHECKING:
    from app.agent import Clients

log = logging.getLogger(__name__)
TERMS_URL = re.compile(r"(terms|tos\b|legal|user[-_]?agreement|conditions|eula)", re.I)
TERMS_PATH = re.compile(r"(terms|tos\b|agreement|conditions|eula)", re.I)
NEVER_TERMS = re.compile(r"(/blog|/news|/help/article|/community|/forum)", re.I)
OTHER_POLICY = re.compile(r"(privacy|cookie)", re.I)

LINK_TEXT = re.compile(r"\b(terms(\s+of\s+(service|use))?|user\s+agreement|conditions\s+of\s+use|legal\s+terms)\b", re.I)
GUESSES = ("/terms", "/terms-of-service", "/legal/terms", "/tos", "/terms-of-use", "/legal")


def terms_link(html: str, base_url: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    best: tuple[int, str] | None = None
    for a in soup.find_all("a", href=True):
        text = " ".join(a.get_text(" ", strip=True).split())
        if not LINK_TEXT.search(text) or "privacy" in text.lower():
            continue
        href = urljoin(base_url, a["href"])
        if urlparse(href).scheme not in ("http", "https"):
            continue
        rank = 0 if re.search(r"terms\s+of\s+(service|use)", text, re.I) else 1
        if best is None or rank < best[0]:
            best = (rank, href)
    return best[1] if best else None


def _domain_of(query: str) -> str | None:
    q = query.strip().lower().removeprefix("https://").removeprefix("http://").split("/")[0]
    return q.removeprefix("www.") if "." in q else None


def pick_terms_result(results: list[exa.Result], domain: str | None) -> str | None:
    """First search result that looks like a terms page (on the service's domain when known)."""
    def ok(r: exa.Result) -> bool:
        host = (urlparse(r.url).hostname or "").removeprefix("www.")
        if urlparse(r.url).scheme not in ("http", "https"):
            return False
        if domain and not (host == domain or host.endswith("." + domain)):
            return False
        path = urlparse(r.url).path
        if NEVER_TERMS.search(path) or (OTHER_POLICY.search(path) and not TERMS_PATH.search(path)):
            return False
        return bool(TERMS_URL.search(f"{path} {r.title or ''}"))

    return next((r.url for r in results if ok(r)), None)


async def find_terms_url(query: str, cl: "Clients | None" = None, client: httpx.AsyncClient | None = None) -> str | None:
    domain = _domain_of(query)
    name = query.strip() if domain is None else domain.split(".")[0]
    if cl is not None and cl.exa is not None:
        try:
            found = pick_terms_result(await exa.search(cl.exa, f"{name} terms of service", domain=domain), domain)
            if found:
                return found
        except Exception as exc:
            log.warning("exa search failed for %s: %s", query, exc)
    if domain is None:
        domain = f"{name.replace(' ', '').lower()}.com"  # best guess for the scraper fallback
    return await _scrape_terms_url(domain, client)


async def _scrape_terms_url(domain: str, client: httpx.AsyncClient | None = None) -> str | None:
    own = client is None
    client = client or safe_client(timeout=20)
    try:
        for home in (f"https://{domain}/", f"https://www.{domain}/"):
            try:
                resp = await client.get(home)
            except httpx.HTTPError:
                continue
            if resp.status_code == 200 and (link := terms_link(resp.text, str(resp.url))):
                return link
        for path in GUESSES:
            try:
                resp = await client.get(f"https://{domain}{path}")
            except httpx.HTTPError:
                continue
            if resp.status_code == 200 and re.search(r"terms", resp.text[:20000], re.I):
                return str(resp.url)
        return None
    finally:
        if own:
            await client.aclose()
