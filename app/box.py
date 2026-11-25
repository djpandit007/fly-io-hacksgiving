"""Box as the inbox and archive.

- Inbox: files you drop in the configured folder (a lease, an offer letter, a policy) are parsed
  once and turned into ledger deadlines.
- Archive: every change report is written back to the same folder as a Markdown file.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Literal

import httpx

from pydantic import BaseModel, Field

from app.config import settings
from app.gateway import Gateway


class BoxItem(BaseModel):
    id: str
    name: str
    type: Literal["file", "folder", "web_link"]
    modified_at: str | None = None


class BoxItems(BaseModel):
    entries: list[BoxItem] = Field(default_factory=list)

PARSEABLE = (".pdf", ".png", ".jpg", ".jpeg", ".docx", ".doc", ".html", ".htm", ".txt", ".heic", ".webp")


async def list_inbox(gw: Gateway) -> list[BoxItem]:
    data = await gw.get(
        f"2.0/folders/{settings.box_folder_id}/items", params={"fields": "id,name,type,modified_at", "limit": 200}
    )
    items = BoxItems.model_validate(data).entries
    return [e for e in items if e.type == "file" and e.name.lower().endswith(PARSEABLE)]


async def download(gw: Gateway, file_id: str) -> bytes:
    resp = await gw.request("GET", f"2.0/files/{file_id}/content", follow_redirects=True)
    return resp.content


async def upload(gw: Gateway, name: str, content: bytes, content_type: str = "application/octet-stream") -> str:
    """Upload into the Fine Print folder; on a name clash, retry once with a timestamp suffix."""
    for attempt in (name, _stamped(name)):
        # The Sprites Box connector serves uploads on the same gateway under /upload/...
        # Attributes must come before the file part.
        attributes = json.dumps({"name": attempt, "parent": {"id": settings.box_folder_id}})
        try:
            data = await gw.post(
                "upload/files/content",
                files={"attributes": (None, attributes), "file": (attempt, content, content_type)},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409 and attempt == name:
                continue
            raise
        return BoxItems.model_validate(data).entries[0].id
    raise AssertionError("unreachable")


def _stamped(name: str) -> str:
    stem, dot, ext = name.rpartition(".")
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{stem} ({stamp}).{ext}" if dot else f"{name} ({stamp})"


async def upload_report(gw: Gateway, name: str, markdown: str) -> str:
    return await upload(gw, name, markdown.encode(), "text/markdown")
