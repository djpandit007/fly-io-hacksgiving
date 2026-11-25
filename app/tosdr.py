"""Community grade from ToS;DR (A best, E worst), shown beside the agent's own verdict.

The response shapes are read defensively: field names have changed between API versions.
"""

from __future__ import annotations

import logging
from typing import Literal

import httpx
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)
API = "https://api.tosdr.org"


class Rating(BaseModel):
    name: str
    grade: Literal["A", "B", "C", "D", "E"] | None = None
    url: str
    points: list[str] = Field(default_factory=list)  # bad points first


def _pick(d: dict, *keys: str, default=None):
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return default


def _grade(raw: object) -> str | None:
    """Ratings appear as "A", "Grade A" or {"letter": "A"} depending on API version."""
    if isinstance(raw, dict):
        raw = raw.get("letter") or raw.get("human")
    letter = str(raw or "").strip().upper().removeprefix("GRADE").strip()
    return letter if letter in ("A", "B", "C", "D", "E") else None


async def lookup(query: str, client: httpx.AsyncClient | None = None) -> Rating | None:
    own = client is None
    client = client or httpx.AsyncClient(timeout=20)
    try:
        resp = await client.get(f"{API}/search/v5/", params={"query": query})
        if resp.status_code != 200:
            return None
        body = resp.json()
        services = _pick(body, "services", default=None) or _pick(body.get("parameters", {}), "services", default=[])
        if not services:
            return None
        svc = services[0]
        sid = _pick(svc, "id")
        detail = await client.get(f"{API}/service/v3/", params={"id": sid})
        data = detail.json() if detail.status_code == 200 else svc
        data = data.get("parameters", data)
        points = data.get("points") or []
        bad = [p for p in points if (p.get("case") or {}).get("classification") in ("bad", "blocker")]
        titles = [_pick(p, "title", default="") for p in bad or points][:5]
        return Rating(
            name=_pick(data, "name", default=query),
            grade=_grade(_pick(data, "rating", "grade", default=None)),
            url=f"https://tosdr.org/en/service/{sid}",
            points=[t for t in titles if t],
        )
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
        log.info("ToS;DR lookup failed for %s: %s", query, exc)
        return None
    finally:
        if own:
            await client.aclose()
