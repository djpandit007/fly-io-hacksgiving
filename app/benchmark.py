"""Clause market benchmarking: how a clause's severity compares with comparable public clauses.

Pinecone holds every public service's material clauses with their category, severity, favours and
opt-out metadata (app/memory.py). A clause is compared with its nearest public neighbours in the same
category, one per service; with fewer than MIN_COHORT distinct services there is no benchmark at all
rather than a noisy one. The percentile is plain arithmetic over the cohort's stored metadata.

Benchmarking only *queries* the index. A member's private document is never upserted, and the query
is sent with `log_body=False` so an error body echoing its text is never logged.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from app import memory
from app.config import settings
from app.gateway import Gateway
from app.judge import Verdict
from app.metrics import dig

TOP_K = 50
MIN_COHORT = 10          # distinct public services needed before any percentile is shown
MAX_NAMED = 3            # comparable services named in a peer summary
OPT_OUT_MIN = 0.7        # Verdict.needs_action: an opt-out the judge is sure of


class Benchmark(BaseModel):
    model_config = ConfigDict(frozen=True)

    percentile: float              # % of the cohort whose clause is less severe than this one (0-100)
    cohort_size: int               # distinct public services compared
    opt_out_share: float           # share of the cohort whose clause offers an opt-out (0-1)
    comparable_services: list[str]  # the closest public services, at most MAX_NAMED

    def line(self, category: str) -> str:
        """A short plain-text peer comparison (escape before putting it in HTML)."""
        text = (
            f"More severe than {self.percentile:.0f}% of comparable {category} clauses "
            f"across {self.cohort_size} public services"
        )
        if self.opt_out_share:
            text += f"; {self.opt_out_share:.0%} of them offer an opt-out"
        return text + "."


SCALE = "clause"  # severities are judge.clause_severity scores: how much the clause itself matters


def clause_metadata(
    verdict: Verdict | None, domain: str = "consumer_tos", clause_severity: float | None = None
) -> dict[str, Any]:
    """The benchmark metadata a public material clause is upserted with.

    `severity` is the clause's own severity, the scale a private clause is benchmarked on; never the
    change's severity (how much the wording moved, even in the reader's favour). Without a clause
    severity the clause is indexed but stays out of benchmark cohorts."""
    meta: dict[str, Any] = {"domain": domain}
    if verdict is not None:
        meta |= {"favours": verdict.favours, "has_opt_out": verdict.opt_out >= OPT_OUT_MIN}
    if clause_severity is not None:
        meta |= {"severity": clause_severity, "scale": SCALE}
    return meta


def percentile(severity: float, cohort: list[float]) -> float:
    """Share of the cohort (in %) strictly less severe than `severity`. Deterministic; 0 for no cohort."""
    if not cohort:
        return 0.0
    return 100.0 * sum(1 for s in cohort if s < severity) / len(cohort)


def from_matches(
    matches: list[memory.Match], severity: float, exclude_service: str | None = None
) -> Benchmark | None:
    """The benchmark over a query's matches: nearest clause per public service, or None if too few."""
    per_service: dict[str, memory.ClauseMetadata] = {}
    for m in sorted(matches, key=lambda m: -m.score):
        meta = m.metadata
        if not meta.public or meta.scale != SCALE or meta.service in ("", "?") or meta.service == exclude_service:
            continue
        per_service.setdefault(meta.service, meta)
    if len(per_service) < MIN_COHORT:
        return None
    cohort = list(per_service.values())
    return Benchmark(
        percentile=percentile(severity, [c.severity for c in cohort]),
        cohort_size=len(cohort),
        opt_out_share=sum(1 for c in cohort if c.has_opt_out) / len(cohort),
        comparable_services=list(per_service)[:MAX_NAMED],
    )


async def benchmark_clause(
    gw: Gateway, text: str, category: str, severity: float, exclude_service: str | None = None,
    *, vector: list[float] | None = None,
) -> Benchmark | None:
    """Where `text` (a clause of `category` with this 0-3 severity) sits among comparable public clauses.

    Read-only: one embed (unless `vector` is given) and one query; never an upsert. None when fewer
    than MIN_COHORT distinct public services are comparable.
    """
    values = vector or await memory.embed(gw, text, "query", log_body=False)
    data = await gw.post(
        f"indexes/{settings.pinecone_index}/query",
        log_body=False,
        json={
            "namespace": settings.pinecone_namespace,
            "vector": values,
            "topK": TOP_K,
            "includeMetadata": True,
            # Only clauses scored on the same scale as `severity`: older vectors hold a change's severity.
            "filter": {"public": True, "category": category, "scale": SCALE},
        },
    )
    gw.report("read_units", dig(data, "usage", "readUnits"))
    return from_matches(memory.QueryResponse.model_validate(data).matches, severity, exclude_service)
