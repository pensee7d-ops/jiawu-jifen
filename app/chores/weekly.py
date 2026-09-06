import datetime as dt
import json


def is_template(conn, stage_id):
    return conn.execute("SELECT * FROM rule_templates WHERE stage_id=?", (stage_id,)).fetchone()


def install_template(conn, stage_id, now, auto_renew=1):
    if is_template(conn, stage_id):
        return
    stamp = now.isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO rule_templates(stage_id,auto_renew,created_at) VALUES (?,?,?)",
        (stage_id, auto_renew, stamp),
    )
    for weekday, condition, threshold, minutes, ends in (
        (4, "none", 0, 150, 0), (5, "week_total", 40, 210, 0),
        (6, "week_total", 70, 150, 1),
    ):
        conn.execute(
            "INSERT INTO daily_rules(stage_id,weekday,condition_type,points_threshold,"
            "reward_minutes,ends_cycle,sort_order,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (stage_id, weekday, condition, threshold, minutes, ends, weekday, stamp, stamp),
        )


def migrate_active(conn, now):
    from chores.lifecycle import settle_open_stage

    if conn.execute("SELECT 1 FROM settings WHERE key='daily_rules_v1'").fetchone():
        return
    with conn:
        for stage in conn.execute("SELECT * FROM stages WHERE active=1").fetchall():
            settle_open_stage(conn, stage["id"], now)
            install_template(conn, stage["id"], now)
            ensure_cycle(conn, stage, now, migrating=True)
        conn.execute("INSERT INTO settings(key,value) VALUES ('daily_rules_v1','1')")


def points_between(conn, start, end, now):
    return int(conn.execute(
        "SELECT COALESCE(SUM(delta),0) FROM point_ledger WHERE deleted_at IS NULL "
        "AND entry_type IN ('earn','adjustment','opening') AND created_at>=? "
        "AND created_at<? AND created_at<=?",
        (start, end, now.isoformat(timespec="seconds")),
    ).fetchone()[0])


def cycle_points(conn, cycle, now):
    if cycle["points_at_close"] is not None:
        return int(cycle["points_at_close"])
    return points_between(conn, cycle["start_at"], cycle["end_at"], now)


def close_cycle(conn, cycle, when, reason):
    if cycle["status"] != "open":
        return
    points = cycle_points(conn, cycle, when)
    stamp = when.isoformat(timespec="seconds")
    conn.execute(
        "UPDATE weekly_cycles SET status='completed',completed_at=?,points_at_close=? "
        "WHERE id=? AND status='open'", (stamp, points, cycle["id"]),
    )
    conn.execute(
        "UPDATE rule_executions SET status='skipped',note='周期已结束，未发放' "
        "WHERE cycle_id=? AND status='pending'", (cycle["id"],),
    )
    conn.execute(
        "INSERT INTO cycle_events(cycle_id,occurred_at,description) VALUES (?,?,?)",
        (cycle["id"], stamp, f"{reason} · 本周 {points} 分"),
    )


def settle_expired(conn, now):
    for cycle in conn.execute(
        "SELECT * FROM weekly_cycles WHERE status='open' AND end_at<=?", (now.isoformat(),),
    ).fetchall():
        close_cycle(conn, cycle, dt.datetime.fromisoformat(cycle["end_at"]), "周期到期自动封存")


def current_cycle(conn, stage_id, now):
    return conn.execute(
        "SELECT * FROM weekly_cycles WHERE stage_id=? AND start_at<=? AND end_at>? "
        "ORDER BY id DESC LIMIT 1", (stage_id, now.isoformat(), now.isoformat()),
    ).fetchone()


def ensure_cycle(conn, stage, now, migrating=False):
    cycle = current_cycle(conn, stage["id"], now)
    if cycle is None:
        template = is_template(conn, stage["id"])
        previous = conn.execute(
            "SELECT * FROM weekly_cycles WHERE stage_id=? ORDER BY id DESC LIMIT 1",
            (stage["id"],),
        ).fetchone()
        if previous and not template["auto_renew"]:
            return None
        day = (now - dt.timedelta(hours=int(stage["cutoff_hour"]))).date()
        monday = day - dt.timedelta(days=day.weekday())
        sunday = monday + dt.timedelta(days=6)
        start = dt.datetime.combine(max(monday, dt.date.fromisoformat(stage["starts_on"])),
                                    dt.time(int(stage["cutoff_hour"])))
        end = dt.datetime.combine(min(sunday, dt.date.fromisoformat(stage["ends_on"])) + dt.timedelta(days=1),
                                  dt.time(int(stage["cutoff_hour"])))
        if not start <= now < end:
            return None
        rules = [dict(row) for row in conn.execute(
            "SELECT * FROM daily_rules WHERE stage_id=? AND active=1 ORDER BY weekday,sort_order,id",
            (stage["id"],),
        )]
        conn.execute(
            "INSERT OR IGNORE INTO weekly_cycles(stage_id,start_date,end_date,created_at,"
            "cutoff_hour,rules_json,name,start_at,end_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (stage["id"], monday.isoformat(), sunday.isoformat(), now.isoformat(timespec="seconds"),
             stage["cutoff_hour"], json.dumps(rules), stage["name"], start.isoformat(), end.isoformat()),
        )
        cycle = current_cycle(conn, stage["id"], now)
        if cycle is None:
            return None
        for rule in rules:
            date = monday + dt.timedelta(days=rule["weekday"])
            valid_day = start.date() <= date < end.date()
            status = "pending" if date >= day and valid_day else "skipped"
            if migrating and date == day and conn.execute(
                "SELECT 1 FROM time_grants WHERE logical_date=? AND deleted_at IS NULL "
                "AND source_type IN ('stage_base','settlement_reward') AND minutes>0",
                (day.isoformat(),),
            ).fetchone():
                status = "skipped"
            conn.execute(
                "INSERT OR IGNORE INTO rule_executions(cycle_id,rule_id,logical_date,status,note) "
                "VALUES (?,?,?,?,?)",
                (cycle["id"], rule["id"], date.isoformat(), status,
                 "等待对应日期和积分达标" if status == "pending" else "已过期、超出生效范围或迁移日保留旧额度，不补发"),
            )
        conn.execute(
            "INSERT INTO cycle_events(cycle_id,occurred_at,description) VALUES (?,?,?)",
            (cycle["id"], now.isoformat(timespec="seconds"), "开启本周周期，规则已生成快照"),
        )
    if cycle["status"] == "open":
        evaluate(conn, cycle, now)
    return conn.execute("SELECT * FROM weekly_cycles WHERE id=?", (cycle["id"],)).fetchone()


