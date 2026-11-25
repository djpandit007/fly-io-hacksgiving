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


# Hosts where anyone can publish a page that looks like a terms page.
USER_CONTENT_HOSTS = (
    "github.io", "githubusercontent.com", "gitlab.io", "medium.com", "substack.com", "notion.site",
    "sites.google.com", "docs.google.com", "drive.google.com", "wordpress.com", "blogspot.com",
    "tumblr.com", "wixsite.com", "weebly.com", "squarespace.com", "pages.dev", "netlify.app",
    "vercel.app", "herokuapp.com", "fly.dev", "sprites.app", "carrd.co", "linktr.ee", "scribd.com",
    "dropbox.com", "box.com", "pastebin.com", "reddit.com", "linkedin.com/pulse",
)
TWO_PART_TLDS = {"co.uk", "com.au", "co.in", "co.jp", "com.br", "co.nz", "org.uk", "ac.uk"}


def registrable_domain(host: str) -> str:
    parts = host.lower().strip(".").split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in TWO_PART_TLDS:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def is_user_content(url: str) -> bool:
    u = urlparse(url)
    host = (u.hostname or "").lower()
    hp = f"{host}{u.path}".lower()
    return any(host == h or host.endswith("." + h) or hp.startswith(h) for h in USER_CONTENT_HOSTS)


def _name_matches(url: str, name: str) -> bool:
    """For a name-only query, the result must be on that name's own domain (youtube -> youtube.com)."""
    label = registrable_domain(urlparse(url).hostname or "").split(".")[0]
    return label == re.sub(r"[^a-z0-9-]", "", name.lower())


def _domain_of(query: str) -> str | None:
    q = query.strip().lower().removeprefix("https://").removeprefix("http://").split("/")[0]
    return q.removeprefix("www.") if "." in q else None


def pick_terms_result(results: list[exa.Result], domain: str | None, name: str | None = None) -> str | None:
    """First search result that looks like a terms page on the service's own domain.

    With a domain, results must be on it. With only a name, the result's registrable domain must be
    that name (`notion` -> notion.so / notion.com). User-content hosts are never accepted.
    """
    def ok(r: exa.Result) -> bool:
        host = (urlparse(r.url).hostname or "").removeprefix("www.")
        if urlparse(r.url).scheme not in ("http", "https") or is_user_content(r.url):
            return False
        if domain and not (host == domain or host.endswith("." + domain)):
            return False
        if not domain and (not name or not _name_matches(r.url, name)):
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
            found = pick_terms_result(await exa.search(cl.exa, f"{name} terms of service", domain=domain), domain, name)
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
