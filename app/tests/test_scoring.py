import datetime as dt
from chores import scoring


def test_week_start_is_monday():
    # 2026-05-17 是周日
    d = dt.date(2026, 5, 17)
    assert scoring.week_start(d) == dt.date(2026, 5, 11)


def test_week_start_on_monday_returns_same_day():
    d = dt.date(2026, 5, 11)
    assert scoring.week_start(d) == dt.date(2026, 5, 11)


def test_award_points_fixed_uses_task_default():
    row = {"default_points": 10}
    assert scoring.award_for_fixed(row) == 10


def test_week_total_only_counts_scored_in_window():
    today = dt.date(2026, 5, 14)
    checkins = [
        {"awarded_points": 10, "status": "scored", "created_at": "2026-05-12T08:00:00"},
        {"awarded_points": 5, "status": "scored", "created_at": "2026-05-14T09:00:00"},
        {"awarded_points": 99, "status": "pending", "created_at": "2026-05-14T09:00:00"},
        {"awarded_points": 7, "status": "scored", "created_at": "2026-05-04T09:00:00"},
        {"awarded_points": -3, "status": "scored", "created_at": "2026-05-13T09:00:00"},
    ]
    assert scoring.week_total(checkins, today) == 12


def test_today_total():
    today = dt.date(2026, 5, 14)
    checkins = [
        {"awarded_points": 8, "status": "scored", "created_at": "2026-05-14T07:00:00"},
        {"awarded_points": 4, "status": "scored", "created_at": "2026-05-13T07:00:00"},
    ]
    assert scoring.today_total(checkins, today) == 8


def test_streak_days_counts_back_from_today():
    today = dt.date(2026, 5, 14)
    checkins = [
        {"status": "scored", "created_at": "2026-05-14T07:00:00"},
        {"status": "scored", "created_at": "2026-05-13T20:00:00"},
        {"status": "scored", "created_at": "2026-05-12T20:00:00"},
        # 5-11 缺勤，断
        {"status": "scored", "created_at": "2026-05-10T20:00:00"},
    ]
    assert scoring.streak_days(checkins, today) == 3


def test_streak_zero_when_no_checkin_today():
    today = dt.date(2026, 5, 14)
    checkins = [{"status": "scored", "created_at": "2026-05-13T07:00:00"}]
    assert scoring.streak_days(checkins, today) == 0


def test_vocab_all_done_this_week_true_when_every_day_has_vocab():
    today = dt.date(2026, 5, 13)  # 周三，本周已过周一~周三
    checkins = [
        {"status": "scored", "is_vocab": 1, "created_at": "2026-05-11T07:00:00"},
        {"status": "scored", "is_vocab": 1, "created_at": "2026-05-12T07:00:00"},
        {"status": "scored", "is_vocab": 1, "created_at": "2026-05-13T07:00:00"},
    ]
    assert scoring.vocab_all_done(checkins, today) is True


def test_vocab_all_done_false_when_a_day_missing():
    today = dt.date(2026, 5, 13)
    checkins = [
        {"status": "scored", "is_vocab": 1, "created_at": "2026-05-11T07:00:00"},
        {"status": "scored", "is_vocab": 1, "created_at": "2026-05-13T07:00:00"},
    ]
    assert scoring.vocab_all_done(checkins, today) is False


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
