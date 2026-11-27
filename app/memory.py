"""Clause memory in Pinecone.

Every material clause from every service is stored, so a new clause can be compared with what you
have already agreed to elsewhere: "you already accepted similar wording from Spotify and Uber".

Embeddings come from Pinecone Inference (``/embed``); vectors live in a dense 1024-dim cosine index.
Paths follow the Sprites Pinecone connector, which routes data-plane calls by index name
(``/indexes/<index>/...``); the connector does not allow creating indexes, so create the index once
in the Pinecone console (see README).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.gateway import Gateway
from app.metrics import dig

EMBED_MODEL = "llama-text-embed-v2"  # 1024 dimensions
MAX_CHARS = 6000                     # stay inside the embedding model's input limit
METADATA_TEXT_CHARS = 2000           # Pinecone metadata is capped at 40 KB per vector


class Precedent(BaseModel):
    model_config = ConfigDict(frozen=True)

    service: str
    heading: str
    text: str
    score: float


# --- Pinecone wire models --------------------------------------------------------------------------

class _Embedding(BaseModel):
    values: list[float]


class EmbedResponse(BaseModel):
    data: list[_Embedding]


class ClauseMetadata(BaseModel):
    service: str = "?"
    heading: str = ""
    category: str = "other"
    text: str = ""
    # Benchmarking (app/benchmark.py). Vectors stored before these existed read back with the defaults.
    domain: str = "consumer_tos"
    severity: float = 0.0
    favours: str = "neutral"
    has_opt_out: bool = False
    public: bool = True  # only public services' clauses are ever upserted; private uploads are query-only


class Match(BaseModel):
    id: str
    score: float
    metadata: ClauseMetadata = Field(default_factory=ClauseMetadata)


class QueryResponse(BaseModel):
    matches: list[Match] = Field(default_factory=list)


# --- Operations ------------------------------------------------------------------------------------

def _index_path(suffix: str) -> str:
    return f"indexes/{settings.pinecone_index}/{suffix}"


async def embed(
    gw: Gateway, text: str, input_type: Literal["passage", "query"], *, log_body: bool = True
) -> list[float]:
    data = await gw.post(
        "embed",
        log_body=log_body,
        json={
            "model": EMBED_MODEL,
            "parameters": {"input_type": input_type, "truncate": "END"},
            "inputs": [{"text": text[:MAX_CHARS]}],
        },
    )
    gw.report("embed_tokens", dig(data, "usage", "total_tokens"))
    return EmbedResponse.model_validate(data).data[0].values


async def remember(
    gw: Gateway, clause_id: str, service: str, heading: str, text: str, category: str, *,
    domain: str = "consumer_tos", severity: float = 0.0, favours: str = "neutral", has_opt_out: bool = False,
) -> None:
    """Index a public service's material clause. Never called for a member's private document."""
    values = await embed(gw, text, "passage")
    meta = ClauseMetadata(
        service=service, heading=heading, category=category, text=text[:METADATA_TEXT_CHARS],
        domain=domain, severity=severity, favours=favours, has_opt_out=has_opt_out, public=True,
    )
    await gw.post(
        _index_path("vectors/upsert"),
        json={
            "namespace": settings.pinecone_namespace,
            "vectors": [{"id": clause_id, "values": values, "metadata": meta.model_dump()}],
        },
    )


async def similar(
    gw: Gateway,
    text: str,
    exclude_service: str,
    services: list[str],
    top_k: int = 5,
    min_score: float = 0.5,
    vector: list[float] | None = None,
    *,
    log_body: bool = True,
) -> list[Precedent]:
    """Clauses similar to `text`, only from `services` (the asking member's own watchlist).

    Pass `vector` to reuse an embedding when the same change is reported to several members, and
    `log_body=False` when an error body could echo text a member typed or was shown.
    """
    candidates = sorted(set(services) - {exclude_service})
    if not candidates:
        return []
    values = vector or await embed(gw, text, "query", log_body=log_body)
    data = await gw.post(
        _index_path("query"),
        log_body=log_body,
        json={
            "namespace": settings.pinecone_namespace,
            "vector": values,
            "topK": top_k + 3,
            "includeMetadata": True,
            "filter": {"service": {"$in": candidates}},
        },
    )
    gw.report("read_units", dig(data, "usage", "readUnits"))
    found = [
        Precedent(service=m.metadata.service, heading=m.metadata.heading, text=m.metadata.text, score=m.score)
        for m in QueryResponse.model_validate(data).matches
    ]
    return [p for p in found if p.service != exclude_service and p.score >= min_score][:top_k]