def evaluate(conn, cycle, now):
    day = (now - dt.timedelta(hours=cycle["cutoff_hour"])).date()
    conn.execute(
        "UPDATE rule_executions SET status='skipped',note='当天已结束，不补发过期时长' "
        "WHERE cycle_id=? AND logical_date<? AND status='pending'", (cycle["id"], day.isoformat()),
    )
    for rule in json.loads(cycle["rules_json"]):
        if rule["weekday"] != day.weekday():
            continue
        execution = conn.execute(
            "SELECT * FROM rule_executions WHERE cycle_id=? AND rule_id=?",
            (cycle["id"], rule["id"]),
        ).fetchone()
        if not execution or execution["status"] != "pending":
            continue
        points = rule_points(conn, cycle, rule, day, now)
        if rule["condition_type"] != "none" and points < rule["points_threshold"]:
            continue
        stamp = now.isoformat(timespec="seconds")
        note = f"周{'一二三四五六日'[day.weekday()]}规则 · {points} 分达标，解锁 {rule['reward_minutes']} 分钟"
        conn.execute(
            "INSERT OR IGNORE INTO time_grants(logical_date,minutes,grant_type,description,"
            "source_type,source_id,actor_name,created_at) VALUES (?,?,'daily_rule',?,'daily_rule_reward',?,'系统',?)",
            (day.isoformat(), rule["reward_minutes"], note, str(execution["id"]), stamp),
        )
        conn.execute(
            "UPDATE rule_executions SET status='unlocked',unlocked_at=?,reward_minutes=?,note=? WHERE id=?",
            (stamp, rule["reward_minutes"], note, execution["id"]),
        )
        conn.execute(
            "INSERT INTO cycle_events(cycle_id,occurred_at,description) VALUES (?,?,?)",
            (cycle["id"], stamp, note),
        )
        if rule["ends_cycle"]:
            close_cycle(conn, cycle, now, "周日规则达标，本周已结算")
            break


def rule_points(conn, cycle, rule, day, now):
    if rule["condition_type"] == "day_total":
        start = dt.datetime.combine(day, dt.time(cycle["cutoff_hour"]))
        return points_between(conn, start.isoformat(), (start + dt.timedelta(days=1)).isoformat(), now)
    return cycle_points(conn, cycle, now)


def summary(conn, stage, now):
    if not stage or not is_template(conn, stage["id"]):
        return None
    cycle = current_cycle(conn, stage["id"], now)
    if not cycle:
        return {"cycle": None, "rules": [], "points": 0, "message": "周期已暂停，开启自动续周后恢复"}
    day = (now - dt.timedelta(hours=cycle["cutoff_hour"])).date()
    executions = {row["rule_id"]: dict(row) for row in conn.execute(
        "SELECT * FROM rule_executions WHERE cycle_id=?", (cycle["id"],),
    )}
    rules = []
    for rule in json.loads(cycle["rules_json"]):
        execution = executions[rule["id"]]
        points = rule_points(conn, cycle, rule, dt.date.fromisoformat(execution["logical_date"]), now)
        rules.append({**rule, **{"execution": execution, "remaining": max(0, rule["points_threshold"] - points),
                                "today": execution["logical_date"] == day.isoformat()}})
    upcoming = next((rule for rule in rules if rule["execution"]["status"] == "pending"), None)
    return {"cycle": dict(cycle), "rules": rules, "points": cycle_points(conn, cycle, now),
            "next": upcoming, "logical_date": day.isoformat(),
            "next_date": (dt.date.fromisoformat(cycle["end_date"]) + dt.timedelta(days=1)).isoformat(),
            "auto_renew": is_template(conn, stage["id"])["auto_renew"],
            "events": conn.execute("SELECT * FROM cycle_events WHERE cycle_id=? ORDER BY id DESC",
                                   (cycle["id"],)).fetchall()}


def validate_rules(rules):
    active = sorted((rule for rule in rules if rule["active"]), key=lambda rule: rule["weekday"])
    if len({rule["weekday"] for rule in active}) != len(active):
        raise ValueError("同一天只能配置一条启用的规则")
    previous = 0
    for rule in active:
        if rule["ends_cycle"] and rule["weekday"] != 6:
            raise ValueError("结束周期规则必须在周日，且是最后一条")
        if rule["condition_type"] == "week_total":
            if rule["points_threshold"] < previous:
                raise ValueError("本周累计积分阈值不能随星期倒退")
            previous = rule["points_threshold"]
