import datetime as dt


def _parse_dt(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s)


def _date_of(s: str) -> dt.date:
    return _parse_dt(s).date()


def award_for_fixed(task_row) -> int:
    return int(task_row["default_points"])


def period_total(checkins) -> int:
    """当前周期累计：传入的已是本周期 checkins，求已计分项之和。

    含监管者的 adjustment（扣分/加分）——它们计入总分，只是不在动态流展示。
    """
    return sum(
        int(c["awarded_points"] or 0)
        for c in checkins
        if c["status"] == "scored"
    )


def today_total(checkins, today: dt.date) -> int:
    return sum(
        int(c["awarded_points"] or 0)
        for c in checkins
        if c["status"] == "scored" and _date_of(c["created_at"]) == today
    )


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


def progress_ratio(period_pts: int, goal: int) -> float:
    if goal <= 0:
        return 1.0
    return min(1.0, period_pts / goal)
