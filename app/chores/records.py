import datetime as dt
import os

from chores import lifecycle


ALLOWED_TYPES = {"checkin", "mission", "computer_session", "time_adjustment"}


def _iso(value: dt.datetime) -> str:
    return value.isoformat(timespec="seconds")


def _mark(conn, table, record_id, actor, reason, now, purge_after):
    conn.execute(
        f"UPDATE {table} SET deleted_at=?,deleted_by=?,delete_reason=?,purge_after=? "
        "WHERE id=? AND deleted_at IS NULL",
        (_iso(now), actor, reason, _iso(purge_after), record_id),
    )
    return conn.total_changes


def trash_record(conn, entity_type, record_id, actor, reason, now):
    if entity_type not in ALLOWED_TYPES or not reason.strip():
        raise ValueError("记录类型或删除原因无效")
    purge_after = now + dt.timedelta(days=7)
    display = ""
    if entity_type == "checkin":
        row = conn.execute(
            "SELECT * FROM checkins WHERE id=? AND deleted_at IS NULL", (record_id,)
        ).fetchone()
        if not row:
            raise ValueError("记录不存在或已删除")
        display = row["title"] or f"打卡 #{record_id}"
        _mark(conn, "checkins", record_id, actor, reason, now, purge_after)
        conn.execute(
            "UPDATE point_ledger SET deleted_at=?,deleted_by=?,delete_reason=?,purge_after=? "
            "WHERE source_type IN ('checkin','legacy_checkin') AND source_id=? AND deleted_at IS NULL",
            (_iso(now), actor, reason, _iso(purge_after), str(record_id)),
        )
    elif entity_type == "mission":
        row = conn.execute(
            "SELECT * FROM missions WHERE id=? AND deleted_at IS NULL", (record_id,)
        ).fetchone()
        if not row or row["status"] != "completed":
            raise ValueError("只能回收已完成任务")
        display = row["title"]
        _mark(conn, "missions", record_id, actor, reason, now, purge_after)
        conn.execute(
            "UPDATE point_ledger SET deleted_at=?,deleted_by=?,delete_reason=?,purge_after=? "
            "WHERE source_type='mission' AND source_id=? AND deleted_at IS NULL",
            (_iso(now), actor, reason, _iso(purge_after), str(record_id)),
        )
    elif entity_type == "computer_session":
        row = conn.execute(
            "SELECT * FROM computer_sessions WHERE id=? AND deleted_at IS NULL", (record_id,)
        ).fetchone()
        if not row:
            raise ValueError("电脑记录不存在或已删除")
        display = f"电脑会话 {row['start_at'][:16]}"
        _mark(conn, "computer_sessions", record_id, actor, reason, now, purge_after)
    else:
        row = conn.execute(
            "SELECT * FROM time_grants WHERE id=? AND source_type='manual_time' "
            "AND deleted_at IS NULL", (record_id,),
        ).fetchone()
        if not row:
            raise ValueError("只能回收人工时长调整")
        display = row["description"]
        _mark(conn, "time_grants", record_id, actor, reason, now, purge_after)
    deletion_id = conn.execute(
        "INSERT INTO record_deletions "
        "(entity_type,entity_id,display_text,deleted_by,reason,deleted_at,purge_after) "
        "VALUES (?,?,?,?,?,?,?)",
        (entity_type, record_id, display, actor, reason.strip(), _iso(now), _iso(purge_after)),
    ).lastrowid
    conn.commit()
    lifecycle.ensure_state(conn, now)
    return deletion_id


