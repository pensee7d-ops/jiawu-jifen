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
CREATE TABLE IF NOT EXISTS checkins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    task_id INTEGER,
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
    ("收拾宠物尿垫", 2, 0),
    ("添粮加水", 2, 0),
    ("全屋吸尘", 10, 0),
    ("全屋拖地", 10, 0),
    ("洗晒衣服", 5, 0),
    ("收衣服", 5, 0),
    ("刷马桶", 5, 0),
]


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


def seed_defaults(conn: sqlite3.Connection) -> None:
    have = conn.execute("SELECT COUNT(*) AS c FROM task_catalog").fetchone()["c"]
    if have == 0:
        for i, (name, pts, vocab) in enumerate(DEFAULT_TASKS):
            conn.execute(
                "INSERT INTO task_catalog (name, default_points, is_vocab, sort_order)"
                " VALUES (?, ?, ?, ?)",
                (name, pts, vocab, i),
            )
    if conn.execute("SELECT COUNT(*) AS c FROM supervisors").fetchone()["c"] == 0:
        for nm in ("姐姐", "我"):
            conn.execute("INSERT INTO supervisors (name) VALUES (?)", (nm,))
    conn.commit()
