"""Exa: web search (find a service's terms page) and page contents (read pages that block scrapers).

The Sprites Exa connector forwards only POST /search and POST /contents, which is all this uses.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.gateway import Gateway


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
    return Results.model_validate(await gw.post("search", json=body)).results


async def contents(gw: Gateway, url: str) -> str:
    data = Results.model_validate(await gw.post("contents", json={"urls": [url], "text": True}))
    return (data.results[0].text or "") if data.results else ""