def restore_record(conn, deletion_id, actor, now):
    deletion = conn.execute(
        "SELECT * FROM record_deletions WHERE id=? AND restored_at IS NULL AND purged_at IS NULL",
        (deletion_id,),
    ).fetchone()
    if not deletion or dt.datetime.fromisoformat(deletion["purge_after"]) <= now:
        raise ValueError("该记录已不可恢复")
    entity_type, record_id = deletion["entity_type"], deletion["entity_id"]
    table = {
        "checkin": "checkins", "mission": "missions",
        "computer_session": "computer_sessions", "time_adjustment": "time_grants",
    }.get(entity_type)
    if not table:
        raise ValueError("记录类型无效")
    if entity_type == "computer_session":
        session = conn.execute(
            "SELECT * FROM computer_sessions WHERE id=?", (record_id,),
        ).fetchone()
        overlap = conn.execute(
            "SELECT 1 FROM computer_sessions WHERE id!=? AND deleted_at IS NULL "
            "AND start_at<COALESCE(?, '9999-12-31T23:59:59') "
            "AND COALESCE(end_at,'9999-12-31T23:59:59')>? LIMIT 1",
            (record_id, session["end_at"], session["start_at"]),
        ).fetchone()
        if overlap:
            raise ValueError("恢复后会与现有电脑会话重叠，请先处理冲突记录")
    conn.execute(
        f"UPDATE {table} SET deleted_at=NULL,deleted_by=NULL,delete_reason=NULL,purge_after=NULL "
        "WHERE id=?", (record_id,),
    )
    if entity_type in ("checkin", "mission"):
        source_types = "('checkin','legacy_checkin')" if entity_type == "checkin" else "('mission')"
        conn.execute(
            f"UPDATE point_ledger SET deleted_at=NULL,deleted_by=NULL,delete_reason=NULL,purge_after=NULL "
            f"WHERE source_type IN {source_types} AND source_id=?", (str(record_id),),
        )
    conn.execute(
        "UPDATE record_deletions SET restored_at=?,restored_by=? WHERE id=?",
        (_iso(now), actor, deletion_id),
    )
    conn.commit()
    lifecycle.ensure_state(conn, now)


def purge_expired(conn, photo_dir, now):
    rows = conn.execute(
        "SELECT * FROM record_deletions WHERE restored_at IS NULL AND purged_at IS NULL "
        "AND purge_after<=?", (_iso(now),),
    ).fetchall()
    for deletion in rows:
        entity_type, record_id = deletion["entity_type"], deletion["entity_id"]
        photo_paths = []
        if entity_type == "checkin":
            row = conn.execute("SELECT photo_path FROM checkins WHERE id=?", (record_id,)).fetchone()
            if row and row["photo_path"]:
                photo_paths.append(row["photo_path"])
            conn.execute(
                "UPDATE checkins SET title=NULL,note=NULL,mood=NULL,photo_path=NULL WHERE id=?",
                (record_id,),
            )
            conn.execute("DELETE FROM comments WHERE checkin_id=?", (record_id,))
            conn.execute("DELETE FROM reactions WHERE checkin_id=?", (record_id,))
        elif entity_type == "mission":
            row = conn.execute("SELECT photo_path FROM missions WHERE id=?", (record_id,)).fetchone()
            if row and row["photo_path"]:
                photo_paths.append(row["photo_path"])
            photo_paths.extend(r["photo_path"] for r in conn.execute(
                "SELECT photo_path FROM mission_submissions WHERE mission_id=? AND photo_path IS NOT NULL",
                (record_id,),
            ).fetchall())
            conn.execute(
                "UPDATE missions SET description=NULL,submission_note=NULL,photo_path=NULL,"
                "rejection_reason=NULL WHERE id=?", (record_id,),
            )
            conn.execute(
                "UPDATE mission_submissions SET note=NULL,photo_path=NULL,reason=NULL WHERE mission_id=?",
                (record_id,),
            )
        elif entity_type == "computer_session":
            conn.execute("UPDATE computer_sessions SET note=NULL WHERE id=?", (record_id,))
        for photo_path in photo_paths:
            name = os.path.basename(photo_path)
            target = os.path.join(photo_dir, name)
            if name and os.path.isfile(target):
                os.remove(target)
        conn.execute("UPDATE record_deletions SET purged_at=? WHERE id=?", (_iso(now), deletion["id"]))
    conn.commit()
    return len(rows)
