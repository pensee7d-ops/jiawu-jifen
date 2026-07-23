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
    reviewed_at TEXT,
    deleted_at TEXT,
    deleted_by TEXT,
    delete_reason TEXT,
    purge_after TEXT
);
CREATE TABLE IF NOT EXISTS computer_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_at TEXT NOT NULL,
    end_at TEXT,
    started_by TEXT,
    ended_by TEXT,
    note TEXT,
    source TEXT NOT NULL DEFAULT 'web',
    updated_at TEXT,
    deleted_at TEXT,
    deleted_by TEXT,
    delete_reason TEXT,
    purge_after TEXT
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
CREATE TABLE IF NOT EXISTS stages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    starts_on TEXT NOT NULL,
    ends_on TEXT NOT NULL,
    mode TEXT NOT NULL,
    cutoff_hour INTEGER NOT NULL DEFAULT 4,
    window_start_weekday INTEGER NOT NULL DEFAULT 4,
    window_end_weekday INTEGER NOT NULL DEFAULT 0,
    points_goal INTEGER NOT NULL,
    basic_minutes INTEGER NOT NULL,
    reward_minutes INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    activated_at TEXT
);
CREATE TABLE IF NOT EXISTS settlement_windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stage_id INTEGER NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    points_goal INTEGER NOT NULL,
    reward_minutes INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    reward_unlocked INTEGER NOT NULL DEFAULT 0,
    unlocked_at TEXT,
    settled_at TEXT,
    UNIQUE(stage_id, start_at, end_at),
    FOREIGN KEY(stage_id) REFERENCES stages(id)
);
CREATE TABLE IF NOT EXISTS point_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    delta INTEGER NOT NULL,
    entry_type TEXT NOT NULL,
    description TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    actor_name TEXT,
    created_at TEXT NOT NULL,
    window_id INTEGER,
    deleted_at TEXT,
    deleted_by TEXT,
    delete_reason TEXT,
    purge_after TEXT,
    UNIQUE(source_type, source_id),
    FOREIGN KEY(window_id) REFERENCES settlement_windows(id)
);
CREATE TABLE IF NOT EXISTS time_grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    logical_date TEXT NOT NULL,
    minutes INTEGER NOT NULL,
    grant_type TEXT NOT NULL,
    description TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    actor_name TEXT,
    created_at TEXT NOT NULL,
    deleted_at TEXT,
    deleted_by TEXT,
    delete_reason TEXT,
    purge_after TEXT,
    UNIQUE(source_type, source_id)
);
CREATE TABLE IF NOT EXISTS computer_session_audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    before_start TEXT,
    before_end TEXT,
    after_start TEXT,
    after_end TEXT,
    actor_name TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES computer_sessions(id)
);
CREATE TABLE IF NOT EXISTS missions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT,
    points INTEGER NOT NULL,
    deadline_at TEXT,
    photo_required INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'open',
    published_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    claimed_at TEXT,
    submitted_at TEXT,
    reviewed_at TEXT,
    reviewed_by TEXT,
    submission_note TEXT,
    photo_path TEXT,
    rejection_reason TEXT,
    awarded_points INTEGER,
    deleted_at TEXT,
    deleted_by TEXT,
    delete_reason TEXT,
    purge_after TEXT
);
CREATE TABLE IF NOT EXISTS mission_submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id INTEGER NOT NULL,
    note TEXT,
    photo_path TEXT,
    submitted_at TEXT NOT NULL,
    reviewed_at TEXT,
    reviewer TEXT,
    decision TEXT,
    points INTEGER,
    reason TEXT,
    FOREIGN KEY(mission_id) REFERENCES missions(id)
);
CREATE TABLE IF NOT EXISTS record_deletions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id INTEGER NOT NULL,
    display_text TEXT NOT NULL,
    deleted_by TEXT NOT NULL,
    reason TEXT NOT NULL,
    deleted_at TEXT NOT NULL,
    purge_after TEXT NOT NULL,
    restored_at TEXT,
    restored_by TEXT,
    purged_at TEXT
);
CREATE TABLE IF NOT EXISTS stage_switches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    old_stage_id INTEGER,
    new_stage_id INTEGER NOT NULL,
    switched_by TEXT NOT NULL,
    switched_at TEXT NOT NULL,
    FOREIGN KEY(old_stage_id) REFERENCES stages(id),
    FOREIGN KEY(new_stage_id) REFERENCES stages(id)
);
CREATE INDEX IF NOT EXISTS idx_point_ledger_created ON point_ledger(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_time_grants_date ON time_grants(logical_date);
CREATE INDEX IF NOT EXISTS idx_missions_status ON missions(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_deletions_active ON record_deletions(restored_at, purged_at, purge_after);
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
    """可重复执行的增量迁移，并把当前旧周期转为期初流水。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(checkins)").fetchall()}
    if "period_id" not in cols:
        conn.execute("ALTER TABLE checkins ADD COLUMN period_id INTEGER")
    session_cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(computer_sessions)").fetchall()
    }
    additions = {
        "started_by": "TEXT",
        "ended_by": "TEXT",
        "note": "TEXT",
        "source": "TEXT NOT NULL DEFAULT 'legacy'",
        "updated_at": "TEXT",
    }
    for name, sql_type in additions.items():
        if session_cols and name not in session_cols:
            conn.execute(f"ALTER TABLE computer_sessions ADD COLUMN {name} {sql_type}")

    soft_delete = {
        "deleted_at": "TEXT", "deleted_by": "TEXT",
        "delete_reason": "TEXT", "purge_after": "TEXT",
    }
    for table in ("checkins", "computer_sessions", "point_ledger", "time_grants", "missions"):
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue
        for name, sql_type in soft_delete.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
    stage_cols = {r["name"] for r in conn.execute("PRAGMA table_info(stages)")}
    if stage_cols and "activated_at" not in stage_cols:
        conn.execute("ALTER TABLE stages ADD COLUMN activated_at TEXT")

    # 只迁移当前开放旧周期；归档周期仍可在旧历史页面查看，但不进入余额。
    have_periods = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='periods'"
    ).fetchone()
    have_ledger = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='point_ledger'"
    ).fetchone()
    migrated = conn.execute(
        "SELECT value FROM settings WHERE key='ledger_v2_migrated'"
    ).fetchone() if have_periods and have_ledger else None
    cur = conn.execute(
        "SELECT id FROM periods WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone() if have_periods and have_ledger and not migrated else None
    if cur and not migrated:
        conn.execute(
            "INSERT OR IGNORE INTO point_ledger "
            "(delta, entry_type, description, source_type, source_id, actor_name, created_at) "
            "SELECT COALESCE(c.awarded_points,0), "
            "CASE WHEN c.kind='adjustment' THEN 'adjustment' ELSE 'opening' END, "
            "CASE WHEN c.kind='adjustment' THEN COALESCE(c.note,'旧系统积分调整') "
            "ELSE '旧系统当前周期：' || COALESCE(t.name,c.title,'打卡') END, "
            "'legacy_checkin', CAST(c.id AS TEXT), c.reviewed_by, c.created_at "
            "FROM checkins c LEFT JOIN task_catalog t ON t.id=c.task_id "
            "WHERE c.period_id=? AND c.status='scored'",
            (cur["id"],),
        )
    if have_periods and have_ledger and not migrated:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key,value) VALUES ('ledger_v2_migrated','1')"
        )
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
    if conn.execute(
        "SELECT COUNT(*) AS c FROM settings WHERE key='period_goal'"
    ).fetchone()["c"] == 0:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('period_goal', '80')"
        )
    conn.commit()


def get_setting(conn: sqlite3.Connection, key: str, default=None):
    r = conn.execute(
        "SELECT value FROM settings WHERE key=?", (key,)
    ).fetchone()
    return r["value"] if r else default


def set_setting(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
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
