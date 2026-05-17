import sqlite3
from chores.db import connect, init_schema, seed_defaults


def test_init_schema_creates_tables(tmp_path):
    db = str(tmp_path / "t.db")
    conn = connect(db)
    init_schema(conn)
    names = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {
        "supervisors", "task_catalog", "checkins", "computer_sessions",
        "comments", "reactions", "announcements", "settings",
    } <= names


def test_connect_returns_row_dicts(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    row = conn.execute("SELECT 1 AS a").fetchone()
    assert row["a"] == 1


def test_seed_defaults_is_idempotent(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    init_schema(conn)
    seed_defaults(conn)
    seed_defaults(conn)
    n = conn.execute("SELECT COUNT(*) AS c FROM task_catalog").fetchone()["c"]
    assert n == 7
