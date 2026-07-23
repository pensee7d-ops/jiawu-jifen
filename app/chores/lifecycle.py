import datetime as dt


def parse_dt(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value)


def logical_date(now: dt.datetime, cutoff_hour: int = 4) -> dt.date:
    return (now - dt.timedelta(hours=cutoff_hour)).date()


def active_stage(conn, now: dt.datetime):
    day = logical_date(now)
    return conn.execute(
        "SELECT * FROM stages WHERE active=1 AND starts_on<=? AND ends_on>=? "
        "ORDER BY id DESC LIMIT 1",
        (day.isoformat(), day.isoformat()),
    ).fetchone()


def window_bounds(stage, now: dt.datetime):
    """返回当前有效小周期；周末窗口之外返回 None。"""
    cutoff = int(stage["cutoff_hour"])
    boundary = now.replace(hour=cutoff, minute=0, second=0, microsecond=0)
    if now < boundary:
        boundary -= dt.timedelta(days=1)
    if stage["mode"] == "daily":
        start = boundary
        if stage["activated_at"]:
            activated = parse_dt(stage["activated_at"])
            if boundary <= activated < boundary + dt.timedelta(days=1):
                start = activated
        return start, boundary + dt.timedelta(days=1)

    start_weekday = int(stage["window_start_weekday"])
    days_since_start = (boundary.weekday() - start_weekday) % 7
    start = boundary - dt.timedelta(days=days_since_start)
    end_weekday = int(stage["window_end_weekday"])
    span = (end_weekday - start_weekday) % 7
    if span == 0:
        span = 7
    end = start + dt.timedelta(days=span)
    if not (start <= now < end):
        return None
    if stage["activated_at"]:
        activated = parse_dt(stage["activated_at"])
        if start <= activated < end:
            start = activated
    return start, end


def ledger_balance(conn) -> int:
    return int(conn.execute(
        "SELECT COALESCE(SUM(delta),0) AS n FROM point_ledger WHERE deleted_at IS NULL"
    ).fetchone()["n"])


def add_points(
    conn, delta: int, entry_type: str, description: str,
    source_type: str, source_id, actor_name: str, created_at: str,
    window_id=None,
):
    conn.execute(
        "INSERT OR IGNORE INTO point_ledger "
        "(delta,entry_type,description,source_type,source_id,actor_name,created_at,window_id) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (int(delta), entry_type, description, source_type, str(source_id),
         actor_name, created_at, window_id),
    )


def _settle_expired(conn, now: dt.datetime):
    rows = conn.execute(
        "SELECT w.* FROM settlement_windows w "
        "JOIN stages s ON s.id=w.stage_id WHERE w.status='open' AND w.end_at<=?",
        (now.isoformat(timespec="seconds"),),
    ).fetchall()
    for row in rows:
        if row["reward_unlocked"]:
            add_points(
                conn, -int(row["points_goal"]), "settlement",
                "达标时长自动兑换", "settlement_window", row["id"], "系统",
                row["end_at"], row["id"],
            )
        conn.execute(
            "UPDATE settlement_windows SET status='settled', settled_at=? WHERE id=?",
            (row["end_at"], row["id"]),
        )


def settle_open_stage(conn, stage_id: int, now: dt.datetime):
    """切换阶段时在点击时刻截断并结算旧阶段的开放窗口。"""
    now_iso = now.isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT * FROM settlement_windows WHERE stage_id=? AND status='open'",
        (stage_id,),
    ).fetchall()
    for row in rows:
        if row["reward_unlocked"]:
            add_points(
                conn, -int(row["points_goal"]), "settlement", "阶段切换自动兑换",
                "settlement_window", row["id"], "系统", now_iso, row["id"],
            )
        conn.execute(
            "UPDATE settlement_windows SET end_at=?,status='settled',settled_at=? WHERE id=?",
            (now_iso, now_iso, row["id"]),
        )


def _ensure_base_grant(conn, stage, now: dt.datetime):
    day = logical_date(now, int(stage["cutoff_hour"])).isoformat()
    conn.execute(
        "INSERT INTO time_grants "
        "(logical_date,minutes,grant_type,description,source_type,source_id,actor_name,created_at) "
        "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(source_type,source_id) DO UPDATE SET "
        "minutes=excluded.minutes,description=excluded.description,deleted_at=NULL,deleted_by=NULL,"
        "delete_reason=NULL,purge_after=NULL",
        (day, int(stage["basic_minutes"]), "base", f"{stage['name']}基础时长",
         "stage_base", f"{stage['id']}:{day}", "系统",
         now.isoformat(timespec="seconds")),
    )


