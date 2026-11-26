"""Document intelligence with LlamaParse (v2) and LlamaExtract (v2).

- ``parse_*``: any PDF, scan, screenshot or HTML page into per-page Markdown.
- ``extract_deadlines``: schema-based extraction of the dated obligations in a personal document,
  with page citations, feeding the ledger.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import settings
from app.gateway import Gateway

Tier = Literal["fast", "cost_effective", "agentic", "agentic_plus"]
JobStatus = Literal["PENDING", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"]
TIER: Tier = "cost_effective"
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}


# --- LlamaCloud wire models -----------------------------------------------------------------------

class Job(BaseModel):
    id: str
    status: JobStatus
    error_message: str | None = None


class MarkdownPage(BaseModel):
    page_number: int
    success: bool = True
    markdown: str = ""


class _MarkdownPages(BaseModel):
    pages: list[MarkdownPage] = Field(default_factory=list)


class ParseResult(BaseModel):
    job: Job
    markdown: _MarkdownPages | None = None


class Citation(BaseModel):
    page: int | None = None
    matching_text: str = ""


class FieldMeta(BaseModel):
    citation: list[Citation] = Field(default_factory=list)
    confidence: float | None = None


class ExtractJob(Job):
    extract_result: dict[str, Any] | None = None
    extract_metadata: dict[str, Any] | None = None


# --- Our types -----------------------------------------------------------------------------------

class Page(BaseModel):
    number: int
    markdown: str


class Parsed(BaseModel):
    job_id: str
    pages: list[Page]

    @property
    def markdown(self) -> str:
        return "\n\n".join(p.markdown for p in self.pages)


async def _poll(gw: Gateway, path: str, params: dict[str, Any] | None = None, timeout: float = 300) -> dict:
    """Poll until the job is terminal. Parse nests the job under ``job``; Extract returns it flat."""
    delay, waited = 2.0, 0.0
    while True:
        data = await gw.get(path, params=params)
        job = Job.model_validate(data.get("job") or data)
        status = job.status
        if status in TERMINAL:
            if status != "COMPLETED":
                raise RuntimeError(f"LlamaCloud job {path} ended {status}: {job.error_message}")
            return data
        if waited > timeout:
            raise TimeoutError(f"LlamaCloud job {path} still {status} after {timeout}s")
        await asyncio.sleep(delay)
        waited += delay
        delay = min(delay * 1.5, 10)


async def _result(gw: Gateway, job_id: str) -> Parsed:
    await _poll(gw, f"api/v2/parse/{job_id}")
    result = ParseResult.model_validate(await gw.get(f"api/v2/parse/{job_id}", params={"expand": "markdown"}))
    pages = [Page(number=p.page_number, markdown=p.markdown) for p in (result.markdown.pages if result.markdown else []) if p.success]
    return Parsed(job_id=job_id, pages=pages)


async def parse_bytes(gw: Gateway, filename: str, content: bytes, tier: str = TIER) -> Parsed:
    job = Job.model_validate(
        await gw.post(
            "api/v2/parse/upload",
            files={"file": (filename, content)},
            data={"configuration": json.dumps({"tier": tier, "version": "latest"})},
        )
    )
    return await _result(gw, job.id)


async def parse_url(gw: Gateway, url: str, tier: str = TIER) -> Parsed:
    job = Job.model_validate(await gw.post("api/v2/parse", json={"source_url": url, "tier": tier, "version": "latest"}))
    return await _result(gw, job.id)


DEADLINE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "document_title": {"type": "string", "description": "What this document is, e.g. 'Apartment lease, 12 Elm St'."},
        "counterparty": {"type": "string", "description": "The company or person on the other side."},
        "deadlines": {
            "type": "array",
            "description": "Every date-bound obligation, right or window for the person who holds this document.",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short action, e.g. 'Give notice to end lease'."},
                    "kind": {"type": "string", "enum": ["opt_out", "renewal", "trial", "return", "notice", "payment", "other"]},
                    "date": {"type": "string", "description": "ISO date YYYY-MM-DD if stated or computable from the document, else empty."},
                    "rule": {"type": "string", "description": "The rule as written, e.g. '60 days before the end of the term'."},
                },
                "required": ["title", "kind", "rule"],
            },
        },
    },
    "required": ["document_title", "deadlines"],
}


DeadlineKind = Literal["opt_out", "renewal", "trial", "return", "notice", "payment", "other"]


class Deadline(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    title: str
    kind: DeadlineKind = "other"
    due: dt.date | None = Field(default=None, alias="date")
    rule: str = ""
    page: int | None = None

    @field_validator("due", mode="before")
    @classmethod
    def _blank_date(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip()
            try:
                return dt.date.fromisoformat(v) if v else None
            except ValueError:
                return None  # "the 1st of each month" etc.: keep the rule, no date
        return v


class DocumentDeadlines(BaseModel):
    document_title: str = "Untitled document"
    counterparty: str = ""
    deadlines: list[Deadline] = Field(default_factory=list)


async def extract_deadlines(gw: Gateway, parse_job_id: str) -> DocumentDeadlines:
    """Run LlamaExtract on an already-parsed document; each deadline carries its source page."""
    params = {"project_id": settings.llama_project_id} if settings.llama_project_id else None
    job = await gw.post(
        "api/v2/extract",
        params=params,
        json={
            "file_input": parse_job_id,
            "configuration": {
                "tier": "cost_effective",
                "version": "latest",
                "extraction_target": "per_doc",
                "data_schema": DEADLINE_SCHEMA,
                "cite_sources": True,
                "system_prompt": "Extract obligations for the individual consumer or tenant, not the company.",
            },
        },
    )
    job_id = Job.model_validate(job).id
    poll_params = dict(params or {}) | {"expand": "extract_metadata"}
    done = ExtractJob.model_validate(await _poll(gw, f"api/v2/extract/{job_id}", params=poll_params))
    result = DocumentDeadlines.model_validate(done.extract_result or {})
    meta = ((done.extract_metadata or {}).get("field_metadata") or {}).get("document_metadata") or {}
    for i, item in enumerate(result.deadlines):
        # Per-field metadata is keyed by path, e.g. "deadlines[0].date"; take the first cited page.
        for key in (f"deadlines[{i}].date", f"deadlines[{i}].rule", f"deadlines[{i}].title"):
            if key in meta:
                cites = FieldMeta.model_validate(meta[key]).citation
                if cites and cites[0].page:
                    item.page = cites[0].page
                    break
    return result
