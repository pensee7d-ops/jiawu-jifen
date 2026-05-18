import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS supervisors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS task_catalog (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    default_points INTEGER NOT NULL,
    is_vocab INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS periods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seq INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT
);
CREATE TABLE IF NOT EXISTS checkins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    task_id INTEGER,
    period_id INTEGER,
    title TEXT,
    photo_path TEXT,
    note TEXT,
    mood TEXT,
    proposed_points INTEGER,
    awarded_points INTEGER,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reviewed_by TEXT,
    reviewed_at TEXT
);
CREATE TABLE IF NOT EXISTS computer_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_at TEXT NOT NULL,
    end_at TEXT
);
CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checkin_id INTEGER NOT NULL,
    author_name TEXT NOT NULL,
    author_role TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checkin_id INTEGER NOT NULL,
    author_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS announcements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULT_TASKS = [
    ("收拾宠物尿垫", 2),
    ("添粮加水", 2),
    ("全屋吸尘", 10),
    ("全屋拖地", 10),
    ("洗晒衣服", 5),
    ("收衣服", 5),
    ("刷马桶", 5),
]

DEFAULT_SUPERVISORS = ("惠姐", "帝哥")


def connect(db_path: str) -> sqlite3.Connection:
    parent = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def migrate(conn: sqlite3.Connection) -> None:
    """轻量迁移：给已存在的旧 checkins 表补 period_id 列。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(checkins)").fetchall()}
    if "period_id" not in cols:
        conn.execute("ALTER TABLE checkins ADD COLUMN period_id INTEGER")
    conn.commit()


def seed_defaults(conn: sqlite3.Connection) -> None:
    have = conn.execute("SELECT COUNT(*) AS c FROM task_catalog").fetchone()["c"]
    if have == 0:
        for i, (name, pts) in enumerate(DEFAULT_TASKS):
            conn.execute(
                "INSERT INTO task_catalog (name, default_points, sort_order)"
                " VALUES (?, ?, ?)",
                (name, pts, i),
            )
    if conn.execute("SELECT COUNT(*) AS c FROM supervisors").fetchone()["c"] == 0:
        for nm in DEFAULT_SUPERVISORS:
            conn.execute("INSERT INTO supervisors (name) VALUES (?)", (nm,))
    conn.commit()


def current_period(conn: sqlite3.Connection):
    """当前开放周期（ended_at 为空），无则返回 None。"""
    return conn.execute(
        "SELECT * FROM periods WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()


def ensure_open_period(conn: sqlite3.Connection, now_iso: str) -> int:
    """保证存在一个开放周期；新建时把未归属的旧 checkins 收进当前周期。返回其 id。"""
    cur = current_period(conn)
    if cur is None:
        seq = (conn.execute("SELECT COALESCE(MAX(seq),0) AS s FROM periods")
               .fetchone()["s"]) + 1
        cid = conn.execute(
            "INSERT INTO periods (seq, started_at) VALUES (?, ?)",
            (seq, now_iso),
        ).lastrowid
    else:
        cid = cur["id"]
    conn.execute(
        "UPDATE checkins SET period_id=? WHERE period_id IS NULL", (cid,)
    )
    conn.commit()
    return cid


def archive_period(conn: sqlite3.Connection, now_iso: str) -> int:
    """结算封存当前周期，开启新周期。返回新周期 id。"""
    cur = ensure_open_period(conn, now_iso)
    conn.execute(
        "UPDATE periods SET ended_at=? WHERE id=?", (now_iso, cur)
    )
    seq = (conn.execute("SELECT COALESCE(MAX(seq),0) AS s FROM periods")
           .fetchone()["s"]) + 1
    nid = conn.execute(
        "INSERT INTO periods (seq, started_at) VALUES (?, ?)", (seq, now_iso)
    ).lastrowid
    conn.commit()
    return nid
