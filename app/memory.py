"""Clause memory in Pinecone, using an index with integrated embedding (records API).

Every material clause from every service is stored, so a new clause can be compared with what you
have already agreed to elsewhere: "you already accepted the same arbitration clause from 3 others".
The index must be created with ``field_map = {"text": "chunk_text"}`` (see scripts/setup_pinecone.sh).
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.gateway import Gateway


class Precedent(BaseModel):
    model_config = ConfigDict(frozen=True)

    service: str
    heading: str
    text: str
    score: float


class ClauseFields(BaseModel):
    service: str = "?"
    heading: str = ""
    chunk_text: str = ""


class Hit(BaseModel):
    id: str = Field(alias="_id")
    score: float = Field(alias="_score")
    fields: ClauseFields = Field(default_factory=ClauseFields)


class _Result(BaseModel):
    hits: list[Hit] = Field(default_factory=list)


class SearchResponse(BaseModel):
    result: _Result = Field(default_factory=_Result)


async def remember(gw: Gateway, clause_id: str, service: str, heading: str, text: str, category: str) -> None:
    record = {"_id": clause_id, "chunk_text": text[:8000], "service": service, "heading": heading, "category": category}
    await gw.request(
        "POST",
        f"records/namespaces/{settings.pinecone_namespace}/upsert",
        content=json.dumps(record) + "\n",
        headers={"Content-Type": "application/x-ndjson"},
    )


async def similar(gw: Gateway, text: str, exclude_service: str, top_k: int = 5, min_score: float = 0.5) -> list[Precedent]:
    data = await gw.post(
        f"records/namespaces/{settings.pinecone_namespace}/search",
        json={
            "query": {"inputs": {"text": text[:8000]}, "top_k": top_k + 3},
            "fields": ["service", "heading", "chunk_text"],
        },
    )
    found = [
        Precedent(service=h.fields.service, heading=h.fields.heading, text=h.fields.chunk_text, score=h.score)
        for h in SearchResponse.model_validate(data).result.hits
    ]
    return [p for p in found if p.service != exclude_service and p.score >= min_score][:top_k]
