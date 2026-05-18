import datetime as dt
from chores import scoring


def test_award_points_fixed_uses_task_default():
    row = {"default_points": 10}
    assert scoring.award_for_fixed(row) == 10


def test_period_total_sums_scored_including_adjustments():
    rows = [
        {"awarded_points": 10, "status": "scored", "kind": "fixed"},
        {"awarded_points": 5, "status": "scored", "kind": "adhoc"},
        {"awarded_points": 99, "status": "pending", "kind": "adhoc"},
        {"awarded_points": 50, "status": "rejected", "kind": "adhoc"},
        {"awarded_points": -3, "status": "scored", "kind": "adjustment"},
    ]
    # 10 + 5 - 3 = 12（pending/rejected 不算；adjustment 计入）
    assert scoring.period_total(rows) == 12


def test_period_total_empty_is_zero():
    assert scoring.period_total([]) == 0


def test_today_total():
    today = dt.date(2026, 5, 14)
    checkins = [
        {"awarded_points": 8, "status": "scored", "created_at": "2026-05-14T07:00:00"},
        {"awarded_points": 4, "status": "scored", "created_at": "2026-05-13T07:00:00"},
    ]
    assert scoring.today_total(checkins, today) == 8


def test_computer_minutes_sums_closed_and_open_segments():
    now = dt.datetime(2026, 5, 14, 18, 0, 0)
    sessions = [
        {"start_at": "2026-05-14T10:00:00", "end_at": "2026-05-14T11:30:00"},
        {"start_at": "2026-05-14T17:00:00", "end_at": None},
        {"start_at": "2026-05-13T10:00:00", "end_at": "2026-05-13T12:00:00"},
    ]
    total, segments = scoring.computer_today(sessions, now)
    assert total == 150  # 90 + 60
    assert segments == 2


def test_progress_ratio_caps_at_one():
    assert scoring.progress_ratio(150, 300) == 0.5
    assert scoring.progress_ratio(900, 300) == 1.0
    assert scoring.progress_ratio(10, 0) == 1.0
