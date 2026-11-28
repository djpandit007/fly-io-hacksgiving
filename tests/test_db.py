import sqlite3

import pytest

from app import db

V1_LEDGER_ROW = ("Opt out: Acme", "x", "2030-01-31", "opt_out", "2026-11-01T00:00:00+00:00")


def make_v1(path):
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA)
    conn.execute("INSERT INTO services (name, terms_url, domain, added_at) VALUES ('Acme', 'https://acme.test/terms', 'acme.test', '2026-11-01T00:00:00+00:00')")
    conn.execute("INSERT INTO ledger (title, detail, due_on, kind, created_at) VALUES (?, ?, ?, ?, ?)", V1_LEDGER_ROW)
    conn.execute("INSERT INTO kv (key, value) VALUES ('owner_chat', '42')")
    conn.commit()
    conn.close()


def test_v1_database_is_upgraded_with_backup_and_data_kept(tmp_path):
    path = tmp_path / "fineprint.db"
    make_v1(path)
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert db.backup_path(path, 1).exists()
    [entry] = db.rows(db.LedgerEntry, conn.execute("SELECT * FROM ledger"))
    assert entry.user_id is None and entry.title == "Opt out: Acme"
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(changes)")}
    assert {"favours", "partial"} <= cols
    for table in ("users", "subscriptions", "mail_seen"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert db.get_kv(conn, "owner_chat") == "42"  # kept for adoption by the owner
    conn.close()


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "fineprint.db"
    make_v1(path)
    db.connect(path).close()
    db.backup_path(path, 1).unlink()
    conn = db.connect(path)  # second start: nothing to migrate, no new backup
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert not db.backup_path(path, 1).exists()
    assert conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 1


def test_fresh_database_gets_current_schema_without_backup(tmp_path):
    path = tmp_path / "new.db"
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert not db.backup_path(path, 1).exists()


def test_per_user_ledger_uniqueness(tmp_path):
    conn = db.connect(tmp_path / "u.db")
    for uid, tok in ((1, "a"), (2, "b")):
        conn.execute("INSERT INTO users (id, chat_id, calendar_token, joined_at) VALUES (?, ?, ?, ?)", (uid, uid * 10, tok, db.now()))
    row = ("T", "2030-01-01", "other", db.now())
    conn.execute("INSERT INTO ledger (user_id, title, due_on, kind, created_at) VALUES (1, ?, ?, ?, ?)", row)
    conn.execute("INSERT INTO ledger (user_id, title, due_on, kind, created_at) VALUES (2, ?, ?, ?, ?)", row)
    assert conn.execute("INSERT OR IGNORE INTO ledger (user_id, title, due_on, kind, created_at) VALUES (1, ?, ?, ?, ?)", row).rowcount == 0


def test_only_one_owner(tmp_path):
    conn = db.connect(tmp_path / "o.db")
    conn.execute("INSERT INTO users (chat_id, role, calendar_token, joined_at) VALUES (1, 'owner', 'a', ?)", (db.now(),))
    try:
        conn.execute("INSERT INTO users (chat_id, role, calendar_token, joined_at) VALUES (2, 'owner', 'b', ?)", (db.now(),))
        raise AssertionError("second owner accepted")
    except sqlite3.IntegrityError:
        pass


def test_v3_database_gains_metrics_tables_and_keeps_its_data(tmp_path):
    path = tmp_path / "v3.db"
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(db.SCHEMA)
    for version in (2, 3):
        for stmt in (s.strip() for s in db.MIGRATIONS[version].split(";\n")):
            if stmt:
                conn.execute(stmt)
    conn.execute("PRAGMA user_version = 3")
    conn.execute("INSERT INTO services (name, terms_url, added_at) VALUES ('Acme', 'https://acme.test/terms', ?)", (db.now(),))
    conn.execute("INSERT INTO users (chat_id, calendar_token, joined_at) VALUES (7, 'tok', ?)", (db.now(),))
    conn.close()

    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"metric_runs", "metric_failures", "metric_counts", "metric_usage"} <= tables
    assert conn.execute("SELECT name FROM services").fetchone()[0] == "Acme"
    assert conn.execute("SELECT chat_id FROM users").fetchone()[0] == 7
    assert db.backup_path(path, 3).exists()


