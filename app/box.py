"""Box as the inbox and archive.

- Inbox: files you drop in the configured folder (a lease, an offer letter, a policy) are parsed
  once and turned into ledger deadlines.
- Archive: every change report is written back to the same folder as a Markdown file.
"""

from __future__ import annotations

import json
from typing import Literal

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


async def upload_report(upload_gw: Gateway, name: str, markdown: str) -> str:
    attributes = json.dumps({"name": name, "parent": {"id": settings.box_folder_id}})
    data = await upload_gw.post(
        "api/2.0/files/content",
        files={"attributes": (None, attributes), "file": (name, markdown.encode(), "text/markdown")},
    )
    return BoxItems.model_validate(data).entries[0].id
