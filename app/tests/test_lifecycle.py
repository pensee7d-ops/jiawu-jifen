import datetime as dt

from chores import db, lifecycle, weekly


def _conn(tmp_path):
    conn = db.connect(str(tmp_path / "lifecycle.db"))
    db.init_schema(conn)
    return conn


def _stage(conn, mode="daily"):
    conn.execute(
        "INSERT INTO stages (name,starts_on,ends_on,mode,cutoff_hour,"
        "window_start_weekday,window_end_weekday,points_goal,basic_minutes,"
        "reward_minutes,active,created_at,created_by) "
        "VALUES ('暑假','2026-01-01','2026-12-31',?,4,4,0,30,120,120,1,?,?)",
        (mode, "2026-01-01T00:00:00", "惠姐"),
    )
    conn.commit()


def test_daily_reward_is_immediate_idempotent_and_settles(tmp_path):
    conn = _conn(tmp_path)
    _stage(conn)
    now = dt.datetime(2026, 7, 1, 10, 0)
    lifecycle.add_points(conn, 40, "earn", "任务", "test", "one", "浩哥", now.isoformat())
    conn.commit()
    stage, window = lifecycle.ensure_state(conn, now)
    assert window["reward_unlocked"] == 1
    assert lifecycle.ledger_balance(conn) == 40
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM time_grants WHERE grant_type='reward'"
    ).fetchone()["n"] == 1

    lifecycle.ensure_state(conn, now)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM time_grants WHERE grant_type='reward'"
    ).fetchone()["n"] == 1

    lifecycle.ensure_state(conn, dt.datetime(2026, 7, 2, 4, 1))
    assert lifecycle.ledger_balance(conn) == 10
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM point_ledger WHERE entry_type='settlement'"
    ).fetchone()["n"] == 1


def test_weekend_window_is_friday_to_monday(tmp_path):
    conn = _conn(tmp_path)
    _stage(conn, "weekly")
    stage = lifecycle.active_stage(conn, dt.datetime(2026, 7, 3, 8))  # Friday
    start, end = lifecycle.window_bounds(stage, dt.datetime(2026, 7, 4, 12))
    assert start == dt.datetime(2026, 7, 3, 4)
    assert end == dt.datetime(2026, 7, 6, 4)
    assert lifecycle.window_bounds(stage, dt.datetime(2026, 7, 7, 12)) is None


def test_computer_usage_splits_at_logical_day_cutoff(tmp_path):
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO computer_sessions (start_at,end_at) VALUES (?,?)",
        ("2026-07-02T03:00:00", "2026-07-02T05:00:00"),
    )
    conn.execute(
        "INSERT INTO time_grants (logical_date,minutes,grant_type,description,"
        "source_type,source_id,created_at) VALUES ('2026-07-02',120,'base','基础',"
        "'test','base','2026-07-02T04:00:00')"
    )
    conn.commit()
    summary = lifecycle.computer_summary(conn, dt.datetime(2026, 7, 2, 6), 4)
    assert summary["used"] == 60
    assert summary["remaining"] == 60


def test_stage_activation_clips_daily_window_start(tmp_path):
    conn = _conn(tmp_path)
    _stage(conn)
    conn.execute("UPDATE stages SET activated_at='2026-07-01T10:15:00'")
    conn.commit()
    stage = lifecycle.active_stage(conn, dt.datetime(2026, 7, 1, 11))
    start, end = lifecycle.window_bounds(stage, dt.datetime(2026, 7, 1, 11))
    assert start == dt.datetime(2026, 7, 1, 10, 15)
    assert end == dt.datetime(2026, 7, 2, 4)


def test_expired_stage_deactivates_without_fallback(tmp_path):
    conn = _conn(tmp_path)
    _stage(conn)
    conn.execute("UPDATE stages SET ends_on='2026-06-30'")
    conn.commit()
    stage, window = lifecycle.ensure_state(conn, dt.datetime(2026, 7, 1, 10))
    assert stage is None and window is None
    assert conn.execute("SELECT active FROM stages").fetchone()[0] == 0


