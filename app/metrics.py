"""Owner metrics, kept in the app's own SQLite database.

Three shapes: one row per daily run and per failed background job; daily counters (with total and
slowest milliseconds) for provider calls, bot commands and jobs; and daily totals of what each
provider *reported* charging or consuming. Cost is never computed from a stored price.

Only counts, durations, amounts, error types, provider and command names, and the names of services
that failed a run are stored: never URLs, file names, message text, addresses or member ids.

Recording must never change the outcome of the work it measures, so every write swallows and logs
its own errors.
"""

from __future__ import annotations

import functools
import json
import logging
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from app import db
from app.config import settings
from app.telegram import esc

log = logging.getLogger(__name__)

RETENTION_DAYS = 90
REPORT_DAYS = 7
UNIT_LABELS = {
    "credits": "credits",
    "read_units": "read units",
    "embed_tokens": "embedding tokens",
    "input_tokens": "input tokens",
    "output_tokens": "output tokens",
}


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def elapsed_ms(started: float) -> int:
    """Milliseconds since `started` (a ``time.monotonic()`` reading)."""
    return int((time.monotonic() - started) * 1000)


def _safe[**P](fn: Callable[P, None]) -> Callable[P, None]:
    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> None:
        try:
            fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — a metric must never fail the work it measures
            log.warning("metric %s not recorded: %s", fn.__name__, type(exc).__name__)

    return wrapper


@_safe
def count(conn: sqlite3.Connection, kind: str, name: str, outcome: str = "ok", ms: int = 0) -> None:
    conn.execute(
        "INSERT INTO metric_counts (day, kind, name, outcome, n, total_ms, max_ms) VALUES (?, ?, ?, ?, 1, ?, ?) "
        "ON CONFLICT(day, kind, name, outcome) DO UPDATE SET "
        "n = n + 1, total_ms = total_ms + excluded.total_ms, max_ms = MAX(max_ms, excluded.max_ms)",
        (_today(), kind, name, outcome, ms, ms),
    )


@_safe
def usage(conn: sqlite3.Connection, provider: str, unit: str, amount: float) -> None:
    conn.execute(
        "INSERT INTO metric_usage (day, provider, unit, amount) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(day, provider, unit) DO UPDATE SET amount = amount + excluded.amount",
        (_today(), provider, unit, float(amount)),
    )


@_safe
def run(
    conn: sqlite3.Connection, *, duration_ms: int, checked: int, failed: list[str],
    changes: int, nudges: int, sent: int, undelivered: int,
) -> None:
    conn.execute(
        "INSERT INTO metric_runs (ran_at, duration_ms, checked, failed, changes, nudges, sent, undelivered) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (db.now(), duration_ms, checked, json.dumps(failed), changes, nudges, sent, undelivered),
    )


@_safe
def failure(conn: sqlite3.Connection, kind: str, exc: BaseException) -> None:
    """A background job that ended in an error: its kind and the error's type, nothing else."""
    conn.execute(
        "INSERT INTO metric_failures (at, kind, error) VALUES (?, ?, ?)", (db.now(), kind, type(exc).__name__)
    )


@_safe
def prune(conn: sqlite3.Connection, days: int = RETENTION_DAYS) -> None:
    cutoff = datetime.now(UTC) - timedelta(days=days)
    day, stamp = cutoff.date().isoformat(), cutoff.isoformat(timespec="seconds")
    conn.execute("DELETE FROM metric_counts WHERE day < ?", (day,))
    conn.execute("DELETE FROM metric_usage WHERE day < ?", (day,))
    conn.execute("DELETE FROM metric_runs WHERE ran_at < ?", (stamp,))
    conn.execute("DELETE FROM metric_failures WHERE at < ?", (stamp,))


