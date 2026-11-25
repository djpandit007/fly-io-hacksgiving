"""Fetch a terms page, reduce it to stable sectioned text, and diff it against the last snapshot.

The diff works on sections (a heading plus its body) rather than lines, so that each change handed
to the judge is a coherent clause with its context, and so that reflowed paragraphs or a new
"Last updated" date do not register as changes.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from typing import Literal

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict

USER_AGENT = "FinePrint/0.1 (personal terms watcher)"
HEADING_TAGS = ("h1", "h2", "h3", "h4")
MAX_HEADING_CHARS = 120
DROP_TAGS = ("script", "style", "noscript", "nav", "footer", "header", "form", "svg", "iframe")

# Lines that change on every revision without changing what you agreed to.
_NOISE = re.compile(
    r"^(last\s+(updated|modified|revised)|effective(\s+date)?|updated\s+on|version)\b[^.]{0,60}$",
    re.IGNORECASE,
)


class Section(BaseModel):
    model_config = ConfigDict(frozen=True)

    heading: str
    body: str

    @property
    def text(self) -> str:
        return f"## {self.heading}\n{self.body}" if self.heading else self.body


class SectionChange(BaseModel):
    model_config = ConfigDict(frozen=True)

    heading: str
    kind: Literal["added", "removed", "modified"]
    before: str
    after: str


async def fetch(url: str, client: httpx.AsyncClient | None = None) -> tuple[str, str]:
    """Return (content_type, body) for a URL."""
    own = client is None
    client = client or httpx.AsyncClient(follow_redirects=True, timeout=30)
    try:
        resp = await client.get(url, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        return resp.headers.get("content-type", ""), resp.text
    finally:
        if own:
            await client.aclose()


def _clean(text: str) -> str:
    return " ".join(text.split())


def _bold_lead(el) -> str | None:
    """Text of a <strong>/<b> that opens a paragraph, e.g. <p><b>4. Fees.</b> You agree…</p>."""
    for child in el.children:
        if isinstance(child, str):
            if child.strip():
                return None
            continue
        if child.name in ("strong", "b"):
            lead = _clean(child.get_text(" ", strip=True))
            return lead if 0 < len(lead) <= MAX_HEADING_CHARS else None
        return None
    return None


def html_to_markdown(html: str) -> str:
    """Keep headings and paragraph breaks; drop page chrome.

    Many legal pages mark headings only with bold text, so a short bold run that opens a paragraph
    is treated as a heading too.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(DROP_TAGS):
        tag.decompose()
    root = soup.find("main") or soup.find("article") or soup.body or soup
    lines: list[str] = []
    for el in root.find_all([*HEADING_TAGS, "p", "li", "td", "dd", "dt", "blockquote", "pre"]):
        # Skip containers whose text is already captured through a nested block element.
        if el.name in ("li", "td", "dd", "blockquote") and el.find(["p", "li", *HEADING_TAGS]):
            continue
        text = _clean(el.get_text(" ", strip=True))
        if not text:
            continue
        if el.name in HEADING_TAGS:
            lines.append(f"## {text}")
            continue
        if el.name in ("p", "dt") and (lead := _bold_lead(el)):
            lines.append(f"## {lead.rstrip('.:')}")
            text = text[len(lead):].strip() if text.startswith(lead) else ""
            if not text:
                continue
        lines.append(f"- {text}" if el.name == "li" else text)
    return "\n".join(lines)


_MD_ANCHOR = re.compile(r"\[\]\([^)]*\)")              # [](#anchor)
_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")       # [text](url) and images
_MD_EMPH = re.compile(r"(\*\*|__|\*|_)(?=\S)(.+?)(?<=\S)\1")
_MD_ESC = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>])")
_MD_LIST = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_SETEXT = re.compile(r"^\s*(=+|-{3,})\s*$")


def _md_inline(text: str) -> str:
    text = _MD_ANCHOR.sub("", text)
    text = _MD_LINK.sub(r"\1", text)
    for _ in range(2):  # nested emphasis
        text = _MD_EMPH.sub(r"\2", text)
    return _clean(_MD_ESC.sub(r"\1", text))


def markdown_to_canonical(md: str) -> str:
    """Markdown from Open Terms Archive or LlamaParse, in the same shape html_to_markdown emits."""
    raw = md.splitlines()
    lines: list[str] = []
    for i, line in enumerate(raw):
        if _SETEXT.match(line):
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("|---") or set(stripped) <= {"|", "-", ":", " "}:
            continue
        nxt = raw[i + 1] if i + 1 < len(raw) else ""
        if stripped.startswith("#") or (_SETEXT.match(nxt) and len(stripped) <= MAX_HEADING_CHARS):
            lines.append("## " + _md_inline(stripped.lstrip("#")))
            continue
        no_anchor = _MD_ANCHOR.sub("", stripped)
        if re.fullmatch(r"(\*\*|__)(.+)\1[.:]?", no_anchor) and len(no_anchor) <= MAX_HEADING_CHARS + 4:
            lines.append("## " + _md_inline(no_anchor).rstrip(".:"))
            continue
        if _MD_LIST.match(stripped):
            lines.append("- " + _md_inline(_MD_LIST.sub("", stripped)))
            continue
        if stripped.startswith(">"):
            stripped = stripped.lstrip("> ")
        lines.append(_md_inline(stripped.replace("|", " ")))
    return "\n".join(line for line in lines if line.strip("#- "))


