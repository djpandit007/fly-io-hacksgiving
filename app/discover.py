"""Find the terms-of-service page for a service, from a name ("notion") or a domain ("notion.so").

Web search (Exa) first; the service's own home page links and common paths as fallback. Then the
service's Policy Bundle: companion legal documents linked from that terms page or its legal hub.
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
from app.db import DOC_ROLES
from app.netguard import BlockedURL, resolve_public, safe_client

if TYPE_CHECKING:
    from app.agent import Clients

log = logging.getLogger(__name__)
TERMS_URL = re.compile(r"(terms|tos\b|legal|user[-_]?agreement|conditions|eula)", re.IGNORECASE)
TERMS_PATH = re.compile(r"(terms|tos\b|agreement|conditions|eula)", re.IGNORECASE)
NEVER_TERMS = re.compile(r"(/blog|/news|/help/article|/community|/forum)", re.IGNORECASE)
OTHER_POLICY = re.compile(r"(privacy|cookie)", re.IGNORECASE)

LINK_TEXT = re.compile(r"\b(terms(\s+of\s+(service|use))?|user\s+agreement|conditions\s+of\s+use|legal\s+terms)\b", re.IGNORECASE)
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
        rank = 0 if re.search(r"terms\s+of\s+(service|use)", text, re.IGNORECASE) else 1
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

# Bundled Public Suffix List, no network. Private suffixes (github.io, sprites.app, ...) are included so
# "acme.github.io" stays its own domain instead of widening to every page on github.io.
_PSL = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None, include_psl_private_domains=True)


def registrable_domain(host: str) -> str:
    parts = _PSL(host.lower().strip("."))
    return parts.top_domain_under_public_suffix or host.lower().strip(".")


def _host_is(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith("." + suffix)


def _path_under(path: str, prefix: str) -> bool:
    """Whole-segment prefix match: /terms matches /terms and /terms/x, not /terms123."""
    path, prefix = path.lower().rstrip("/") or "/", prefix.lower().rstrip("/")
    return not prefix or path == prefix or path.startswith(prefix + "/")


def is_user_content(url: str) -> bool:
    u = urlparse(url)
    host = (u.hostname or "").lower().removeprefix("www.")
    path = u.path or "/"
    if any(_host_is(host, h) and _path_under(path, prefix) for h, prefix in USER_CONTENT):
        return True
    for h, official in USER_PATH_HOSTS.items():
        if host == h and not any(_path_under(path, p) for p in official):
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


async def find_terms_url(query: str, cl: Clients | None = None, client: httpx.AsyncClient | None = None) -> str | None:
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
        except Exception as exc:  # noqa: BLE001
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
            if resp.status_code == 200 and re.search(r"terms", resp.text[:20000], re.IGNORECASE):
                return str(resp.url)
        return None
    finally:
        if own:
            await client.aclose()


# --- Policy Bundles: the companion documents linked from a service's terms page ---------------------

COMPANION_ROLES = ("dpa", "ai_terms", "billing", "privacy")  # most specific first: one role per link
# Link text is stronger evidence than the URL path; a path match alone ranks below any text match.
COMPANION_TEXT: dict[str, re.Pattern[str]] = {
    "dpa": re.compile(r"\bdata\s+processing\s+(addendum|agreement|terms)\b|\bDPA\b", re.IGNORECASE),
    "ai_terms": re.compile(
        r"\b(ai|artificial\s+intelligence|generative\s+ai|machine\s+learning)\b.{0,30}?"
        r"\b(terms|policy|addendum|supplement|agreement|principles)\b",
        re.IGNORECASE,
    ),
    "billing": re.compile(r"\b(billing|payments?|subscriptions?|refunds?)\s+(terms|policy|agreement)\b", re.IGNORECASE),
    "privacy": re.compile(r"\bprivacy\s+(policy|notice|statement)\b|^privacy$", re.IGNORECASE),
}
COMPANION_PATH: dict[str, re.Pattern[str]] = {
    "dpa": re.compile(r"(^|[/_-])dpa([/_.-]|$)|data[-_]processing", re.IGNORECASE),
    "ai_terms": re.compile(r"(^|[/_-])(ai|genai|generative-ai)[-_]?(terms|policy|addendum|supplement)", re.IGNORECASE),
    "billing": re.compile(r"(billing|payments?|subscriptions?|refunds?)[-_]?(terms|policy)", re.IGNORECASE),
    "privacy": re.compile(r"privacy([-_](policy|notice|statement))?([/_.-]|$)", re.IGNORECASE),
}
NOT_COMPANION = re.compile(r"cookie|/blog|/news|/help/article|/community|/forum|\.(png|jpe?g|svg|zip)$", re.IGNORECASE)
LEGAL_HUB_TEXT = re.compile(r"^(legal|legal\s+(center|centre|hub|information|policies)|policies)$", re.IGNORECASE)
LEGAL_HUB_PATH = re.compile(r"/(legal|policies)/?$", re.IGNORECASE)

# Bound the work one discovery does: the terms page plus at most one linked legal hub.
MAX_DISCOVERY_PAGES = 2


def _plain_url(url: str) -> str:
    """https, host and path only: no credentials, query or fragment (the shape services are stored in)."""
    u = urlparse(url.strip())
    host = (u.hostname or "").lower()
    port = f":{u.port}" if u.port and u.port not in (80, 443) else ""
    return f"https://{host}{port}{u.path.rstrip('/') or '/'}"


def _same_page(a: str, b: str) -> bool:
    def key(url: str) -> str:
        u = urlparse(url)
        return f"{(u.hostname or '').lower().removeprefix('www.')}{u.path.rstrip('/')}"

    return key(a) == key(b)


def _on_domain(url: str, domain: str) -> bool:
    u = urlparse(url)
    return u.scheme in ("http", "https") and bool(u.hostname) and registrable_domain(u.hostname or "") == domain


def companion_links(html: str, base_url: str, domain: str) -> dict[str, tuple[int, str]]:
    """Candidate companion documents linked from one page: role -> (rank, url), rank 0 best.

    Only http(s) links on `domain` (the service's registrable domain) that are not user content are
    considered. Their addresses are checked later, before any is kept.
    """
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, tuple[int, str]] = {}
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        path = urlparse(href).path
        if not _on_domain(href, domain) or is_user_content(href) or NOT_COMPANION.search(path):
            continue
        text = " ".join(a.get_text(" ", strip=True).split())
        if "cookie" in text.lower():
            continue
        for role in COMPANION_ROLES:
            if COMPANION_TEXT[role].search(text):
                rank = 0
            elif COMPANION_PATH[role].search(path):
                rank = 1
            else:
                continue
            if role not in found or rank < found[role][0]:
                found[role] = (rank, _plain_url(href))
            break  # one role per link: a "Data Processing Addendum" is not also the privacy policy
    return found


def legal_hub(html: str, base_url: str, domain: str) -> str | None:
    """The page's link to the service's legal hub ("Legal", /legal, /policies) on `domain`, if any."""
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        if not _on_domain(href, domain) or is_user_content(href):
            continue
        text = " ".join(a.get_text(" ", strip=True).split())
        if LEGAL_HUB_TEXT.match(text) or LEGAL_HUB_PATH.search(urlparse(href).path):
            return _plain_url(href)
    return None


async def _is_public(url: str) -> bool:
    try:
        await resolve_public(urlparse(url).hostname or "")
        return True
    except BlockedURL:
        log.info("skipped a companion link that is not on a public address")
        return False


async def discover_companions(terms_url: str, client: httpx.AsyncClient | None = None) -> dict[str, str]:
    """Companion legal documents (privacy, ai_terms, billing, dpa) linked from the terms page or its
    legal hub, on the terms page's own registrable domain: role -> URL.

    Pages are fetched through the netguard client, and every link kept must resolve to public
    addresses only; one that does not is skipped and never fetched. Failures give {}.
    """
    host = urlparse(terms_url).hostname or ""
    if not host:
        return {}
    domain = registrable_domain(host)
    own = client is None
    client = client or safe_client(timeout=20)
    best: dict[str, tuple[int, str]] = {}
    try:
        pages, fetched = [terms_url], 0
        while pages and fetched < MAX_DISCOVERY_PAGES:
            page = pages.pop(0)
            fetched += 1
            try:
                resp = await client.get(page)
            except httpx.HTTPError as exc:
                log.info("companion discovery fetch failed: %s", type(exc).__name__)
                continue
            if resp.status_code != 200 or "html" not in resp.headers.get("content-type", "text/html"):
                continue
            if registrable_domain(resp.url.host or "") != domain:  # redirected off the service's domain
                continue
            base = str(resp.url)
            for role, (rank, url) in companion_links(resp.text, base, domain).items():
                if role not in best or rank < best[role][0]:
                    best[role] = (rank, url)
            if fetched == 1 and (hub := legal_hub(resp.text, base, domain)) and not _same_page(hub, terms_url):
                pages.append(hub)
    finally:
        if own:
            await client.aclose()

    bundle: dict[str, str] = {}
    for role in DOC_ROLES:
        if role not in best:
            continue
        url = best[role][1]
        if _same_page(url, terms_url) or any(_same_page(url, kept) for kept in bundle.values()):
            continue
        if await _is_public(url):
            bundle[role] = url
    return bundle


async def discover_bundle(
    query: str, cl: Clients | None = None, client: httpx.AsyncClient | None = None
) -> dict[str, str] | None:
    """A service's Policy Bundle from a name, domain or terms URL: role -> URL, always with `terms`.

    None when no terms page is found (the member is then asked for the URL).
    """
    terms = query if query.startswith("http") else await find_terms_url(query, cl, client)
    if not terms:
        return None
    return {"terms": terms, **await discover_companions(terms, client)}