def _weekly_stage(conn):
    conn.execute(
        "INSERT INTO stages (name,starts_on,ends_on,mode,cutoff_hour,"
        "window_start_weekday,window_end_weekday,points_goal,basic_minutes,"
        "reward_minutes,active,created_at,created_by) "
        "VALUES ('周末计划','2026-01-01','2026-12-31','rules',4,4,0,0,0,0,1,?,?)",
        ("2026-01-01T00:00:00", "惠姐"),
    )
    stage_id = conn.execute("SELECT id FROM stages ORDER BY id DESC").fetchone()["id"]
    weekly.install_template(conn, stage_id, dt.datetime(2026, 7, 1, 10))
    conn.commit()
    return stage_id


def _grant_minutes(conn, date):
    return conn.execute(
        "SELECT COALESCE(SUM(minutes),0) AS n FROM time_grants "
        "WHERE logical_date=? AND source_type='daily_rule_reward' AND deleted_at IS NULL",
        (date,),
    ).fetchone()["n"]


def test_friday_rule_unlocks_150_minutes_once(tmp_path):
    conn = _conn(tmp_path)
    _weekly_stage(conn)
    lifecycle.ensure_state(conn, dt.datetime(2026, 7, 3, 9))
    lifecycle.ensure_state(conn, dt.datetime(2026, 7, 3, 10))
    assert _grant_minutes(conn, "2026-07-03") == 150
    assert conn.execute("SELECT COUNT(*) AS n FROM rule_executions WHERE status='unlocked'").fetchone()["n"] == 1


def test_saturday_week_total_unlocks_immediately_and_is_idempotent(tmp_path):
    conn = _conn(tmp_path)
    _weekly_stage(conn)
    now = dt.datetime(2026, 7, 4, 9)
    lifecycle.ensure_state(conn, now)
    assert _grant_minutes(conn, "2026-07-04") == 0
    lifecycle.add_points(conn, 40, "earn", "周末任务", "test", "sat-40", "浩哥", now.isoformat())
    conn.commit()
    lifecycle.ensure_state(conn, now)
    lifecycle.ensure_state(conn, now)
    assert _grant_minutes(conn, "2026-07-04") == 210


def test_sunday_unlock_closes_cycle_and_later_deduction_does_not_revoke(tmp_path):
    conn = _conn(tmp_path)
    _weekly_stage(conn)
    now = dt.datetime(2026, 7, 5, 11)
    lifecycle.add_points(conn, 70, "earn", "周末任务", "test", "sun-70", "浩哥", now.isoformat())
    conn.commit()
    lifecycle.ensure_state(conn, now)
    cycle = conn.execute("SELECT * FROM weekly_cycles ORDER BY id DESC LIMIT 1").fetchone()
    assert _grant_minutes(conn, "2026-07-05") == 150
    assert cycle["status"] == "completed" and cycle["points_at_close"] == 70
    lifecycle.add_points(conn, -50, "adjustment", "扣分", "test", "after-close", "惠姐", now.isoformat())
    conn.commit()
    lifecycle.ensure_state(conn, now)
    assert _grant_minutes(conn, "2026-07-05") == 150
    assert conn.execute("SELECT points_at_close FROM weekly_cycles WHERE id=?", (cycle["id"],)).fetchone()[0] == 70


def test_sunday_miss_closes_at_monday_cutoff_and_next_cycle_opens(tmp_path):
    conn = _conn(tmp_path)
    _weekly_stage(conn)
    lifecycle.ensure_state(conn, dt.datetime(2026, 7, 5, 12))
    assert _grant_minutes(conn, "2026-07-05") == 0
    lifecycle.ensure_state(conn, dt.datetime(2026, 7, 6, 4, 1))
    cycles = conn.execute("SELECT * FROM weekly_cycles ORDER BY start_at").fetchall()
    assert len(cycles) == 2
    assert cycles[0]["status"] == "completed" and cycles[1]["status"] == "open"
