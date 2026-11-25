import sqlite3

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
