"""Find the terms-of-service page for a domain."""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.watch import USER_AGENT

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


async def find_terms_url(domain: str, client: httpx.AsyncClient | None = None) -> str | None:
    own = client is None
    client = client or httpx.AsyncClient(follow_redirects=True, timeout=20, headers={"User-Agent": USER_AGENT})
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
