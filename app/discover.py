"""Find the terms-of-service page for a service, from a name ("notion") or a domain ("notion.so").

Web search (Exa) first; the service's own home page links and common paths as fallback.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

import httpx
import tldextract
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


# Where anyone can publish a page that looks like a terms page: (host suffix, path prefix).
# An empty prefix means the whole host.
USER_CONTENT: tuple[tuple[str, str], ...] = (
    ("github.io", ""), ("githubusercontent.com", ""), ("gitlab.io", ""), ("medium.com", ""),
    ("substack.com", ""), ("notion.site", ""), ("sites.google.com", ""), ("docs.google.com", ""),
    ("drive.google.com", ""), ("wordpress.com", ""), ("blogspot.com", ""), ("tumblr.com", ""),
    ("wixsite.com", ""), ("weebly.com", ""), ("squarespace.com", ""), ("pages.dev", ""), ("netlify.app", ""),
    ("vercel.app", ""), ("herokuapp.com", ""), ("fly.dev", ""), ("sprites.app", ""), ("carrd.co", ""),
    ("linktr.ee", ""), ("scribd.com", ""), ("dropbox.com", ""), ("box.com", ""), ("pastebin.com", ""),
    ("reddit.com", ""), ("linkedin.com", "/pulse"), ("linkedin.com", "/posts"),
)
# Shared hosts where every path is user content except the operator's own policy pages.
USER_PATH_HOSTS: dict[str, tuple[str, ...]] = {
    "github.com": ("/site-policy", "/customer-terms", "/github/site-policy"),
    "gitlab.com": ("/handbook/legal", "/terms"),
    "x.com": ("/en/tos", "/tos", "/en/privacy", "/privacy"),
    "twitter.com": ("/en/tos", "/tos", "/en/privacy", "/privacy"),
}

_PSL = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)  # bundled Public Suffix List, no network


def registrable_domain(host: str) -> str:
    parts = _PSL(host.lower().strip("."))
    return parts.top_domain_under_public_suffix or host.lower().strip(".")


def _host_is(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith("." + suffix)


def is_user_content(url: str) -> bool:
    u = urlparse(url)
    host = (u.hostname or "").lower().removeprefix("www.")
    path = u.path or "/"
    if any(_host_is(host, h) and path.lower().startswith(prefix) for h, prefix in USER_CONTENT):
        return True
    for h, official in USER_PATH_HOSTS.items():
        if host == h and not any(path.lower().startswith(p) for p in official):
            return True
    return False


async def official_domain(name: str, client: httpx.AsyncClient | None = None) -> str | None:
    """The brand's own registrable domain: where https://<name>.com finally lands (public hosts only).

    A search result whose domain merely *contains* the name (youtube.co, notion-terms.io) proves
    nothing; the .com redirect chain is controlled by the brand.
    """
    label = re.sub(r"[^a-z0-9-]", "", name.lower())
    if not label:
        return None
    own = client is None
    client = client or safe_client(timeout=15)
    try:
        resp = await client.get(f"https://{label}.com/")
        if resp.status_code >= 400:
            return None
        return registrable_domain(resp.url.host or "") or None
    except httpx.HTTPError:
        return None
    finally:
        if own:
            await client.aclose()


def _domain_of(query: str) -> str | None:
    q = query.strip().lower().removeprefix("https://").removeprefix("http://").split("/")[0]
    return q.removeprefix("www.") if "." in q else None


def pick_terms_result(results: list[exa.Result], domain: str | None) -> str | None:
    """First search result that looks like a terms page on `domain` (required). User content is rejected."""
    def ok(r: exa.Result) -> bool:
        host = (urlparse(r.url).hostname or "").lower().removeprefix("www.")
        if not domain or urlparse(r.url).scheme not in ("http", "https") or is_user_content(r.url):
            return False
        if not _host_is(host, domain):
            return False
        path = urlparse(r.url).path
        if NEVER_TERMS.search(path) or (OTHER_POLICY.search(path) and not TERMS_PATH.search(path)):
            return False
        return bool(TERMS_URL.search(f"{path} {r.title or ''}"))

    return next((r.url for r in results if ok(r)), None)


async def find_terms_url(query: str, cl: "Clients | None" = None, client: httpx.AsyncClient | None = None) -> str | None:
    """A terms URL on the service's own domain, or None (then the member is asked for the URL).

    A domain query searches that domain. A name query first establishes the brand's official domain
    from https://<name>.com; without one, no search result is trusted.
    """
    domain = _domain_of(query)
    if domain is None:
        domain = await official_domain(query, client)
        if domain is None:
            return None
    else:
        domain = registrable_domain(domain)  # subdomains (docs.github.com) still match below
    name = domain.split(".")[0]
    if cl is not None and cl.exa is not None:
        try:
            found = pick_terms_result(await exa.search(cl.exa, f"{name} terms of service", domain=domain), domain)
            if found:
                return found
        except Exception as exc:
            log.warning("exa search failed for %s: %s", query, exc)
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