def test_v4_database_gains_answers_table_and_keeps_its_data(tmp_path):
    path = tmp_path / "v4.db"
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(db.SCHEMA)
    for version in (2, 3, 4):
        for stmt in (s.strip() for s in db.MIGRATIONS[version].split(";\n")):
            if stmt:
                conn.execute(stmt)
    conn.execute("PRAGMA user_version = 4")
    conn.execute("INSERT INTO services (name, terms_url, added_at) VALUES ('Acme', 'https://acme.test/terms', ?)", (db.now(),))
    conn.execute("INSERT INTO metric_counts (day, kind, name, outcome, n) VALUES ('2026-11-26', 'command', '/list', 'ok', 3)")
    conn.close()

    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(answers)")}
    assert columns == {"token", "kind", "model", "prompt_version", "change_id", "quotes_ok", "sent_at", "vote",
                       "reason", "voted_at"}
    assert {r["name"] for r in conn.execute("PRAGMA table_info(unwatched)")} == {"user_id", "service_id", "unwatched_at"}
    assert conn.execute("SELECT name FROM services").fetchone()[0] == "Acme"
    assert conn.execute("SELECT n FROM metric_counts").fetchone()[0] == 3


def make_v5(path):
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(db.SCHEMA)
    for version in (2, 3, 4, 5):
        for stmt in (s.strip() for s in db.MIGRATIONS[version].split(";\n")):
            if stmt:
                conn.execute(stmt)
    conn.execute("PRAGMA user_version = 5")
    conn.execute("INSERT INTO services (name, terms_url, added_at) VALUES ('Acme', 'https://acme.test/terms', '2026-11-01T00:00:00+00:00')")
    conn.execute("INSERT INTO services (name, terms_url, added_at) VALUES ('Beta', 'https://beta.test/tos', '2026-11-02T00:00:00+00:00')")
    conn.execute("INSERT INTO snapshots (service_id, taken_at, origin, sha, text) VALUES (1, ?, 'live', 'abc', 'text')", (db.now(),))
    conn.execute("INSERT INTO users (chat_id, calendar_token, joined_at) VALUES (7, 'tok', ?)", (db.now(),))
    conn.close()


def test_v5_database_gains_policy_bundles_and_keeps_its_data(tmp_path):
    path = tmp_path / "fineprint.db"
    make_v5(path)
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert db.backup_path(path, 5).exists()
    assert db.backup_path(path, 5).name == "fineprint.db.v5.bak"
    docs = db.rows(db.ServiceDocument, conn.execute("SELECT * FROM service_documents ORDER BY service_id"))
    assert [(d.service_id, d.role, d.url) for d in docs] == [
        (1, "terms", "https://acme.test/terms"),
        (2, "terms", "https://beta.test/tos"),
    ]
    assert docs[0].label == "Terms"
    [snap] = db.rows(db.Snapshot, conn.execute("SELECT * FROM snapshots"))
    assert snap.document_id == docs[0].id and snap.evidence_path is None  # v7: given its terms document
    assert "document_id" in {r["name"] for r in conn.execute("PRAGMA table_info(changes)")}
    [user] = db.rows(db.User, conn.execute("SELECT * FROM users"))
    assert user.jurisdiction is None and user.chat_id == 7
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"document_contradictions", "approval_tokens", "action_receipts", "swarm_events"} <= tables
    assert "dashboard_sessions" not in tables
    conn.close()


