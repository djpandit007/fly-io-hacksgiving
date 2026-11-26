"""Exa: web search (find a service's terms page) and page contents (read pages that block scrapers).

The Sprites Exa connector forwards only POST /search and POST /contents, which is all this uses.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.gateway import Gateway
from app.metrics import dig


class Result(BaseModel):
    url: str
    title: str | None = None
    text: str | None = None


class Results(BaseModel):
    results: list[Result] = Field(default_factory=list)


async def search(gw: Gateway, query: str, domain: str | None = None, num: int = 5) -> list[Result]:
    body: dict = {"query": query, "numResults": num, "type": "auto"}
    if domain:
        body["includeDomains"] = [domain]
    data = await gw.post("search", json=body)
    gw.report("usd", dig(data, "costDollars", "total"))
    return Results.model_validate(data).results


async def contents(gw: Gateway, url: str) -> str:
    data = await gw.post("contents", json={"urls": [url], "text": True})
    gw.report("usd", dig(data, "costDollars", "total"))
    results = Results.model_validate(data).results
    return (results[0].text or "") if results else ""