def normalise(markdown: str) -> str:
    """Canonical text used for hashing and diffing."""
    out: list[str] = []
    for raw in markdown.splitlines():
        line = " ".join(raw.replace(" ", " ").split())
        line = line.replace("’", "'").replace("“", '"').replace("”", '"')
        if not line or _NOISE.match(line.lstrip("#- ").strip()):
            continue
        if line.startswith("#"):
            line = "## " + line.lstrip("#").strip()
        out.append(line)
    return "\n".join(out)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def sections(text: str) -> list[Section]:
    result: list[Section] = []
    heading, body = "", []
    for line in text.splitlines():
        if line.startswith("## "):
            if heading or body:
                result.append(Section(heading=heading, body="\n".join(body)))
            heading, body = line[3:].strip(), []
        else:
            body.append(line)
    if heading or body:
        result.append(Section(heading=heading, body="\n".join(body)))
    return result


def _key(heading: str) -> str:
    # "4. Arbitration" and "5. Arbitration" are the same section after renumbering.
    return re.sub(r"^[\dIVXivx]+[.)]\s*|\W+", " ", heading).strip().lower()


def _keyed(secs: list[Section]) -> list[tuple[str, Section]]:
    """Unique keys: a heading that repeats ("User Content" twice) is keyed by its occurrence."""
    seen: dict[str, int] = {}
    out = []
    for sec in secs:
        k = _key(sec.heading)
        seen[k] = seen.get(k, 0) + 1
        out.append((k if seen[k] == 1 else f"{k}#{seen[k]}", sec))
    return out


def _words(text: str) -> list[str]:
    return text.split()


def _tokens(text: str) -> list[str]:
    """Comparison tokens: lower-case words only, so quotes, bullets and markup never count."""
    return re.findall(r"\w+", text.lower())


def diff(old: str, new: str, min_changed_words: int = 3) -> list[SectionChange]:
    """Section-level changes from old to new, ignoring tiny edits (typos, punctuation)."""
    old_secs, new_secs = sections(old), sections(new)
    old_keyed, new_keyed = _keyed(old_secs), _keyed(new_secs)
    old_by_key = dict(old_keyed)
    new_keys = {k for k, _ in new_keyed}
    changes: list[SectionChange] = []

    for key, sec in new_keyed:
        prev = old_by_key.get(key)
        if prev is None:
            changes.append(SectionChange(heading=sec.heading, kind="added", before="", after=sec.body))
            continue
        if prev.body == sec.body:
            continue
        matcher = difflib.SequenceMatcher(a=_tokens(prev.body), b=_tokens(sec.body), autojunk=False)
        changed = sum(
            max(i2 - i1, j2 - j1) for op, i1, i2, j1, j2 in matcher.get_opcodes() if op != "equal"
        )
        if changed >= min_changed_words:
            changes.append(SectionChange(heading=sec.heading, kind="modified", before=prev.body, after=sec.body))

    for key, sec in old_keyed:
        if key not in new_keys:
            changes.append(SectionChange(heading=sec.heading, kind="removed", before=sec.body, after=""))
    return _pair_renamed(changes, min_changed_words)


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(a=_tokens(a), b=_tokens(b), autojunk=False).ratio()


def _pair_renamed(changes: list[SectionChange], min_changed_words: int) -> list[SectionChange]:
    """An 'added' and a 'removed' section with near-identical bodies are one section renamed."""
    added = [c for c in changes if c.kind == "added"]
    removed = [c for c in changes if c.kind == "removed"]
    drop: set[int] = set()
    extra: list[SectionChange] = []
    for a in added:
        best = max(
            (r for r in removed if id(r) not in drop),
            key=lambda r: _similarity(r.before, a.after),
            default=None,
        )
        if best is None or _similarity(best.before, a.after) < 0.8:
            continue
        drop |= {id(a), id(best)}
        matcher = difflib.SequenceMatcher(a=_tokens(best.before), b=_tokens(a.after), autojunk=False)
        changed = sum(max(i2 - i1, j2 - j1) for op, i1, i2, j1, j2 in matcher.get_opcodes() if op != "equal")
        if changed >= min_changed_words:
            extra.append(SectionChange(heading=a.heading, kind="modified", before=best.before, after=a.after))
    return [c for c in changes if id(c) not in drop] + extra


def redline(before: str, after: str) -> str:
    """Compact word-level redline for messages: [-removed-] {+added+}."""
    a, b = _words(before), _words(after)
    out: list[str] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if op == "equal":
            seg = a[i1:i2]
            out.append(" ".join(seg) if len(seg) <= 12 else " ".join(seg[:5] + ["…"] + seg[-5:]))
        if op in ("delete", "replace"):
            out.append("[-" + " ".join(a[i1:i2]) + "-]")
        if op in ("insert", "replace"):
            out.append("{+" + " ".join(b[j1:j2]) + "+}")
    return " ".join(out)


_DAYS = re.compile(r"\bwithin\s+(\w+)\s*(?:\(\d+\)\s*)?(calendar\s+|business\s+)?days?\b", re.I)
_WORDS = {
    "seven": 7, "ten": 10, "fourteen": 14, "fifteen": 15, "twenty": 20, "thirty": 30,
    "forty-five": 45, "sixty": 60, "ninety": 90,
}


def opt_out_days(text: str) -> int | None:
    """Find an 'within N days' window in clause text; code finds it, the judge decides if it matters."""
    for m in _DAYS.finditer(text):
        raw = m.group(1).lower()
        if raw.isdigit():
            return int(raw)
        if raw in _WORDS:
            return _WORDS[raw]
    return None
