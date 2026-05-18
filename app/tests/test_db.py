import sqlite3
from chores.db import (
    connect, init_schema, migrate, seed_defaults,
    current_period, ensure_open_period, archive_period,
)


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
        "comments", "reactions", "announcements", "settings", "periods",
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
    sups = [r["name"] for r in conn.execute(
        "SELECT name FROM supervisors ORDER BY id").fetchall()]
    assert sups == ["惠姐", "帝哥"]


def test_migrate_adds_period_id_to_legacy_checkins(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    # 模拟旧表（无 period_id）
    conn.execute(
        "CREATE TABLE checkins (id INTEGER PRIMARY KEY, kind TEXT NOT NULL,"
        " status TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO checkins (kind, status, created_at) "
        "VALUES ('fixed','scored','2026-05-17T08:00:00')"
    )
    conn.commit()
    migrate(conn)
    cols = {r["name"] for r in conn.execute(
        "PRAGMA table_info(checkins)").fetchall()}
    assert "period_id" in cols


def test_ensure_open_period_creates_one_and_backfills(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    init_schema(conn)
    conn.execute(
        "INSERT INTO checkins (kind, status, created_at) "
        "VALUES ('fixed','scored','2026-05-17T08:00:00')"
    )
    conn.commit()
    pid = ensure_open_period(conn, "2026-05-17T09:00:00")
    assert current_period(conn)["id"] == pid
    # 旧无归属 checkin 被收进当前周期
    row = conn.execute("SELECT period_id FROM checkins").fetchone()
    assert row["period_id"] == pid
    # 幂等：再次调用不新建
    assert ensure_open_period(conn, "2026-05-17T10:00:00") == pid


def test_archive_period_closes_current_and_opens_next(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    init_schema(conn)
    p1 = ensure_open_period(conn, "2026-05-17T09:00:00")
    p2 = archive_period(conn, "2026-05-18T09:00:00")
    assert p2 != p1
    old = conn.execute("SELECT * FROM periods WHERE id=?", (p1,)).fetchone()
    new = conn.execute("SELECT * FROM periods WHERE id=?", (p2,)).fetchone()
    assert old["ended_at"] == "2026-05-18T09:00:00"
    assert new["ended_at"] is None
    assert new["seq"] == old["seq"] + 1
    assert current_period(conn)["id"] == p2