class Recorder:
    """The same functions bound to a connection, for code that has none (gateways, Telegram)."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def count(self, kind: str, name: str, outcome: str = "ok", ms: int = 0) -> None:
        count(self.conn, kind, name, outcome, ms)

    def usage(self, provider: str, unit: str, amount: float) -> None:
        usage(self.conn, provider, unit, amount)


def dig(data: Any, *keys: str) -> float | None:
    """A number at a nested key of a provider response, or None when it is absent or not a number."""
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    if isinstance(data, bool) or not isinstance(data, int | float):
        return None
    return float(data)


# --- /stats -----------------------------------------------------------------------------------------

def _ms(ms: float) -> str:
    return f"{ms / 1000:.1f} s" if ms >= 1000 else f"{ms:.0f} ms"


def _num(amount: float) -> str:
    return f"{amount:,.0f}" if float(amount).is_integer() else f"{amount:,.2f}"


def _usd(amount: float) -> str:
    return f"${amount:.2f}" if amount >= 1 else f"${amount:.4f}"


def report(conn: sqlite3.Connection, days: int = REPORT_DAYS) -> str:
    """The owner's summary of the past `days` days, as Telegram HTML. Totals only: no member is named."""
    since = datetime.now(UTC) - timedelta(days=days)
    day, stamp = since.date().isoformat(), since.isoformat(timespec="seconds")
    out = [f"<b>Fine Print — last {days} days</b>"]

    runs = conn.execute("SELECT * FROM metric_runs WHERE ran_at >= ? ORDER BY ran_at DESC", (stamp,)).fetchall()
    lines = []
    for r in runs:
        failed = json.loads(r["failed"])
        failed_text = f", <b>{len(failed)} failed</b> ({esc(', '.join(failed))})" if failed else ""
        undelivered = f", <b>{r['undelivered']} undelivered</b>" if r["undelivered"] else ""
        lines.append(
            f"• {datetime.fromisoformat(r['ran_at']):%d %b %H:%M} — {r['checked']} checked{failed_text}, "
            f"{r['changes']} changes, {r['nudges']} nudges, {r['sent']} sent{undelivered}, {_ms(r['duration_ms'])}"
        )
    out.append(f"\n<b>Daily runs</b> ({len(runs)})\n" + ("\n".join(lines) or "None recorded."))

    jobs = conn.execute(
        "SELECT name, SUM(n) AS n, SUM(total_ms) AS total, MAX(max_ms) AS slowest FROM metric_counts "
        "WHERE kind = 'job' AND day >= ? GROUP BY name ORDER BY name", (day,),
    ).fetchall()
    fails = conn.execute(
        "SELECT kind, error, COUNT(*) AS n FROM metric_failures WHERE at >= ? GROUP BY kind, error ORDER BY n DESC",
        (stamp,),
    ).fetchall()
    lines = [f"• {esc(j['name'])} — {j['n']} jobs, avg {_ms(j['total'] / j['n'])}, slowest {_ms(j['slowest'])}" for j in jobs]
    lines.append(
        "Failures: " + ", ".join(f"{esc(f['kind'])} {esc(f['error'])} ×{f['n']}" for f in fails) if fails else "No failures."
    )
    out.append("\n<b>Background jobs</b>\n" + "\n".join(lines))

    providers = conn.execute(
        "SELECT name, SUM(n) AS n, SUM(total_ms) AS total, MAX(max_ms) AS slowest, "
        "SUM(CASE WHEN outcome = 'error' THEN n ELSE 0 END) AS failed, "
        "SUM(CASE WHEN outcome = 'retried' THEN n ELSE 0 END) AS retried "
        "FROM metric_counts WHERE kind = 'provider' AND day >= ? GROUP BY name ORDER BY name", (day,),
    ).fetchall()
    lines = [
        f"• {esc(p['name'])} — {p['n']} calls, {p['failed']} failed, {p['retried']} retried, "
        f"avg {_ms(p['total'] / p['n'])}, slowest {_ms(p['slowest'])}"
        for p in providers
    ]
    out.append("\n<b>Providers</b>\n" + ("\n".join(lines) or "No calls recorded."))

    used = conn.execute(
        "SELECT provider, unit, SUM(amount) AS amount FROM metric_usage WHERE day >= ? "
        "GROUP BY provider, unit ORDER BY provider, unit", (day,),
    ).fetchall()
    dollars = {u["provider"]: u["amount"] for u in used if u["unit"] == "usd"}
    other: dict[str, list[str]] = {}
    for u in used:
        if u["unit"] != "usd":
            other.setdefault(u["provider"], []).append(f"{_num(u['amount'])} {UNIT_LABELS.get(u['unit'], u['unit'])}")
    lines = []
    if dollars:
        parts = ", ".join(f"{esc(name)} {_usd(amount)}" for name, amount in dollars.items())
        lines.append(f"<b>{_usd(sum(dollars.values()))}</b> reported by providers ({parts})")
    else:
        lines.append("No dollar cost reported.")
    if other:
        lines.append(
            "Not included in the total: " + "; ".join(f"{esc(name)} {', '.join(units)}" for name, units in other.items())
        )
    silent = [p["name"] for p in providers if p["name"] not in dollars and p["name"] not in other]
    if silent:
        lines.append("Not reported by: " + esc(", ".join(silent)))
    lines.append("The cost of running the Sprite is not included.")
    out.append("\n<b>Cost</b>\n" + "\n".join(lines))

    def scalar(sql: str, *params: Any) -> int:
        return conn.execute(sql, params).fetchone()[0] or 0

    commands = conn.execute(
        "SELECT name, SUM(n) AS n, SUM(CASE WHEN outcome = 'error' THEN n ELSE 0 END) AS failed FROM metric_counts "
        "WHERE kind = 'command' AND day >= ? GROUP BY name ORDER BY n DESC, name", (day,),
    ).fetchall()
    command_text = ", ".join(
        f"{esc(c['name'])} {c['n']}" + (f" ({c['failed']} failed)" if c["failed"] else "") for c in commands
    )
    tapped = conn.execute(
        "SELECT name, SUM(n) AS n, SUM(CASE WHEN outcome = 'error' THEN n ELSE 0 END) AS failed FROM metric_counts "
        "WHERE kind = 'tap' AND day >= ? GROUP BY name ORDER BY n DESC, name", (day,),
    ).fetchall()
    tap_text = ", ".join(
        f"{esc(t['name'])} {t['n']}" + (f" ({t['failed']} failed)" if t["failed"] else "") for t in tapped
    )
    out.append(
        "\n<b>Usage</b>\n"
        f"Members: {scalar("SELECT COUNT(*) FROM users WHERE status = 'active'")} active, "
        f"{scalar('SELECT COUNT(*) FROM users WHERE joined_at >= ?', stamp)} joined\n"
        f"Services watched: {scalar('SELECT COUNT(DISTINCT service_id) FROM subscriptions')}\n"
        "Material changes found: "
        f"{scalar('SELECT COUNT(*) FROM changes WHERE detected_at >= ? AND (material >= ? OR partial = 1)', stamp, settings.material_threshold)}\n"
        f"Commands: {command_text or 'none'}\n"
        f"Buttons: {tap_text or 'none'}"
    )

    from app import (
        feedback,  # here, not at the top: feedback -> taps -> judge -> metrics would be a cycle
    )

    rated = feedback.summary(conn, days)
    out.append("\n<b>Answer feedback</b>\n" + ("\n".join(rated) if rated else "No feedback yet."))
    return "\n".join(out)
