import datetime as dt


def _parse_dt(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s)


def _date_of(s: str) -> dt.date:
    return _parse_dt(s).date()


def week_start(d: dt.date) -> dt.date:
    """周一为一周起点。"""
    return d - dt.timedelta(days=d.weekday())


def award_for_fixed(task_row) -> int:
    return int(task_row["default_points"])


def _scored_in_week(checkins, today: dt.date):
    start = week_start(today)
    end = start + dt.timedelta(days=7)
    for c in checkins:
        if c["status"] != "scored":
            continue
        d = _date_of(c["created_at"])
        if start <= d < end:
            yield c


def week_total(checkins, today: dt.date) -> int:
    return sum(int(c["awarded_points"] or 0) for c in _scored_in_week(checkins, today))


def today_total(checkins, today: dt.date) -> int:
    return sum(
        int(c["awarded_points"] or 0)
        for c in checkins
        if c["status"] == "scored" and _date_of(c["created_at"]) == today
    )


def streak_days(checkins, today: dt.date) -> int:
    days = {
        _date_of(c["created_at"])
        for c in checkins
        if c["status"] == "scored"
    }
    streak = 0
    cursor = today
    while cursor in days:
        streak += 1
        cursor -= dt.timedelta(days=1)
    return streak


def vocab_all_done(checkins, today: dt.date) -> bool:
    start = week_start(today)
    required = {start + dt.timedelta(days=i) for i in range((today - start).days + 1)}
    done = {
        _date_of(c["created_at"])
        for c in checkins
        if c["status"] == "scored" and int(c.get("is_vocab") or 0) == 1
    }
    return required.issubset(done)


def computer_today(sessions, now: dt.datetime):
    today = now.date()
    total_min = 0
    segments = 0
    for s in sessions:
        start = _parse_dt(s["start_at"])
        if start.date() != today:
            continue
        end = _parse_dt(s["end_at"]) if s["end_at"] else now
        total_min += int((end - start).total_seconds() // 60)
        segments += 1
    return total_min, segments


def progress_ratio(week_pts: int, goal: int) -> float:
    if goal <= 0:
        return 1.0
    return min(1.0, week_pts / goal)