def _reconcile_open_reward(conn, window, stage, now: dt.datetime):
    goal = int(window["points_goal"])
    enough = goal > 0 and ledger_balance(conn) >= goal
    if window["reward_unlocked"] and not enough:
        conn.execute(
            "UPDATE settlement_windows SET reward_unlocked=0,unlocked_at=NULL WHERE id=?",
            (window["id"],),
        )
        conn.execute(
            "UPDATE time_grants SET deleted_at=?,deleted_by='系统',delete_reason='当前窗口积分不足',"
            "purge_after=NULL WHERE source_type='settlement_reward' AND source_id=? "
            "AND deleted_at IS NULL",
            (now.isoformat(timespec="seconds"), str(window["id"])),
        )
        return conn.execute(
            "SELECT * FROM settlement_windows WHERE id=?", (window["id"],)
        ).fetchone()
    if not window["reward_unlocked"] and enough:
        unlocked_at = now.isoformat(timespec="seconds")
        conn.execute(
            "UPDATE settlement_windows SET reward_unlocked=1, unlocked_at=? "
            "WHERE id=? AND reward_unlocked=0", (unlocked_at, window["id"]),
        )
        day = logical_date(now, int(stage["cutoff_hour"])).isoformat()
        conn.execute(
            "INSERT INTO time_grants "
            "(logical_date,minutes,grant_type,description,source_type,source_id,actor_name,created_at) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(source_type,source_id) DO UPDATE SET "
            "logical_date=excluded.logical_date,minutes=excluded.minutes,deleted_at=NULL,deleted_by=NULL,"
            "delete_reason=NULL,purge_after=NULL",
            (day, int(window["reward_minutes"]), "reward", "积分达标奖励",
             "settlement_reward", str(window["id"]), "系统", unlocked_at),
        )
        return conn.execute(
            "SELECT * FROM settlement_windows WHERE id=?", (window["id"],)
        ).fetchone()
    return window


def ensure_state(conn, now: dt.datetime):
    """补结算、创建当前窗口/基础时长，并在达标时即时发放奖励。"""
    _settle_expired(conn, now)
    for candidate in conn.execute("SELECT * FROM stages WHERE active=1").fetchall():
        day = logical_date(now, int(candidate["cutoff_hour"])).isoformat()
        if day < candidate["starts_on"] or day > candidate["ends_on"]:
            conn.execute("UPDATE stages SET active=0 WHERE id=?", (candidate["id"],))
    stage = active_stage(conn, now)
    window = None
    if stage:
        bounds = window_bounds(stage, now)
        if bounds:
            start, end = bounds
            conn.execute(
                "INSERT OR IGNORE INTO settlement_windows "
                "(stage_id,start_at,end_at,points_goal,reward_minutes) VALUES (?,?,?,?,?)",
                (stage["id"], start.isoformat(timespec="seconds"),
                 end.isoformat(timespec="seconds"), int(stage["points_goal"]),
                 int(stage["reward_minutes"])),
            )
            window = conn.execute(
                "SELECT * FROM settlement_windows WHERE stage_id=? AND start_at=? AND end_at=?",
                (stage["id"], start.isoformat(timespec="seconds"),
                 end.isoformat(timespec="seconds")),
            ).fetchone()
            _ensure_base_grant(conn, stage, now)
            window = _reconcile_open_reward(conn, window, stage, now)
    conn.commit()
    return stage, window


def point_summary(conn, stage=None, window=None):
    agg = conn.execute(
        "SELECT COALESCE(SUM(delta),0) AS balance, "
        "COALESCE(SUM(CASE WHEN delta>0 THEN delta ELSE 0 END),0) AS earned, "
        "COALESCE(-SUM(CASE WHEN delta<0 THEN delta ELSE 0 END),0) AS deducted "
        "FROM point_ledger WHERE deleted_at IS NULL"
    ).fetchone()
    locked = int(window["points_goal"]) if window and window["reward_unlocked"] else 0
    goal = int(window["points_goal"]) if window else (int(stage["points_goal"]) if stage else 0)
    balance = int(agg["balance"])
    return {
        "balance": balance,
        "earned": int(agg["earned"]),
        "deducted": int(agg["deducted"]),
        "locked": locked,
        "projected": balance - locked,
        "goal": goal,
        "progress": min(100, max(0, int(balance / goal * 100))) if goal else 0,
    }


def day_bounds(now: dt.datetime, cutoff_hour: int = 4):
    day = logical_date(now, cutoff_hour)
    start = dt.datetime.combine(day, dt.time(hour=cutoff_hour))
    return day, start, start + dt.timedelta(days=1)


def computer_summary(conn, now: dt.datetime, cutoff_hour: int = 4):
    day, start, end = day_bounds(now, cutoff_hour)
    sessions = conn.execute(
        "SELECT * FROM computer_sessions WHERE deleted_at IS NULL AND start_at<? AND COALESCE(end_at,?)>? "
        "ORDER BY start_at",
        (end.isoformat(), now.isoformat(), start.isoformat()),
    ).fetchall()
    used_seconds = 0
    details = []
    for row in sessions:
        raw_start = parse_dt(row["start_at"])
        raw_end = parse_dt(row["end_at"]) if row["end_at"] else now
        seg_start, seg_end = max(raw_start, start), min(raw_end, end, now)
        seconds = max(0, int((seg_end - seg_start).total_seconds()))
        used_seconds += seconds
        item = dict(row)
        item["minutes"] = seconds // 60
        details.append(item)
    grants = [dict(r) for r in conn.execute(
        "SELECT * FROM time_grants WHERE logical_date=? AND deleted_at IS NULL ORDER BY id", (day.isoformat(),)
    ).fetchall()]
    allowance = max(0, sum(int(g["minutes"]) for g in grants))
    used = used_seconds // 60
    open_session = conn.execute(
        "SELECT * FROM computer_sessions WHERE end_at IS NULL AND deleted_at IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return {
        "logical_date": day.isoformat(), "allowance": allowance, "used": used,
        "remaining": max(0, allowance - used), "overrun": max(0, used - allowance),
        "sessions": details, "grants": grants,
        "open_session": dict(open_session) if open_session else None,
    }
