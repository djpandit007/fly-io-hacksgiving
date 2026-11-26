"""Backfill past versions of a terms page, so changes from the last year show up on day one.

1. Open Terms Archive publishes versions as git repos (``Service/Document.md``, one commit per
   change). Used when the service is in a collection.
2. Otherwise the Wayback Machine: the CDX API lists distinct captures, and ``id_`` URLs return the
   original HTML without the archive toolbar.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel

from app.watch import USER_AGENT, html_to_markdown, markdown_to_canonical, normalise

log = logging.getLogger(__name__)

OTA_REPOS = ("OpenTermsArchive/pga-versions", "OpenTermsArchive/contrib-versions")
OTA_DOCS = ("Terms of Service", "Privacy Policy")


class Version(BaseModel):
    taken_at: datetime          # when this text was in force
    text: str                   # normalised
    origin: Literal["ota", "wayback"]


async def ota_versions(client: httpx.AsyncClient, service: str, since: datetime, document: str = "Terms of Service") -> list[Version]:
    path = f"{service}/{document}.md"
    for repo in OTA_REPOS:
        resp = await client.get(
            f"https://api.github.com/repos/{repo}/commits",
            params={"path": path, "since": since.isoformat(), "per_page": 30},
            headers={"Accept": "application/vnd.github+json"},
        )
        if resp.status_code != 200 or not resp.json():
            continue
        versions = []
        for commit in reversed(resp.json()):  # oldest first
            raw = await client.get(f"https://raw.githubusercontent.com/{repo}/{commit['sha']}/{quote(path)}")
            if raw.status_code == 200:
                versions.append(Version(taken_at=commit["commit"]["committer"]["date"], text=normalise(markdown_to_canonical(raw.text)), origin="ota"))
        if versions:
            return versions
    return []


async def wayback_versions(client: httpx.AsyncClient, url: str, since: datetime, limit: int = 8) -> list[Version]:
    resp = await client.get(
        "https://web.archive.org/cdx/search/cdx",
        params={
            "url": url,
            "output": "json",
            "fl": "timestamp,digest",
            "filter": "statuscode:200",
            "collapse": "digest",
            "from": since.strftime("%Y%m%d"),
        },
    )
    if resp.status_code != 200 or not resp.text.strip():
        return []
    rows = resp.json()[1:]  # first row is the header
    if len(rows) > limit:  # keep first, last and an even spread between
        step = (len(rows) - 1) / (limit - 1)
        rows = [rows[round(i * step)] for i in range(limit)]
    versions: list[Version] = []
    for ts, _digest in rows:
        page = await client.get(f"https://web.archive.org/web/{ts}id_/{url}")
        if page.status_code != 200:
            continue
        text = normalise(html_to_markdown(page.text))
        if versions and versions[-1].text == text:
            continue
        taken = datetime.strptime(ts, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        versions.append(Version(taken_at=taken, text=text, origin="wayback"))
    return versions


async def backfill(service: str, url: str, days: int = 365) -> list[Version]:
    since = datetime.now(UTC) - timedelta(days=days)
    async with httpx.AsyncClient(follow_redirects=True, timeout=45, headers={"User-Agent": USER_AGENT}) as client:
        try:
            versions = await ota_versions(client, service, since)
        except httpx.HTTPError as exc:
            log.info("OTA lookup failed for %s: %s", service, exc)
            versions = []
        if not versions:
            try:
                versions = await wayback_versions(client, url, since)
            except httpx.HTTPError as exc:
                log.info("Wayback lookup failed for %s: %s", url, exc)
    return versions