def test_v7_snapshots_are_unique_per_document_and_keep_references(tmp_path):
    path = tmp_path / "fineprint.db"
    make_v5(path)
    conn = db.connect(path)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1  # back on after the rebuild
    assert conn.execute("PRAGMA foreign_key_check").fetchone() is None
    snap = conn.execute("SELECT * FROM snapshots").fetchone()
    terms = snap["document_id"]
    conn.execute("INSERT INTO service_documents (service_id, role, url, added_at) VALUES (?, 'privacy', 'https://acme.test/p', ?)",
                 (snap["service_id"], db.now()))
    privacy = conn.execute("SELECT id FROM service_documents WHERE role = 'privacy'").fetchone()[0]
    insert = "INSERT OR IGNORE INTO snapshots (service_id, taken_at, origin, sha, text, document_id) VALUES (?, ?, 'live', ?, 't', ?)"
    # The same text in another document, and the same document returning to it later, are both kept.
    assert conn.execute(insert, (snap["service_id"], snap["taken_at"], snap["sha"], privacy)).rowcount == 1
    assert conn.execute(insert, (snap["service_id"], "2099-01-01T00:00:00+00:00", snap["sha"], terms)).rowcount == 1
    # Only the very same version at the same time is a duplicate.
    assert conn.execute(insert, (snap["service_id"], snap["taken_at"], snap["sha"], terms)).rowcount == 0
    conn.close()


def test_v6_service_documents_are_unique_per_role_and_url(tmp_path):
    conn = db.connect(tmp_path / "b.db")
    conn.execute("INSERT INTO services (name, terms_url, added_at) VALUES ('Acme', 'https://acme.test/terms', ?)", (db.now(),))
    conn.execute("INSERT INTO service_documents (service_id, role, url, added_at) VALUES (1, 'terms', 'https://acme.test/terms', ?)", (db.now(),))
    for role, url in (("terms", "https://acme.test/other"), ("privacy", "https://acme.test/terms")):
        try:
            conn.execute("INSERT INTO service_documents (service_id, role, url, added_at) VALUES (1, ?, ?, ?)", (role, url, db.now()))
            raise AssertionError("duplicate accepted")
        except sqlite3.IntegrityError:
            pass


CENSUS_INSERT = "INSERT INTO census (domain, role, flag, name, url, p, sha, graded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"


def test_fresh_database_has_provenance_and_census(tmp_path):
    conn = db.connect(tmp_path / "v8.db")
    assert db.SCHEMA_VERSION == 8
    assert "provenance" in {r["name"] for r in conn.execute("PRAGMA table_info(snapshots)")}
    conn.execute(CENSUS_INSERT, ("spotify.com", "terms", "arbitration", "Spotify", "https://www.spotify.com/legal/", 0.93, "a" * 64, db.now()))
    [row] = db.rows(db.CensusRow, conn.execute("SELECT * FROM census"))
    assert (row.clause, row.severity, row.has_opt_out) == (None, None, False)
    with pytest.raises(sqlite3.IntegrityError):  # one row per (domain, role, flag)
        conn.execute(CENSUS_INSERT, ("spotify.com", "terms", "arbitration", "S", "u", 0.1, "b" * 64, db.now()))


def test_v7_database_upgrades_with_backup_and_keeps_snapshots(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    monkeypatch.setattr(db, "SCHEMA_VERSION", 7)
    conn = db.connect(path)
    conn.execute("INSERT INTO services (name, terms_url, domain, added_at) VALUES ('Acme', 'https://acme.test/terms', 'acme.test', ?)", (db.now(),))
    conn.execute("INSERT INTO snapshots (service_id, taken_at, origin, sha, text) VALUES (1, ?, 'live', 'abc', 'hello')", (db.now(),))
    conn.close()
    monkeypatch.undo()
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
    assert db.backup_path(path, 7).exists()
    [snap] = db.rows(db.Snapshot, conn.execute("SELECT * FROM snapshots"))
    assert snap.text == "hello" and snap.provenance is None
