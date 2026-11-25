"""Read-only Gmail scan.

- Builds the watchlist: which companies have you signed up with or bought from?
- Catches "we've updated our terms" emails so that service is checked straight away.

Only sender domains and subjects are read (``format=metadata``); message bodies are never fetched
or stored. The OAuth token file holds ``client_id``, ``client_secret`` and ``refresh_token`` for
scope ``gmail.readonly`` and is written by ``scripts/gmail_auth.py`` with mode 0600.
"""

from __future__ import annotations

import re
from collections import Counter
from email.utils import parseaddr
from pathlib import Path

import httpx
from pydantic import BaseModel, SecretStr

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URL = "https://oauth2.googleapis.com/token"

SIGNUP_QUERY = (
    'newer_than:1y -in:chats ('
    'subject:(welcome OR "confirm your" OR "verify your" OR receipt OR order OR subscription OR trial '
    'OR invoice OR "your account") OR "terms of service" OR "privacy policy")'
)
UPDATE_QUERY = 'newer_than:{days}d subject:((update OR updated OR updating OR changes OR changing) (terms OR privacy OR policy OR agreement))'
UPDATE_SUBJECT = re.compile(r"(updat|chang).{0,40}(terms|privacy|policy|agreement)", re.I)

# Mail providers and senders that are not services you agreed terms with.
IGNORED = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com", "me.com",
    "proton.me", "protonmail.com", "google.com", "accounts.google.com",
}
TWO_PART_TLDS = {"co.uk", "com.au", "co.in", "co.jp", "com.br", "co.nz", "org.uk", "ac.uk"}


class GmailCredentials(BaseModel):
    """Contents of the 0600 token file written by scripts/gmail_auth.py."""

    client_id: str
    client_secret: SecretStr
    refresh_token: SecretStr


class SenderStat(BaseModel):
    domain: str
    count: int
    sample_subject: str


class TermsUpdateMail(BaseModel):
    domain: str
    subject: str


def registrable_domain(host: str) -> str:
    parts = host.lower().strip(".").split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in TWO_PART_TLDS:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def sender_domain(from_header: str) -> str | None:
    _, addr = parseaddr(from_header)
    if "@" not in addr:
        return None
    domain = registrable_domain(addr.rsplit("@", 1)[1])
    return None if domain in IGNORED else domain


class Gmail:
    def __init__(self, token_file: Path, client: httpx.AsyncClient | None = None):
        self.token_file = token_file
        self.client = client or httpx.AsyncClient(timeout=30)
        self._access: str | None = None

    @property
    def configured(self) -> bool:
        return self.token_file.exists()

    async def _token(self) -> str:
        if self._access:
            return self._access
        creds = GmailCredentials.model_validate_json(self.token_file.read_text())
        resp = await self.client.post(
            TOKEN_URL,
            data={
                "client_id": creds.client_id,
                "client_secret": creds.client_secret.get_secret_value(),
                "refresh_token": creds.refresh_token.get_secret_value(),
                "grant_type": "refresh_token",
            },
        )
        resp.raise_for_status()
        self._access = resp.json()["access_token"]
        return self._access

    async def _get(self, path: str, **params) -> dict:
        resp = await self.client.get(
            f"{GMAIL}/{path}", params=params, headers={"Authorization": f"Bearer {await self._token()}"}
        )
        resp.raise_for_status()
        return resp.json()

    async def _headers(self, query: str, limit: int) -> list[tuple[str, str]]:
        ids: list[str] = []
        page_token = None
        while len(ids) < limit:
            params = {"q": query, "maxResults": min(100, limit - len(ids))}
            if page_token:
                params["pageToken"] = page_token
            data = await self._get("messages", **params)
            ids += [m["id"] for m in data.get("messages", [])]
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        out = []
        for mid in ids:
            msg = await self._get(f"messages/{mid}", format="metadata", metadataHeaders=["From", "Subject"])
            hdrs = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
            out.append((hdrs.get("from", ""), hdrs.get("subject", "")))
        return out

    async def senders(self, limit: int = 300) -> list[SenderStat]:
        counts: Counter[str] = Counter()
        subjects: dict[str, str] = {}
        for from_, subject in await self._headers(SIGNUP_QUERY, limit):
            if domain := sender_domain(from_):
                counts[domain] += 1
                subjects.setdefault(domain, subject)
        return [SenderStat(domain=d, count=n, sample_subject=subjects[d]) for d, n in counts.most_common()]

    async def terms_updates(self, days: int = 2) -> list[TermsUpdateMail]:
        """Recent 'we updated our terms' emails."""
        found = []
        for from_, subject in await self._headers(UPDATE_QUERY.format(days=days), 50):
            if UPDATE_SUBJECT.search(subject) and (domain := sender_domain(from_)):
                found.append(TermsUpdateMail(domain=domain, subject=subject))
        return found

    async def aclose(self) -> None:
        await self.client.aclose()
