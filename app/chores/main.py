import os
import datetime as dt
import json
import uuid
import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.concurrency import run_in_threadpool

from chores.config import Config
from chores import db as dbmod
from chores import auth, scoring, lifecycle, records, weekly
from chores.images import save_photo, PhotoError

BASE = os.path.dirname(__file__)
_TZ = ZoneInfo("Asia/Shanghai")
CHECKIN_NAME = "浩哥"


def _now() -> dt.datetime:
    """北京时间（naive，与库内存储格式一致）。"""
    return dt.datetime.now(_TZ).replace(tzinfo=None)


def create_app() -> FastAPI:
    cfg = Config.from_env()
    conn = dbmod.connect(cfg.db_path)
    dbmod.init_schema(conn)
    dbmod.migrate(conn)
    dbmod.seed_defaults(conn)
    weekly.migrate_active(conn, _now())
    dbmod.ensure_open_period(conn, _now().isoformat(timespec="seconds"))
    lifecycle.ensure_state(conn, _now())
    os.makedirs(cfg.photo_dir, exist_ok=True)
    records.purge_expired(conn, cfg.photo_dir, _now())

    state_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(application):
        async def clock():
            while True:
                await asyncio.sleep(30)
                async with state_lock:
                    try:
                        lifecycle.ensure_state(conn, _now())
                    except Exception:
                        conn.rollback()
                        logging.getLogger(__name__).exception("自动周期处理失败，下次重试")
        task = asyncio.create_task(clock())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(SessionMiddleware, secret_key=cfg.secret_key)
    app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
    app.mount("/photos", StaticFiles(directory=cfg.photo_dir), name="photos")
    templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))

    app.state.cfg = cfg
    app.state.conn = conn
    app.state.templates = templates

    templates.env.globals["weekly_status"] = lambda: weekly.summary(
        conn, lifecycle.active_stage(conn, _now()), _now(),
    )
    templates.env.filters["duration"] = lambda minutes: (
        f"{int(minutes) // 60} 小时 {int(minutes) % 60} 分" if int(minutes) % 60
        else f"{int(minutes) // 60} 小时"
    )

    @app.middleware("http")
    async def keep_settlements_current(request: Request, call_next):
        async with state_lock:
            try:
                now = _now()
                records.purge_expired(conn, cfg.photo_dir, now)
                lifecycle.ensure_state(conn, now)
                _expire_exchange_requests(now)
                _expire_missions(now.isoformat(timespec="seconds"))
                return await call_next(request)
            except Exception:
                conn.rollback()
                raise

    def _goal() -> int:
        v = dbmod.get_setting(conn, "period_goal", str(cfg.weekly_goal))
        try:
            return int(v)
        except (TypeError, ValueError):
            return cfg.weekly_goal

    def _period_rows(pid: int):
        rows = conn.execute(
            "SELECT c.*, t.name AS task_name FROM checkins c "
            "LEFT JOIN task_catalog t ON c.task_id=t.id WHERE c.period_id=? "
            "AND c.deleted_at IS NULL",
            (pid,),
        ).fetchall()
        return [dict(r) for r in rows]

    def _sync_points(delta, entry_type, description, source_type, source_id, actor, now):
        lifecycle.add_points(
            conn, delta, entry_type, description, source_type, source_id, actor,
            now.isoformat(timespec="seconds"),
        )
        conn.commit()
        lifecycle.ensure_state(conn, now)

    def _expire_exchange_requests(now: dt.datetime):
        active = lifecycle.active_stage(conn, now)
        changed = False
        for row in conn.execute(
            "SELECT r.id,r.logical_date,r.stage_id,s.cutoff_hour "
            "FROM time_exchange_requests r JOIN stages s ON s.id=r.stage_id "
            "WHERE r.status='pending'"
        ).fetchall():
            cutoff = active["cutoff_hour"] if active and active["id"] == row["stage_id"] else row["cutoff_hour"]
            current_day = lifecycle.logical_date(now, int(cutoff)).isoformat()
            if current_day != row["logical_date"] or not active or active["id"] != row["stage_id"]:
                conn.execute(
                    "UPDATE time_exchange_requests SET status='expired',reviewed_at=?,"
                    "review_reason='逻辑日或阶段已经结束' WHERE id=? AND status='pending'",
                    (now.isoformat(timespec="seconds"), row["id"]),
                )
                changed = True
        if changed:
            conn.commit()

    def _pending_summary():
        checkins = conn.execute(
            "SELECT COUNT(*) AS n FROM checkins WHERE status='pending' AND deleted_at IS NULL"
        ).fetchone()["n"]
        missions = conn.execute(
            "SELECT COUNT(*) AS n FROM missions WHERE status='submitted' AND deleted_at IS NULL"
        ).fetchone()["n"]
        exchanges = conn.execute(
            "SELECT COUNT(*) AS n FROM time_exchange_requests WHERE status='pending'"
        ).fetchone()["n"]
        return {
            "checkin": int(checkins), "mission": int(missions),
            "time_exchange": int(exchanges),
            "total": int(checkins) + int(missions) + int(exchanges),
        }

    templates.env.globals["pending_counts"] = _pending_summary

    async def _save_upload(photo):
        if photo is None:
            return None
        raw = await photo.read()
        if not raw:
            return None
        return await run_in_threadpool(save_photo, raw, cfg.photo_dir)

    async def _save_photo_choice(photo_camera, photo_album, legacy_photo=None):
        choices = [p for p in (photo_camera, photo_album, legacy_photo) if p and p.filename]
        if len(choices) > 1:
            raise PhotoError("请只选择现场拍照或相册照片中的一种")
        return await _save_upload(choices[0] if choices else None)

    def _build_feed(rows):
        """动态只展示打卡（排除监管者的 adjustment 调整记录）。"""
        feed = sorted(
            (r for r in rows if r["kind"] != "adjustment"),
            key=lambda r: r["created_at"],
            reverse=True,
        )[:50]
        for f in feed:
            f["comments"] = [
                dict(x) for x in conn.execute(
                    "SELECT author_name, author_role, body, created_at "
                    "FROM comments WHERE checkin_id=? ORDER BY id", (f["id"],)
                ).fetchall()
            ]
            f["reactions"] = [
                dict(x) for x in conn.execute(
                    "SELECT author_name, kind FROM reactions WHERE checkin_id=?",
                    (f["id"],)
                ).fetchall()
            ]
        return feed

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, error: str = ""):
        return templates.TemplateResponse(
            request, "login.html", {"request": request, "error": error}
        )

    @app.post("/login")
    def login(request: Request, password: str = Form(...)):
        role = auth.verify_password(
            password, cfg.supervisor_password, cfg.checkin_password
        )
        if role is None:
            return RedirectResponse("/login?error=口令错误", status_code=303)
        request.session["role"] = role
        if role == auth.ROLE_SUPERVISOR:
            return RedirectResponse("/whoami", status_code=303)
        request.session["name"] = CHECKIN_NAME
        return RedirectResponse("/", status_code=303)

    @app.get("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/whoami", response_class=HTMLResponse)
    def whoami_page(request: Request):
        if auth.current_role(request) != auth.ROLE_SUPERVISOR:
            return RedirectResponse("/login", status_code=303)
        sups = conn.execute(
            "SELECT name FROM supervisors WHERE active=1 ORDER BY id"
        ).fetchall()
        return templates.TemplateResponse(
            request, "whoami.html", {"request": request, "supervisors": sups}
        )

    @app.post("/whoami")
    def whoami_set(request: Request, name: str = Form(...)):
        if auth.current_role(request) != auth.ROLE_SUPERVISOR:
            return RedirectResponse("/login", status_code=303)
        request.session["name"] = name
        return RedirectResponse("/", status_code=303)

    @app.get("/checkin", response_class=HTMLResponse)
    def checkin_form(request: Request, error: str = ""):
        if auth.current_role(request) != auth.ROLE_CHECKIN:
            return RedirectResponse("/", status_code=303)
        tasks = conn.execute(
            "SELECT id, name, default_points FROM task_catalog "
            "WHERE active=1 ORDER BY sort_order, id"
        ).fetchall()
        return templates.TemplateResponse(
            request, "checkin_form.html", {"request": request, "tasks": tasks, "error": error}
        )

    @app.post("/checkin")
    async def checkin_submit(
        request: Request,
        kind: str = Form(...),
        task_id: str = Form(""),
        title: str = Form(""),
        proposed_points: str = Form(""),
        note: str = Form(""),
        mood: str = Form(""),
        photo_camera: UploadFile = File(None),
        photo_album: UploadFile = File(None),
        photo: UploadFile = File(None),
    ):
        if auth.current_role(request) != auth.ROLE_CHECKIN:
            return RedirectResponse("/", status_code=303)
        try:
            photo_path = await _save_photo_choice(photo_camera, photo_album, photo)
        except PhotoError as exc:
            tasks = conn.execute(
                "SELECT id, name, default_points FROM task_catalog "
                "WHERE active=1 ORDER BY sort_order, id"
            ).fetchall()
            return templates.TemplateResponse(
                request, "checkin_form.html",
                {"request": request, "tasks": tasks, "error": str(exc)},
                status_code=400,
            )
        now_dt = _now()
        now = now_dt.isoformat(timespec="seconds")
        pid = dbmod.ensure_open_period(conn, now)
        if kind == "fixed":
            t = conn.execute(
                "SELECT name, default_points FROM task_catalog WHERE id=? AND active=1",
                (task_id,),
            ).fetchone()
            if t is None:
                return RedirectResponse("/checkin?error=请选择有效任务", status_code=303)
            pts = scoring.award_for_fixed(t)
            cid = conn.execute(
                "INSERT INTO checkins (kind, task_id, period_id, photo_path, note,"
                " mood, awarded_points, status, created_at) VALUES "
                "(?,?,?,?,?,?,?,'scored',?)",
                (kind, int(task_id), pid, photo_path, note, mood, pts, now),
            ).lastrowid
            conn.commit()
            _sync_points(pts, "earn", t["name"], "checkin", cid,
                         auth.current_name(request) or CHECKIN_NAME, now_dt)
        else:
            if not title.strip():
                return RedirectResponse("/checkin?error=请填写做了什么", status_code=303)
            try:
                prop = int(proposed_points) if proposed_points.strip() else None
            except ValueError:
                return RedirectResponse("/checkin?error=建议分必须是整数", status_code=303)
            conn.execute(
                "INSERT INTO checkins (kind, period_id, title, photo_path, note,"
                " mood, proposed_points, status, created_at) VALUES "
                "(?,?,?,?,?,?,?,'pending',?)",
                ("adhoc", pid, title, photo_path, note, mood, prop, now),
            )
            conn.commit()
        return RedirectResponse("/activity", status_code=303)

    @app.post("/computer/on")
    def computer_on(request: Request, return_to: str = Form("/computer")):
        if auth.current_role(request) != auth.ROLE_CHECKIN:
            return RedirectResponse("/login", status_code=303)
        open_row = conn.execute(
            "SELECT id FROM computer_sessions WHERE end_at IS NULL AND deleted_at IS NULL"
        ).fetchone()
        if open_row is None:
            conn.execute(
                "INSERT INTO computer_sessions (start_at,started_by,source,updated_at) "
                "VALUES (?,?, 'web', ?)",
                (_now().isoformat(timespec="seconds"),
                 auth.current_name(request) or CHECKIN_NAME,
                 _now().isoformat(timespec="seconds")),
            )
            conn.commit()
        return RedirectResponse("/" if return_to == "/" else "/computer", status_code=303)

    @app.post("/computer/off")
    def computer_off(request: Request, return_to: str = Form("/computer")):
        if auth.current_role(request) != auth.ROLE_CHECKIN:
            return RedirectResponse("/login", status_code=303)
        open_row = conn.execute(
            "SELECT id FROM computer_sessions WHERE end_at IS NULL AND deleted_at IS NULL "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if open_row is not None:
            conn.execute(
                "UPDATE computer_sessions SET end_at=?, ended_by=?, updated_at=? WHERE id=?",
                (_now().isoformat(timespec="seconds"),
                 auth.current_name(request) or CHECKIN_NAME,
                 _now().isoformat(timespec="seconds"), open_row["id"]),
            )
            conn.commit()
        return RedirectResponse("/" if return_to == "/" else "/computer", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        if auth.current_role(request) is None:
            return RedirectResponse("/login", status_code=303)
        now = _now()
        today = now.date()
        stage, window = lifecycle.ensure_state(conn, now)
        week = weekly.summary(conn, stage, now)
        period = dbmod.current_period(conn)
        if period is None:
            dbmod.ensure_open_period(conn, now.isoformat(timespec="seconds"))
            period = dbmod.current_period(conn)
        rows = _period_rows(period["id"])
        cutoff = int(stage["cutoff_hour"]) if stage else 4
        computer = lifecycle.computer_summary(conn, now, cutoff)
        points = lifecycle.point_summary(conn, stage, window)
        ann = conn.execute(
            "SELECT body FROM announcements WHERE active=1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.execute(
            "UPDATE missions SET status='expired' WHERE status='open' "
            "AND deadline_at IS NOT NULL AND deadline_at!='' AND deadline_at<?",
            (now.isoformat(timespec="seconds"),),
        )
        conn.commit()
        logical_today = lifecycle.logical_date(now, cutoff)
        day_start = dt.datetime.combine(logical_today, dt.time(hour=cutoff))
        day_end = day_start + dt.timedelta(days=1)
        today_checkins = conn.execute(
            "SELECT COUNT(*) AS n FROM checkins WHERE deleted_at IS NULL AND kind!='adjustment' "
            "AND created_at>=? AND created_at<?", (day_start.isoformat(), day_end.isoformat()),
        ).fetchone()["n"]
        active_missions = [dict(r) for r in conn.execute(
            "SELECT * FROM missions WHERE deleted_at IS NULL AND status IN ('open','claimed','submitted') "
            "ORDER BY CASE status WHEN 'submitted' THEN 0 WHEN 'claimed' THEN 1 ELSE 2 END,id DESC LIMIT 3"
        ).fetchall()]
        total = scoring.period_total(rows)
        goal = _goal()
        ctx = {
            "request": request,
            "role": auth.current_role(request),
            "name": auth.current_name(request),
            "period_seq": period["seq"],
            "period_started": period["started_at"][:10],
            "period_total": total,
            "today_total": scoring.today_total(rows, today),
            "progress": int(scoring.progress_ratio(total, goal) * 100),
            "goal": goal,
            "comp_min": computer["used"],
            "comp_seg": len(computer["sessions"]),
            "computer": computer,
            "points": points,
            "stage": dict(stage) if stage else None,
            "window": dict(window) if window else None,
            "week": week,
            "announcement": ann["body"] if ann else "",
            "today_checkins": int(today_checkins),
            "active_missions": active_missions,
            "pending": _pending_summary(),
        }
        return templates.TemplateResponse(request, "dashboard.html", ctx)

    @app.get("/activity", response_class=HTMLResponse)
    def activity_page(request: Request, feed_date: str = "", kind: str = "all",
                      error: str = "", cycle_id: str = ""):
        if auth.current_role(request) is None:
            return RedirectResponse("/login", status_code=303)
        now = _now()
        stage, _ = lifecycle.ensure_state(conn, now)
        cutoff = int(stage["cutoff_hour"]) if stage else 4
        logical_today = lifecycle.logical_date(now, cutoff)
        first = conn.execute(
            "SELECT MIN(first_at) AS first_at FROM ("
            "SELECT MIN(created_at) AS first_at FROM checkins WHERE deleted_at IS NULL AND kind!='adjustment' "
            "UNION ALL SELECT MIN(created_at) FROM missions WHERE deleted_at IS NULL)"
        ).fetchone()["first_at"]
        earliest = lifecycle.logical_date(dt.datetime.fromisoformat(first), cutoff) if first else logical_today
        minimum = dt.date.fromisoformat(stage["starts_on"]) if stage else earliest
        maximum = min(logical_today, dt.date.fromisoformat(stage["ends_on"]) if stage else logical_today)
        if minimum > maximum:
            minimum = maximum
        try:
            selected = dt.date.fromisoformat(feed_date) if feed_date else logical_today
        except ValueError:
            selected = logical_today
        selected = min(max(selected, minimum), maximum)
        start = dt.datetime.combine(selected, dt.time(hour=cutoff))
        end = start + dt.timedelta(days=1)
        selected_cycle = conn.execute("SELECT * FROM weekly_cycles WHERE id=?", (cycle_id,)).fetchone() if cycle_id else None
        if selected_cycle:
            start = dt.datetime.fromisoformat(selected_cycle["start_at"])
            end = dt.datetime.fromisoformat(selected_cycle["end_at"])
        start_iso, end_iso = start.isoformat(), end.isoformat()

        items = []
        if kind in ("all", "checkin"):
            rows = [dict(r) for r in conn.execute(
                "SELECT c.*,t.name AS task_name FROM checkins c LEFT JOIN task_catalog t ON t.id=c.task_id "
                "WHERE c.deleted_at IS NULL AND c.kind!='adjustment' AND c.created_at>=? AND c.created_at<?",
                (start_iso, end_iso),
            ).fetchall()]
            for row in _build_feed(rows):
                items.append({"item_type": "checkin", "occurred_at": row["created_at"], "checkin": row})
        if kind in ("all", "mission"):
            mission_rows = conn.execute(
                "SELECT * FROM missions WHERE deleted_at IS NULL AND ("
                "(created_at>=? AND created_at<?) OR (claimed_at>=? AND claimed_at<?))",
                (start_iso, end_iso, start_iso, end_iso),
            ).fetchall()
            for row in mission_rows:
                mission = dict(row)
                mission["mission_id"] = mission["id"]
                if start_iso <= mission["created_at"] < end_iso:
                    items.append({"item_type": "mission", "event_type": "published",
                                  "occurred_at": mission["created_at"], "mission": mission})
                if mission["claimed_at"] and start_iso <= mission["claimed_at"] < end_iso:
                    items.append({"item_type": "mission", "event_type": "claimed",
                                  "occurred_at": mission["claimed_at"], "mission": mission})
            submissions = conn.execute(
                "SELECT s.*,m.title,m.points,m.awarded_points,m.deleted_at FROM mission_submissions s "
                "JOIN missions m ON m.id=s.mission_id WHERE m.deleted_at IS NULL AND ("
                "(s.submitted_at>=? AND s.submitted_at<?) OR (s.reviewed_at>=? AND s.reviewed_at<?))",
                (start_iso, end_iso, start_iso, end_iso),
            ).fetchall()
            for row in submissions:
                mission = dict(row)
                if start_iso <= mission["submitted_at"] < end_iso:
                    items.append({"item_type": "mission", "event_type": "submitted",
                                  "occurred_at": mission["submitted_at"], "mission": mission})
                if mission["reviewed_at"] and start_iso <= mission["reviewed_at"] < end_iso:
                    items.append({"item_type": "mission",
                                  "event_type": mission["decision"] or "reviewed",
                                  "occurred_at": mission["reviewed_at"], "mission": mission})
        # 同一任务在一个逻辑日只占一张卡片；卡内展示当天发生过的状态变化，
        # 主状态使用当天最后一次变化，避免“领取/提交/审核”重复刷屏。
        grouped_missions = {}
        checkin_items = []
        for item in items:
            if item["item_type"] == "checkin":
                checkin_items.append(item)
                continue
            mission_id = item["mission"].get("mission_id") or item["mission"].get("id")
            grouped = grouped_missions.get(mission_id)
            if grouped is None:
                grouped = dict(item)
                grouped["today_events"] = []
                grouped_missions[mission_id] = grouped
            if item["event_type"] not in grouped["today_events"]:
                grouped["today_events"].append(item["event_type"])
            if item["occurred_at"] >= grouped["occurred_at"]:
                grouped["occurred_at"] = item["occurred_at"]
                grouped["event_type"] = item["event_type"]
        for mission_id, grouped in grouped_missions.items():
            current = conn.execute(
                "SELECT * FROM missions WHERE id=? AND deleted_at IS NULL", (mission_id,)
            ).fetchone()
            if current:
                mission = dict(current)
                progress = ["published"]
                if mission["claimed_at"]:
                    progress.append("claimed")
                if mission["submitted_at"]:
                    progress.append("submitted")
                if mission["status"] == "completed":
                    progress.append("approved")
                elif mission["rejection_reason"]:
                    progress.append("rejected")
                grouped["mission"] = mission
                grouped["progress"] = progress
        items = checkin_items + list(grouped_missions.values())
        items.sort(key=lambda item: item["occurred_at"], reverse=True)
        active = conn.execute(
            "SELECT * FROM missions WHERE deleted_at IS NULL AND status IN ('open','claimed') "
            "ORDER BY CASE status WHEN 'claimed' THEN 0 ELSE 1 END,id DESC"
        ).fetchall()
        return templates.TemplateResponse(request, "activity.html", {
            "request": request, "role": auth.current_role(request), "items": items,
            "missions": active, "kind": kind if kind in ("all", "checkin", "mission") else "all",
            "error": error, "feed_date": selected.isoformat(),
            "feed_today": logical_today.isoformat(), "feed_min": minimum.isoformat(),
            "feed_max": maximum.isoformat(),
            "feed_prev": (selected - dt.timedelta(days=1)).isoformat() if selected > minimum else None,
            "feed_next": (selected + dt.timedelta(days=1)).isoformat() if selected < maximum else None,
            "cycles": conn.execute("SELECT * FROM weekly_cycles ORDER BY start_at DESC,id DESC LIMIT 52").fetchall(),
            "selected_cycle": selected_cycle,
        })

    @app.get("/history", response_class=HTMLResponse)
    def history_list(request: Request):
        if auth.current_role(request) is None:
            return RedirectResponse("/login", status_code=303)
        periods = conn.execute(
            "SELECT * FROM periods WHERE ended_at IS NOT NULL ORDER BY id DESC"
        ).fetchall()
        items = []
        for p in periods:
            agg = conn.execute(
                "SELECT COALESCE(SUM(awarded_points),0) AS total,"
                " SUM(CASE WHEN kind!='adjustment' THEN 1 ELSE 0 END) AS n "
                "FROM checkins WHERE period_id=? AND status='scored' AND deleted_at IS NULL", (p["id"],)
            ).fetchone()
            items.append({
                "id": p["id"], "seq": p["seq"],
                "started": p["started_at"][:10], "ended": p["ended_at"][:10],
                "total": agg["total"], "n": agg["n"] or 0,
            })
        return templates.TemplateResponse(
            request, "history.html", {"request": request, "items": items,
                "cycles": conn.execute("SELECT * FROM weekly_cycles ORDER BY start_at DESC,id DESC").fetchall()}
        )

    @app.get("/history/{pid}", response_class=HTMLResponse)
    def history_detail(request: Request, pid: int):
        if auth.current_role(request) is None:
            return RedirectResponse("/login", status_code=303)
        period = conn.execute(
            "SELECT * FROM periods WHERE id=?", (pid,)
        ).fetchone()
        if period is None:
            return RedirectResponse("/history", status_code=303)
        rows = _period_rows(pid)
        ctx = {
            "request": request,
            "period_seq": period["seq"],
            "period_started": period["started_at"][:10],
            "period_ended": period["ended_at"][:10] if period["ended_at"] else "",
            "period_total": scoring.period_total(rows),
            "feed": _build_feed(rows),
        }
        return templates.TemplateResponse(request, "history_detail.html", ctx)

    @app.get("/cycles/{cycle_id}", response_class=HTMLResponse)
    def cycle_detail(request: Request, cycle_id: int):
        if auth.current_role(request) is None:
            return RedirectResponse("/login", status_code=303)
        cycle = conn.execute("SELECT * FROM weekly_cycles WHERE id=?", (cycle_id,)).fetchone()
        if cycle is None:
            return RedirectResponse("/history", status_code=303)
        stage = conn.execute("SELECT * FROM stages WHERE id=?", (cycle["stage_id"],)).fetchone()
        snapshot = weekly.summary(conn, stage, _now())
        if not snapshot or not snapshot.get("cycle") or snapshot["cycle"]["id"] != cycle_id:
            import json
            rules = json.loads(cycle["rules_json"])
            executions = {row["rule_id"]: dict(row) for row in conn.execute(
                "SELECT * FROM rule_executions WHERE cycle_id=?", (cycle_id,),
            )}
            snapshot = {"cycle": dict(cycle), "points": int(cycle["points_at_close"] or 0),
                        "rules": [{**rule, "execution": executions.get(rule["id"], {}), "today": False,
                                   "remaining": 0} for rule in rules],
                        "events": conn.execute("SELECT * FROM cycle_events WHERE cycle_id=? ORDER BY id DESC",
                                               (cycle_id,)).fetchall(), "auto_renew": False}
        return templates.TemplateResponse(request, "cycle_detail.html", {
            "request": request, "week": snapshot, "stage": stage,
        })

    @app.get("/archive", response_class=HTMLResponse)
    def archive_confirm(request: Request):
        if auth.current_role(request) != auth.ROLE_SUPERVISOR:
            return RedirectResponse("/login", status_code=303)
        period = dbmod.current_period(conn)
        rows = _period_rows(period["id"]) if period else []
        n = len([r for r in rows if r["kind"] != "adjustment"])
        ctx = {
            "request": request,
            "period_seq": period["seq"] if period else 0,
            "period_started": period["started_at"][:10] if period else "",
            "period_total": scoring.period_total(rows),
            "checkin_n": n,
        }
        return templates.TemplateResponse(request, "archive_confirm.html", ctx)

    @app.post("/archive")
    def archive_do(request: Request):
        if auth.current_role(request) != auth.ROLE_SUPERVISOR:
            return RedirectResponse("/login", status_code=303)
        dbmod.archive_period(conn, _now().isoformat(timespec="seconds"))
        return RedirectResponse("/", status_code=303)

    def _author(request: Request):
        role = auth.current_role(request)
        name = auth.current_name(request) or (
            CHECKIN_NAME if role == auth.ROLE_CHECKIN else "监管者"
        )
        return role, name

    @app.post("/checkin/{cid}/comment")
    def add_comment(request: Request, cid: int, body: str = Form(...)):
        if auth.current_role(request) is None:
            return RedirectResponse("/login", status_code=303)
        role, name = _author(request)
        target = conn.execute(
            "SELECT 1 FROM checkins WHERE id=? AND deleted_at IS NULL", (cid,),
        ).fetchone()
        if body.strip() and target:
            conn.execute(
                "INSERT INTO comments (checkin_id, author_name, author_role, body,"
                " created_at) VALUES (?,?,?,?,?)",
                (cid, name, role, body.strip(), _now().isoformat(timespec="seconds")),
            )
            conn.commit()
        return RedirectResponse("/activity", status_code=303)

    @app.post("/checkin/{cid}/react")
    def add_react(request: Request, cid: int, kind: str = Form(...)):
        if auth.current_role(request) is None:
            return RedirectResponse("/login", status_code=303)
        role, name = _author(request)
        target = conn.execute(
            "SELECT 1 FROM checkins WHERE id=? AND deleted_at IS NULL", (cid,),
        ).fetchone()
        if not target:
            return RedirectResponse("/activity", status_code=303)
        conn.execute(
            "INSERT INTO reactions (checkin_id, author_name, kind, created_at)"
            " VALUES (?,?,?,?)",
            (cid, name, kind, _now().isoformat(timespec="seconds")),
        )
        conn.commit()
        return RedirectResponse("/activity", status_code=303)

    @app.get("/review")
    def review_list(request: Request):
        if auth.current_role(request) != auth.ROLE_SUPERVISOR:
            return RedirectResponse("/login", status_code=303)
        return RedirectResponse("/admin/reviews?kind=checkin", status_code=303)

    @app.post("/review/{cid}")
    def review_act(
        request: Request, cid: int,
        action: str = Form(...), points: str = Form("0")
    ):
        if auth.current_role(request) != auth.ROLE_SUPERVISOR:
            return RedirectResponse("/login", status_code=303)
        pending_row = conn.execute(
            "SELECT * FROM checkins WHERE id=? AND status='pending' AND deleted_at IS NULL", (cid,)
        ).fetchone()
        if pending_row is None:
            return RedirectResponse(
                "/admin/reviews?kind=checkin&message=这条打卡已经处理", status_code=303,
            )
        reviewer = auth.current_name(request) or "监管者"
        now = _now().isoformat(timespec="seconds")
        if action == "approve":
            try:
                awarded = int(points or 0)
            except ValueError:
                return RedirectResponse("/admin/reviews?kind=checkin", status_code=303)
            conn.execute(
                "UPDATE checkins SET status='scored', awarded_points=?, "
                "reviewed_by=?, reviewed_at=? WHERE id=?",
                (awarded, reviewer, now, cid),
            )
        else:
            conn.execute(
                "UPDATE checkins SET status='rejected', awarded_points=0, "
                "reviewed_by=?, reviewed_at=? WHERE id=?",
                (reviewer, now, cid),
            )
        conn.commit()
        if action == "approve":
            row = conn.execute("SELECT title FROM checkins WHERE id=?", (cid,)).fetchone()
            _sync_points(awarded, "earn", row["title"] or "清单外打卡",
                         "checkin", cid, reviewer, _now())
        return RedirectResponse("/admin/reviews?kind=checkin", status_code=303)

    @app.post("/adjust")
    def adjust(request: Request, points: str = Form(...), reason: str = Form("")):
        if auth.current_role(request) != auth.ROLE_SUPERVISOR:
            return RedirectResponse("/login", status_code=303)
        reviewer = auth.current_name(request) or "监管者"
        now = _now().isoformat(timespec="seconds")
        pid = dbmod.ensure_open_period(conn, now)
        try:
            delta = int(points)
        except ValueError:
            return RedirectResponse("/admin/points", status_code=303)
        cid = conn.execute(
            "INSERT INTO checkins (kind, period_id, title, note, awarded_points,"
            " status, created_at, reviewed_by, reviewed_at) VALUES "
            "('adjustment',?,?,?,?, 'scored', ?, ?, ?)",
            (pid, "分值调整", reason, delta, now, reviewer, now),
        ).lastrowid
        conn.commit()
        _sync_points(delta, "adjustment", reason.strip() or "管理者积分调整",
                     "checkin", cid, reviewer, _now())
        return RedirectResponse("/admin/points", status_code=303)

    @app.get("/admin", response_class=HTMLResponse)
    def admin_home(request: Request):
        if auth.current_role(request) != auth.ROLE_SUPERVISOR:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(request, "admin.html", {
            "request": request, "pending": _pending_summary(),
        })

    @app.get("/admin/reviews", response_class=HTMLResponse)
    def admin_reviews(request: Request, kind: str = "all", message: str = ""):
        if auth.current_role(request) != auth.ROLE_SUPERVISOR:
            return RedirectResponse("/login", status_code=303)
        allowed = kind if kind in ("all", "checkin", "mission", "time_exchange") else "all"
        return templates.TemplateResponse(request, "reviews_admin.html", {
            "request": request, "kind": allowed, "message": message,
            "pending": _pending_summary(),
            "checkins": conn.execute(
                "SELECT * FROM checkins WHERE status='pending' AND deleted_at IS NULL "
                "ORDER BY COALESCE(proposed_points,0) DESC,id"
            ).fetchall() if allowed in ("all", "checkin") else [],
            "missions": conn.execute(
                "SELECT * FROM missions WHERE status='submitted' AND deleted_at IS NULL ORDER BY submitted_at"
            ).fetchall() if allowed in ("all", "mission") else [],
            "exchanges": conn.execute(
                "SELECT r.*,s.name AS stage_name FROM time_exchange_requests r "
                "JOIN stages s ON s.id=r.stage_id WHERE r.status='pending' ORDER BY r.created_at"
            ).fetchall() if allowed in ("all", "time_exchange") else [],
        })

    @app.get("/admin/points", response_class=HTMLResponse)
    def admin_points(request: Request):
        if auth.current_role(request) != auth.ROLE_SUPERVISOR:
            return RedirectResponse("/login", status_code=303)
        stage, window = lifecycle.ensure_state(conn, _now())
        return templates.TemplateResponse(request, "points_admin.html", {
            "request": request, "summary": lifecycle.point_summary(conn, stage, window),
            "rows": conn.execute(
                "SELECT * FROM point_ledger WHERE deleted_at IS NULL ORDER BY id DESC LIMIT 50"
            ).fetchall(),
        })

    @app.get("/admin/catalog", response_class=HTMLResponse)
    def catalog_page(request: Request):
        if auth.current_role(request) != auth.ROLE_SUPERVISOR:
            return RedirectResponse("/login", status_code=303)
        tasks = conn.execute(
            "SELECT * FROM task_catalog ORDER BY sort_order, id"
        ).fetchall()
        return templates.TemplateResponse(
            request, "catalog_admin.html", {"request": request, "tasks": tasks}
        )

    @app.post("/admin/catalog")
    def catalog_edit(
        request: Request, op: str = Form(...),
        task_id: str = Form(""), name: str = Form(""),
        points: str = Form("0")
    ):
        if auth.current_role(request) != auth.ROLE_SUPERVISOR:
            return RedirectResponse("/login", status_code=303)
        if op == "add" and name.strip():
            conn.execute(
                "INSERT INTO task_catalog (name, default_points, sort_order)"
                " VALUES (?,?, (SELECT COALESCE(MAX(sort_order),0)+1 FROM task_catalog))",
                (name.strip(), int(points or 0)),
            )
        elif op == "deactivate":
            conn.execute("UPDATE task_catalog SET active=0 WHERE id=?", (task_id,))
        elif op == "activate":
            conn.execute("UPDATE task_catalog SET active=1 WHERE id=?", (task_id,))
        elif op == "update":
            conn.execute(
                "UPDATE task_catalog SET name=?, default_points=? WHERE id=?",
                (name.strip(), int(points or 0), task_id),
            )
        conn.commit()
        return RedirectResponse("/admin/catalog", status_code=303)

    @app.get("/admin/announcement", response_class=HTMLResponse)
    def announcement_page(request: Request):
        if auth.current_role(request) != auth.ROLE_SUPERVISOR:
            return RedirectResponse("/login", status_code=303)
        cur = conn.execute(
            "SELECT body FROM announcements WHERE active=1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return templates.TemplateResponse(
            request, "announcement.html",
            {
                "request": request,
                "current": cur["body"] if cur else "",
                "goal": _goal(),
            },
        )

    @app.post("/admin/announcement")
    def announcement_set(
        request: Request, body: str = Form(""), goal: str = Form("")
    ):
        if auth.current_role(request) != auth.ROLE_SUPERVISOR:
            return RedirectResponse("/login", status_code=303)
        if goal.strip().isdigit():
            dbmod.set_setting(conn, "period_goal", int(goal.strip()))
        conn.execute("UPDATE announcements SET active=0 WHERE active=1")
        conn.execute(
            "INSERT INTO announcements (body, created_at, active) VALUES (?,?,1)",
            (body.strip(), _now().isoformat(timespec="seconds")),
        )
        conn.commit()
        return RedirectResponse("/", status_code=303)

    @app.get("/points", response_class=HTMLResponse)
    def points_page(request: Request, kind: str = ""):
        if auth.current_role(request) is None:
            return RedirectResponse("/login", status_code=303)
        stage, window = lifecycle.ensure_state(conn, _now())
        if kind:
            rows = conn.execute(
            "SELECT * FROM point_ledger WHERE entry_type=? AND deleted_at IS NULL ORDER BY id DESC LIMIT 200",
                (kind,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM point_ledger WHERE deleted_at IS NULL ORDER BY id DESC LIMIT 200"
            ).fetchall()
        return templates.TemplateResponse(request, "points.html", {
            "request": request, "rows": rows,
            "summary": lifecycle.point_summary(conn, stage, window), "kind": kind,
        })

    def _expire_missions(now_iso):
        conn.execute(
            "UPDATE missions SET status='expired' WHERE status='open' "
            "AND deadline_at IS NOT NULL AND deadline_at!='' AND deadline_at<?",
            (now_iso,),
        )
        conn.commit()

    @app.get("/missions")
    def missions_page(request: Request):
        if auth.current_role(request) is None:
            return RedirectResponse("/login", status_code=303)
        return RedirectResponse("/activity#missions", status_code=303)

    @app.post("/missions/{mid}/claim")
    def mission_claim(request: Request, mid: int):
        if auth.current_role(request) != auth.ROLE_CHECKIN:
            return RedirectResponse("/login", status_code=303)
        now = _now().isoformat(timespec="seconds")
        _expire_missions(now)
        conn.execute(
            "UPDATE missions SET status='claimed', claimed_at=? WHERE id=? AND status='open'",
            (now, mid),
        )
        conn.commit()
        return RedirectResponse("/activity#missions", status_code=303)

    @app.post("/missions/{mid}/submit")
    async def mission_submit(
        request: Request, mid: int, note: str = Form(""),
        photo_camera: UploadFile = File(None), photo_album: UploadFile = File(None),
        photo: UploadFile = File(None),
    ):
        if auth.current_role(request) != auth.ROLE_CHECKIN:
            return RedirectResponse("/login", status_code=303)
        mission = conn.execute(
            "SELECT * FROM missions WHERE id=? AND status='claimed' AND deleted_at IS NULL", (mid,)
        ).fetchone()
        if mission is None:
            return RedirectResponse("/activity#missions", status_code=303)
        try:
            photo_path = await _save_photo_choice(photo_camera, photo_album, photo)
        except PhotoError:
            return RedirectResponse("/activity?error=照片格式不支持或文件过大#missions", status_code=303)
        if mission["photo_required"] and not photo_path:
            return RedirectResponse("/activity?error=这个任务需要完成照片#missions", status_code=303)
        now = _now().isoformat(timespec="seconds")
        conn.execute(
            "UPDATE missions SET status='submitted', submitted_at=?, submission_note=?, "
            "photo_path=?, rejection_reason=NULL WHERE id=? AND status='claimed'",
            (now, note.strip(), photo_path, mid),
        )
        conn.execute(
            "INSERT INTO mission_submissions (mission_id,note,photo_path,submitted_at) "
            "VALUES (?,?,?,?)", (mid, note.strip(), photo_path, now),
        )
        conn.commit()
        return RedirectResponse("/activity#missions", status_code=303)

    @app.get("/admin/missions", response_class=HTMLResponse)
    def admin_missions(request: Request, error: str = ""):
        if auth.current_role(request) != auth.ROLE_SUPERVISOR:
            return RedirectResponse("/login", status_code=303)
        rows = conn.execute(
            "SELECT * FROM missions WHERE deleted_at IS NULL ORDER BY id DESC LIMIT 200"
        ).fetchall()
        return templates.TemplateResponse(request, "missions_admin.html", {
            "request": request, "missions": rows, "error": error,
        })

    @app.post("/admin/missions")
    def admin_mission_create(
        request: Request, title: str = Form(...), description: str = Form(""),
        points: str = Form(...), deadline_at: str = Form(""),
        photo_required: str = Form("0"),
    ):
        if auth.current_role(request) != auth.ROLE_SUPERVISOR:
            return RedirectResponse("/login", status_code=303)
        try:
            value = int(points)
        except ValueError:
            return RedirectResponse("/admin/missions?error=积分必须是整数", status_code=303)
        if not title.strip() or value < 0:
            return RedirectResponse("/admin/missions?error=请填写有效任务和积分", status_code=303)
        now = _now().isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO missions "
            "(title,description,points,deadline_at,photo_required,published_by,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (title.strip(), description.strip(), value,
             deadline_at if deadline_at else None,
             1 if photo_required == "1" else 0,
             auth.current_name(request) or "监管者", now),
        )
        conn.commit()
        return RedirectResponse("/admin/missions", status_code=303)

    @app.post("/admin/missions/{mid}/cancel")
    def admin_mission_cancel(request: Request, mid: int):
        if auth.current_role(request) != auth.ROLE_SUPERVISOR:
            return RedirectResponse("/login", status_code=303)
        conn.execute(
            "UPDATE missions SET status='cancelled' WHERE id=? AND status IN ('open','claimed')",
            (mid,),
        )
        conn.commit()
        return RedirectResponse("/admin/missions", status_code=303)

    @app.post("/admin/missions/{mid}/edit")
    def admin_mission_edit(
        request: Request, mid: int, title: str = Form(...),
        description: str = Form(""), points: str = Form(...),
        deadline_at: str = Form(""), photo_required: str = Form("0"),
    ):
        if auth.current_role(request) != auth.ROLE_SUPERVISOR:
            return RedirectResponse("/login", status_code=303)
        try:
            value = int(points)
        except ValueError:
            return RedirectResponse("/admin/missions?error=积分必须是整数", status_code=303)
        if not title.strip() or value < 0:
            return RedirectResponse("/admin/missions?error=任务内容不合法", status_code=303)
        conn.execute(
            "UPDATE missions SET title=?,description=?,points=?,deadline_at=?,photo_required=? "
            "WHERE id=? AND status='open'",
            (title.strip(), description.strip(), value, deadline_at or None,
             1 if photo_required == "1" else 0, mid),
        )
        conn.commit()
        return RedirectResponse("/admin/missions", status_code=303)

    @app.post("/admin/missions/{mid}/review")
    def admin_mission_review(
        request: Request, mid: int, action: str = Form(...),
        points: str = Form("0"), reason: str = Form(""),
    ):
        if auth.current_role(request) != auth.ROLE_SUPERVISOR:
            return RedirectResponse("/login", status_code=303)
        mission = conn.execute(
            "SELECT * FROM missions WHERE id=? AND status='submitted' AND deleted_at IS NULL", (mid,)
        ).fetchone()
        if mission is None:
            return RedirectResponse(
                "/admin/reviews?kind=mission&message=这条任务已经处理", status_code=303,
            )
        reviewer = auth.current_name(request) or "监管者"
        now_dt = _now()
        now = now_dt.isoformat(timespec="seconds")
        submission = conn.execute(
            "SELECT id FROM mission_submissions WHERE mission_id=? ORDER BY id DESC LIMIT 1",
            (mid,),
        ).fetchone()
        if action == "approve":
            try:
                awarded = int(points)
            except ValueError:
                return RedirectResponse(
                    "/admin/reviews?kind=mission&message=积分必须是整数", status_code=303,
                )
            conn.execute(
                "UPDATE missions SET status='completed', reviewed_at=?, reviewed_by=?, "
                "awarded_points=?, rejection_reason=NULL WHERE id=? AND status='submitted'",
                (now, reviewer, awarded, mid),
            )
            if submission:
                conn.execute(
                    "UPDATE mission_submissions SET reviewed_at=?,reviewer=?,decision='approved',points=? "
                    "WHERE id=?", (now, reviewer, awarded, submission["id"]),
                )
            conn.commit()
            _sync_points(awarded, "earn", mission["title"], "mission", mid,
                         reviewer, now_dt)
        else:
            conn.execute(
                "UPDATE missions SET status='claimed', reviewed_at=?, reviewed_by=?, "
                "rejection_reason=? WHERE id=? AND status='submitted'",
                (now, reviewer, reason.strip() or "请补充后重新提交", mid),
            )
            if submission:
                conn.execute(
                    "UPDATE mission_submissions SET reviewed_at=?,reviewer=?,decision='rejected',reason=? "
                    "WHERE id=?", (now, reviewer, reason.strip(), submission["id"]),
                )
            conn.commit()
        return RedirectResponse("/admin/reviews?kind=mission", status_code=303)

    @app.get("/admin/stages", response_class=HTMLResponse)
    def stages_page(request: Request, error: str = "", stage_id: str = "", edit_day: str = ""):
        if auth.current_role(request) != auth.ROLE_SUPERVISOR:
            return RedirectResponse("/login", status_code=303)
        rows = [dict(row) for row in conn.execute(
            "SELECT s.*,t.auto_renew FROM stages s LEFT JOIN rule_templates t ON t.stage_id=s.id "
            "ORDER BY s.active DESC,s.id DESC"
        )]
        selected = next((row for row in rows if str(row["id"]) == stage_id), None)
        if selected is None:
            selected = next((row for row in rows if row["auto_renew"] is not None), None)
        rules = [dict(row) for row in conn.execute(
            "SELECT * FROM daily_rules WHERE stage_id=? ORDER BY sort_order,weekday,id",
            (selected["id"],),
        )] if selected else []
        try:
            selected_day = int(edit_day) if edit_day else 4
        except ValueError:
            selected_day = 4
        selected_day = selected_day if 0 <= selected_day <= 6 else 4
        now = _now()
        week = weekly.summary(conn, selected, now)
        visible_by_day = {}
        for rule in rules:
            existing = visible_by_day.get(rule["weekday"])
            if existing is None or (rule["active"] and not existing["active"]):
                visible_by_day[rule["weekday"]] = rule
        cycle_rules = {rule["id"]: rule for rule in week["rules"]} if week and week.get("cycle") else {}
        template_timeline = []
        for weekday in range(7):
            rule = visible_by_day.get(weekday)
            cycle_rule = cycle_rules.get(rule["id"]) if rule else None
            template_timeline.append({
                "weekday": weekday,
                "rule": rule,
                "execution": cycle_rule["execution"] if cycle_rule else None,
            })
        editing_rule = visible_by_day.get(selected_day)
        change_events = conn.execute(
            "SELECT * FROM rule_change_events WHERE stage_id=? ORDER BY id DESC LIMIT 30",
            (selected["id"],),
        ).fetchall() if selected else []
        return templates.TemplateResponse(request, "stages_admin.html", {
            "request": request, "stages": rows, "selected": selected, "daily_rules": rules,
            "error": error, "week": week, "today": now.date().isoformat(),
            "selected_day": selected_day, "editing_rule": editing_rule,
            "template_timeline": template_timeline,
            "rule_change_events": change_events,
            "switches": conn.execute(
                "SELECT x.*,o.name AS old_name,n.name AS new_name FROM stage_switches x "
                "LEFT JOIN stages o ON o.id=x.old_stage_id JOIN stages n ON n.id=x.new_stage_id "
                "ORDER BY x.id DESC LIMIT 20"
            ).fetchall(),
        })

    @app.post("/admin/weekly-templates")
    def weekly_template_edit(
        request: Request, stage_id: str = Form(""), name: str = Form(""),
        starts_on: str = Form(""), ends_on: str = Form(""), cutoff_hour: str = Form("4"),
        auto_renew: str = Form("0"), exchange_enabled: str = Form("0"),
        exchange_points_per_unit: str = Form("10"), exchange_minutes_per_unit: str = Form("30"),
        exchange_daily_limit_minutes: str = Form("60"),
    ):
        if auth.current_role(request) != auth.ROLE_SUPERVISOR:
            return RedirectResponse("/login", status_code=303)
        try:
            cutoff = int(cutoff_hour)
            start, end = dt.date.fromisoformat(starts_on), dt.date.fromisoformat(ends_on)
            exchange_points, exchange_minutes, exchange_limit = map(int, (
                exchange_points_per_unit, exchange_minutes_per_unit, exchange_daily_limit_minutes,
            ))
            if not name.strip() or start > end or not 0 <= cutoff <= 23:
                raise ValueError("名称、日期或切换小时不合法")
            if min(exchange_points, exchange_minutes) <= 0 or exchange_limit < 0:
                raise ValueError("额外兑换规则不合法")
            sid = int(stage_id) if stage_id else None
            if sid and not weekly.is_template(conn, sid):
                raise ValueError("周期模板不存在")
        except (ValueError, TypeError) as error:
            return RedirectResponse("/admin/stages?error=" + str(error), status_code=303)
        now = _now()
        with conn:
            if sid:
                conn.execute(
                    "UPDATE stages SET name=?,starts_on=?,ends_on=?,cutoff_hour=? WHERE id=?",
                    (name.strip(), starts_on, ends_on, cutoff, sid),
                )
                conn.execute("UPDATE rule_templates SET auto_renew=? WHERE stage_id=?",
                             (int(auto_renew == "1"), sid))
            else:
                sid = conn.execute(
                    "INSERT INTO stages(name,starts_on,ends_on,mode,cutoff_hour,points_goal,"
                    "basic_minutes,reward_minutes,active,created_at,created_by) "
                    "VALUES (?,?,?,'rules',?,0,0,0,0,?,?)",
                    (name.strip(), starts_on, ends_on, cutoff, now.isoformat(timespec="seconds"),
                     auth.current_name(request) or "监管者"),
                ).lastrowid
                weekly.install_template(conn, sid, now, int(auto_renew == "1"))
            conn.execute(
                "UPDATE stages SET exchange_enabled=?,exchange_points_per_unit=?,"
                "exchange_minutes_per_unit=?,exchange_daily_limit_minutes=? WHERE id=?",
                (int(exchange_enabled == "1"), exchange_points, exchange_minutes, exchange_limit, sid),
            )
        return RedirectResponse(f"/admin/stages?stage_id={sid}", status_code=303)

    @app.post("/admin/daily-rules")
    def daily_rule_edit(
        request: Request, op: str = Form("add"), rule_id: str = Form(""),
        stage_id: str = Form(""), weekday: str = Form("4"),
        condition_type: str = Form("none"), points_threshold: str = Form("0"),
        reward_minutes: str = Form("0"), ends_cycle: str = Form("0"),
        effective_scope: str = Form("next_cycle"),
    ):
        if auth.current_role(request) != auth.ROLE_SUPERVISOR:
            return RedirectResponse("/login", status_code=303)
        try:
            sid, day, threshold, minutes = map(int, (stage_id, weekday, points_threshold, reward_minutes))
            rid = int(rule_id) if rule_id else None
            if not weekly.is_template(conn, sid):
                raise ValueError("周期模板不存在")
            if op not in ("add", "update", "disable", "enable", "copy", "up", "down"):
                raise ValueError("未知操作")
            if effective_scope not in ("next_cycle", "current_cycle"):
                raise ValueError("生效范围不合法")
            if not 0 <= day <= 6 or threshold < 0 or minutes < 0:
                raise ValueError("星期、积分或分钟不合法")
            if condition_type not in ("none", "week_total", "day_total"):
                raise ValueError("触发条件不合法")
            rules = [dict(row) for row in conn.execute(
                "SELECT * FROM daily_rules WHERE stage_id=? ORDER BY sort_order,id", (sid,),
            )]
            old = next((rule for rule in rules if rule["id"] == rid), None)
            if op != "add" and old is None:
                raise ValueError("规则不存在或不属于该模板")
            if effective_scope == "current_cycle" and op != "update":
                raise ValueError("本周立即采用只支持修改尚未执行的现有规则")
            if op in ("up", "down"):
                position = rules.index(old)
                target = position + (-1 if op == "up" else 1)
                if 0 <= target < len(rules):
                    rules[position], rules[target] = rules[target], rules[position]
                with conn:
                    for order, rule in enumerate(rules):
                        conn.execute("UPDATE daily_rules SET sort_order=? WHERE id=?", (order, rule["id"]))
                return RedirectResponse(f"/admin/stages?stage_id={sid}", status_code=303)
            if op in ("disable", "enable"):
                candidate = {**old, "active": int(op == "enable")}
            elif op == "copy":
                candidate = {**old, "id": None, "active": 0, "sort_order": len(rules)}
            else:
                candidate = {"id": rid, "weekday": day, "condition_type": condition_type,
                             "points_threshold": 0 if condition_type == "none" else threshold,
                             "reward_minutes": minutes, "ends_cycle": int(ends_cycle == "1"),
                             "active": old["active"] if old else 1,
                             "sort_order": old["sort_order"] if old else len(rules)}
            proposed = [rule for rule in rules if rule["id"] != candidate["id"]] + [candidate]
            weekly.validate_rules(proposed)
            now = _now()
            stamp = now.isoformat(timespec="seconds")
            values = (candidate["weekday"], candidate["condition_type"], candidate["points_threshold"],
                      candidate["reward_minutes"], candidate["ends_cycle"], candidate["active"],
                      candidate["sort_order"], stamp)
            with conn:
                if candidate["id"]:
                    conn.execute(
                        "UPDATE daily_rules SET weekday=?,condition_type=?,points_threshold=?,reward_minutes=?,"
                        "ends_cycle=?,active=?,sort_order=?,updated_at=? WHERE id=? AND stage_id=?",
                        values + (candidate["id"], sid),
                    )
                    stored_rule_id = candidate["id"]
                else:
                    stored_rule_id = conn.execute(
                        "INSERT INTO daily_rules(weekday,condition_type,points_threshold,reward_minutes,"
                        "ends_cycle,active,sort_order,updated_at,stage_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        values + (sid, stamp),
                    ).lastrowid
                if effective_scope == "current_cycle":
                    if not weekly.apply_current_cycle_rule(
                        conn, sid, stored_rule_id, old, candidate,
                        auth.current_name(request) or "监管者", now,
                    ):
                        raise ValueError("该规则本周已经执行或没有开启中的周期，不能立即采用")
                conn.execute(
                    "INSERT INTO rule_change_events(stage_id,rule_id,actor_name,effective_scope,"
                    "before_json,after_json,created_at) VALUES (?,?,?,?,?,?,?)",
                    (sid, stored_rule_id, auth.current_name(request) or "监管者", effective_scope,
                     json.dumps(old or {}, ensure_ascii=False),
                     json.dumps(candidate, ensure_ascii=False), stamp),
                )
        except (ValueError, TypeError) as error:
            return RedirectResponse("/admin/stages?error=" + str(error), status_code=303)
        return RedirectResponse(f"/admin/stages?stage_id={sid}&edit_day={candidate['weekday']}", status_code=303)

    @app.post("/admin/stages")
    def stages_edit(
        request: Request, op: str = Form("add"), stage_id: str = Form(""),
        name: str = Form(""), starts_on: str = Form(""), ends_on: str = Form(""),
        mode: str = Form("daily"), cutoff_hour: str = Form("4"),
        window_start_weekday: str = Form("4"), window_end_weekday: str = Form("0"),
        points_goal: str = Form("30"), basic_minutes: str = Form("120"),
        reward_minutes: str = Form("120"), exchange_enabled: str = Form("0"),
        exchange_points_per_unit: str = Form("10"),
        exchange_minutes_per_unit: str = Form("30"),
        exchange_daily_limit_minutes: str = Form("60"),
    ):
        if auth.current_role(request) != auth.ROLE_SUPERVISOR:
            return RedirectResponse("/login", status_code=303)
        if stage_id and conn.execute(
            "SELECT 1 FROM rule_templates WHERE stage_id=?", (stage_id,),
        ).fetchone():
            return RedirectResponse("/admin/stages?error=请使用周期模板编辑器", status_code=303)
        try:
            cutoff = int(cutoff_hour)
            start_wd = int(window_start_weekday) if mode == "weekly" else 4
            end_wd = int(window_end_weekday) if mode == "weekly" else 0
            goal_value, base_value, reward_value = map(
                int, (points_goal, basic_minutes, reward_minutes),
            )
            exchange_points, exchange_minutes, exchange_limit = map(
                int, (exchange_points_per_unit, exchange_minutes_per_unit,
                      exchange_daily_limit_minutes),
            )
            dt.date.fromisoformat(starts_on)
            dt.date.fromisoformat(ends_on)
        except (ValueError, TypeError):
            return RedirectResponse("/admin/stages?error=日期和数字格式不正确", status_code=303)
        if (not name.strip() or starts_on > ends_on or mode not in ("daily", "weekly")
                or not 0 <= cutoff <= 23 or not 0 <= start_wd <= 6 or not 0 <= end_wd <= 6
                or min(goal_value, base_value, reward_value, exchange_limit) < 0
                or exchange_points <= 0 or exchange_minutes <= 0):
            return RedirectResponse("/admin/stages?error=阶段规则不合法", status_code=303)
        payload = (name.strip(), starts_on, ends_on, mode, cutoff, start_wd,
                   end_wd, goal_value, base_value, reward_value,
                   1 if exchange_enabled == "1" else 0, exchange_points,
                   exchange_minutes, exchange_limit)
        if op == "update" and stage_id:
            conn.execute(
                "UPDATE stages SET name=?,starts_on=?,ends_on=?,mode=?,cutoff_hour=?,"
                "window_start_weekday=?,window_end_weekday=?,points_goal=?,basic_minutes=?,"
                "reward_minutes=?,exchange_enabled=?,exchange_points_per_unit=?,"
                "exchange_minutes_per_unit=?,exchange_daily_limit_minutes=? WHERE id=?",
                payload + (stage_id,),
            )
        else:
            conn.execute(
                "INSERT INTO stages (name,starts_on,ends_on,mode,cutoff_hour,"
                "window_start_weekday,window_end_weekday,points_goal,basic_minutes,"
                "reward_minutes,exchange_enabled,exchange_points_per_unit,"
                "exchange_minutes_per_unit,exchange_daily_limit_minutes,active,created_at,created_by) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
                payload + (_now().isoformat(timespec="seconds"),
                           auth.current_name(request) or "监管者"),
            )
        conn.commit()
        return RedirectResponse("/admin/stages", status_code=303)

    @app.post("/admin/stages/{stage_id}/switch")
    def stage_switch(request: Request, stage_id: int):
        if auth.current_role(request) != auth.ROLE_SUPERVISOR:
            return RedirectResponse("/login", status_code=303)
        target = conn.execute("SELECT * FROM stages WHERE id=?", (stage_id,)).fetchone()
        if not target:
            return RedirectResponse("/admin/stages?error=阶段不存在", status_code=303)
        now = _now()
        target_day = lifecycle.logical_date(now, int(target["cutoff_hour"])).isoformat()
        if not (target["starts_on"] <= target_day <= target["ends_on"]):
            return RedirectResponse(
                "/admin/stages?error=当前日期不在该阶段的生效日期内", status_code=303,
            )
        actor = auth.current_name(request) or "监管者"
        now_iso = now.isoformat(timespec="seconds")
        old = conn.execute("SELECT * FROM stages WHERE active=1 ORDER BY id DESC LIMIT 1").fetchone()
        if old and old["id"] == stage_id:
            return RedirectResponse("/admin/stages", status_code=303)
        if old:
            lifecycle.settle_open_stage(conn, old["id"], now)
            old_cycle = weekly.current_cycle(conn, old["id"], now)
            if old_cycle:
                weekly.close_cycle(conn, old_cycle, now, "手动切换阶段，封存本周")
        old_day = lifecycle.logical_date(now, int(old["cutoff_hour"]) if old else 4).isoformat()
        conn.execute(
            "UPDATE time_grants SET deleted_at=?,deleted_by=?,delete_reason='阶段切换，旧基础额度作废' "
            "WHERE source_type='stage_base' AND logical_date=? AND deleted_at IS NULL",
            (now_iso, actor, old_day),
        )
        conn.execute("UPDATE stages SET active=0")
        conn.execute("UPDATE stages SET active=1,activated_at=? WHERE id=?", (now_iso, stage_id))
        conn.execute(
            "INSERT INTO stage_switches (old_stage_id,new_stage_id,switched_by,switched_at) "
            "VALUES (?,?,?,?)", (old["id"] if old else None, stage_id, actor, now_iso),
        )
        conn.commit()
        lifecycle.ensure_state(conn, now)
        return RedirectResponse("/admin/stages", status_code=303)

    @app.post("/time-exchanges")
    def exchange_request_create(request: Request, units: str = Form("1")):
        if auth.current_role(request) != auth.ROLE_CHECKIN:
            return RedirectResponse("/login", status_code=303)
        now = _now()
        stage, window = lifecycle.ensure_state(conn, now)
        if not stage or not stage["exchange_enabled"]:
            return RedirectResponse("/computer?error=当前阶段没有开放额外时长兑换", status_code=303)
        try:
            count = int(units)
        except ValueError:
            count = 0
        if count <= 0:
            return RedirectResponse("/computer?error=请选择有效兑换份数", status_code=303)
        day = lifecycle.logical_date(now, int(stage["cutoff_hour"])).isoformat()
        pending = conn.execute(
            "SELECT 1 FROM time_exchange_requests WHERE stage_id=? AND logical_date=? "
            "AND status='pending'", (stage["id"], day),
        ).fetchone()
        if pending:
            return RedirectResponse("/computer?error=今天已有一笔兑换等待审批", status_code=303)
        point_unit = int(stage["exchange_points_per_unit"])
        minute_unit = int(stage["exchange_minutes_per_unit"])
        cost, minutes = point_unit * count, minute_unit * count
        summary = lifecycle.point_summary(conn, stage, window)
        if cost > summary["spendable"]:
            return RedirectResponse("/computer?error=可兑换积分不足", status_code=303)
        approved = conn.execute(
            "SELECT COALESCE(SUM(minutes_requested),0) AS n FROM time_exchange_requests "
            "WHERE stage_id=? AND logical_date=? AND status='approved'", (stage["id"], day),
        ).fetchone()["n"]
        limit = int(stage["exchange_daily_limit_minutes"])
        if limit and int(approved) + minutes > limit:
            return RedirectResponse("/computer?error=超过当前阶段的每日兑换上限", status_code=303)
        conn.execute(
            "INSERT INTO time_exchange_requests (stage_id,logical_date,requester_name,units,"
            "points_per_unit,minutes_per_unit,points_cost,minutes_requested,status,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,'pending',?)",
            (stage["id"], day, auth.current_name(request) or CHECKIN_NAME, count,
             point_unit, minute_unit, cost, minutes, now.isoformat(timespec="seconds")),
        )
        conn.commit()
        return RedirectResponse("/computer?message=兑换申请已提交，等待管理者审批", status_code=303)

    @app.post("/time-exchanges/{exchange_id}/cancel")
    def exchange_request_cancel(request: Request, exchange_id: int):
        if auth.current_role(request) != auth.ROLE_CHECKIN:
            return RedirectResponse("/login", status_code=303)
        conn.execute(
            "UPDATE time_exchange_requests SET status='cancelled',reviewed_at=?,"
            "review_reason='申请人取消' WHERE id=? AND status='pending' AND requester_name=?",
            (_now().isoformat(timespec="seconds"), exchange_id,
             auth.current_name(request) or CHECKIN_NAME),
        )
        conn.commit()
        return RedirectResponse("/computer?message=兑换申请已取消", status_code=303)

    @app.post("/admin/time-exchanges/{exchange_id}/review")
    def exchange_request_review(request: Request, exchange_id: int,
                                action: str = Form(...), reason: str = Form("")):
        if auth.current_role(request) != auth.ROLE_SUPERVISOR:
            return RedirectResponse("/login", status_code=303)
        now = _now()
        reviewer = auth.current_name(request) or "监管者"
        if action != "approve":
            changed = conn.execute(
                "UPDATE time_exchange_requests SET status='rejected',reviewed_at=?,"
                "reviewed_by=?,review_reason=? WHERE id=? AND status='pending'",
                (now.isoformat(timespec="seconds"), reviewer,
                 reason.strip() or "本次兑换未批准", exchange_id),
            ).rowcount
            conn.commit()
            message = "兑换申请已驳回" if changed else "这笔申请已经处理"
            return RedirectResponse(f"/admin/reviews?kind=time_exchange&message={message}", status_code=303)

        stage, window = lifecycle.ensure_state(conn, now)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT r.*,s.active,s.cutoff_hour,s.exchange_enabled,s.exchange_daily_limit_minutes "
                "FROM time_exchange_requests r JOIN stages s ON s.id=r.stage_id "
                "WHERE r.id=? AND r.status='pending'", (exchange_id,),
            ).fetchone()
            if row is None:
                raise ValueError("这笔申请已经处理")
            day = lifecycle.logical_date(now, int(stage["cutoff_hour"] if stage else row["cutoff_hour"])).isoformat()
            if (not stage or stage["id"] != row["stage_id"] or not row["active"]
                    or not row["exchange_enabled"] or day != row["logical_date"]):
                raise ValueError("申请所属阶段或逻辑日已经结束")
            approved = conn.execute(
                "SELECT COALESCE(SUM(minutes_requested),0) AS n FROM time_exchange_requests "
                "WHERE stage_id=? AND logical_date=? AND status='approved'",
                (row["stage_id"], row["logical_date"]),
            ).fetchone()["n"]
            limit = int(row["exchange_daily_limit_minutes"])
            if limit and int(approved) + int(row["minutes_requested"]) > limit:
                raise ValueError("批准后会超过每日兑换上限")
            summary = lifecycle.point_summary(conn, stage, window)
            if int(row["points_cost"]) > summary["spendable"]:
                raise ValueError("当前可兑换积分已经不足")
            now_iso = now.isoformat(timespec="seconds")
            ledger_id = conn.execute(
                "INSERT INTO point_ledger (delta,entry_type,description,source_type,source_id,"
                "actor_name,created_at,window_id) VALUES (?,?,?,?,?,?,?,?)",
                (-int(row["points_cost"]), "exchange", "额外电脑时长兑换",
                 "time_exchange", str(row["id"]), reviewer, now_iso,
                 window["id"] if window else None),
            ).lastrowid
            grant_id = conn.execute(
                "INSERT INTO time_grants (logical_date,minutes,grant_type,description,source_type,"
                "source_id,actor_name,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (row["logical_date"], int(row["minutes_requested"]), "exchange",
                 f"{row['points_cost']}积分兑换", "time_exchange", str(row["id"]),
                 reviewer, now_iso),
            ).lastrowid
            changed = conn.execute(
                "UPDATE time_exchange_requests SET status='approved',reviewed_at=?,reviewed_by=?,"
                "review_reason=?,point_ledger_id=?,time_grant_id=? WHERE id=? AND status='pending'",
                (now_iso, reviewer, reason.strip(), ledger_id, grant_id, row["id"]),
            ).rowcount
            if changed != 1:
                raise ValueError("这笔申请已经处理")
            conn.commit()
        except Exception as exc:
            conn.rollback()
            message = str(exc) if isinstance(exc, ValueError) else "审批失败，请重试"
            return RedirectResponse(f"/admin/reviews?kind=time_exchange&message={message}", status_code=303)
        lifecycle.ensure_state(conn, now)
        return RedirectResponse(
            "/admin/reviews?kind=time_exchange&message=兑换已批准，积分和时长已经同步入账",
            status_code=303,
        )

    @app.get("/computer", response_class=HTMLResponse)
    def computer_page(request: Request, tab: str = "overview", error: str = "",
                      message: str = ""):
        if auth.current_role(request) is None:
            return RedirectResponse("/login", status_code=303)
        now = _now()
        stage, window = lifecycle.ensure_state(conn, now)
        cutoff = int(stage["cutoff_hour"]) if stage else 4
        summary = lifecycle.computer_summary(conn, now, cutoff)
        points = lifecycle.point_summary(conn, stage, window)
        requests = conn.execute(
            "SELECT r.*,s.name AS stage_name FROM time_exchange_requests r "
            "JOIN stages s ON s.id=r.stage_id ORDER BY r.id DESC LIMIT 20"
        ).fetchall()
        approved = 0
        pending_request = None
        if stage:
            approved = conn.execute(
                "SELECT COALESCE(SUM(minutes_requested),0) AS n FROM time_exchange_requests "
                "WHERE stage_id=? AND logical_date=? AND status='approved'",
                (stage["id"], summary["logical_date"]),
            ).fetchone()["n"]
            pending_request = conn.execute(
                "SELECT * FROM time_exchange_requests WHERE stage_id=? AND logical_date=? "
                "AND status='pending' ORDER BY id DESC LIMIT 1",
                (stage["id"], summary["logical_date"]),
            ).fetchone()
        max_units = 0
        if stage and stage["exchange_enabled"]:
            max_units = points["spendable"] // int(stage["exchange_points_per_unit"])
            limit = int(stage["exchange_daily_limit_minutes"])
            if limit:
                max_units = min(max_units, max(0, limit - int(approved))
                                // int(stage["exchange_minutes_per_unit"]))
        return templates.TemplateResponse(request, "computer.html", {
            "request": request, "role": auth.current_role(request), "tab": tab,
            "error": error, "message": message, "stage": stage, "summary": summary,
            "points": points, "max_units": max_units, "approved_exchange_minutes": int(approved),
            "pending_request": pending_request, "exchange_requests": requests,
            "sessions": conn.execute(
                "SELECT * FROM computer_sessions WHERE deleted_at IS NULL ORDER BY start_at DESC LIMIT 200"
            ).fetchall(),
            "audits": conn.execute(
                "SELECT * FROM computer_session_audits ORDER BY id DESC LIMIT 50"
            ).fetchall() if auth.current_role(request) == auth.ROLE_SUPERVISOR else [],
        })

    @app.get("/computer/history")
    def computer_history(request: Request):
        if auth.current_role(request) is None:
            return RedirectResponse("/login", status_code=303)
        return RedirectResponse("/computer", status_code=303)

    def _session_overlaps(start_at, end_at, exclude_id=None):
        sql = ("SELECT id FROM computer_sessions WHERE start_at < ? "
               "AND COALESCE(end_at,'9999-12-31T23:59:59') > ? AND deleted_at IS NULL")
        args = [end_at or "9999-12-31T23:59:59", start_at]
        if exclude_id is not None:
            sql += " AND id!=?"
            args.append(exclude_id)
        return conn.execute(sql, args).fetchone() is not None

    @app.get("/admin/computer")
    def admin_computer(request: Request, error: str = ""):
        if auth.current_role(request) != auth.ROLE_SUPERVISOR:
            return RedirectResponse("/login", status_code=303)
        suffix = f"&error={error}" if error else ""
        return RedirectResponse(f"/computer?tab=manage{suffix}", status_code=303)

    @app.post("/admin/computer/grant")
    def computer_grant(
        request: Request, logical_date: str = Form(...), minutes: str = Form(...),
        reason: str = Form(...),
    ):
        if auth.current_role(request) != auth.ROLE_SUPERVISOR:
            return RedirectResponse("/login", status_code=303)
        try:
            dt.date.fromisoformat(logical_date)
            value = int(minutes)
        except ValueError:
            return RedirectResponse("/computer?tab=manage&error=日期或分钟格式错误", status_code=303)
        if not reason.strip() or value == 0:
            return RedirectResponse("/computer?tab=manage&error=调整分钟和原因不能为空", status_code=303)
        now = _now().isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO time_grants (logical_date,minutes,grant_type,description,"
            "source_type,source_id,actor_name,created_at) VALUES (?,?, 'adjustment',?,?,?,?,?)",
            (logical_date, value, reason.strip(), "manual_time", uuid.uuid4().hex,
             auth.current_name(request) or "监管者", now),
        )
        conn.commit()
        return RedirectResponse("/computer?tab=manage&message=时长调整已记录", status_code=303)

    @app.post("/admin/computer/session")
    def computer_session_edit(
        request: Request, session_id: str = Form(""), start_at: str = Form(...),
        end_at: str = Form(""), reason: str = Form(...),
    ):
        if auth.current_role(request) != auth.ROLE_SUPERVISOR:
            return RedirectResponse("/login", status_code=303)
        start = start_at
        end = end_at if end_at else None
        try:
            start_dt = dt.datetime.fromisoformat(start)
            end_dt = dt.datetime.fromisoformat(end) if end else None
        except ValueError:
            return RedirectResponse("/computer?tab=manage&error=时间格式错误", status_code=303)
        if not reason.strip() or (end_dt and end_dt <= start_dt):
            return RedirectResponse("/computer?tab=manage&error=请填写原因并检查起止时间", status_code=303)
        sid = int(session_id) if session_id else None
        if _session_overlaps(start, end, sid):
            return RedirectResponse("/computer?tab=manage&error=该时段与已有记录重叠", status_code=303)
        actor = auth.current_name(request) or "监管者"
        now = _now().isoformat(timespec="seconds")
        if sid:
            before = conn.execute("SELECT * FROM computer_sessions WHERE id=?", (sid,)).fetchone()
            if before is None:
                return RedirectResponse("/computer?tab=manage", status_code=303)
            conn.execute(
                "UPDATE computer_sessions SET start_at=?,end_at=?,ended_by=?,note=?,updated_at=? "
                "WHERE id=?", (start, end, actor, reason.strip(), now, sid),
            )
            action = "correct"
            before_start, before_end = before["start_at"], before["end_at"]
        else:
            sid = conn.execute(
                "INSERT INTO computer_sessions (start_at,end_at,started_by,ended_by,note,source,updated_at) "
                "VALUES (?,?,?,?,?,'manual',?)",
                (start, end, actor, actor if end else None, reason.strip(), now),
            ).lastrowid
            action, before_start, before_end = "create", None, None
        conn.execute(
            "INSERT INTO computer_session_audits "
            "(session_id,action,before_start,before_end,after_start,after_end,actor_name,reason,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (sid, action, before_start, before_end, start, end, actor, reason.strip(), now),
        )
        conn.commit()
        return RedirectResponse("/computer?tab=manage&message=电脑记录已保存", status_code=303)

    @app.post("/admin/computer/force-off")
    def computer_force_off(request: Request, reason: str = Form(...)):
        if auth.current_role(request) != auth.ROLE_SUPERVISOR:
            return RedirectResponse("/login", status_code=303)
        row = conn.execute(
            "SELECT * FROM computer_sessions WHERE end_at IS NULL AND deleted_at IS NULL "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row and reason.strip():
            actor = auth.current_name(request) or "监管者"
            now = _now().isoformat(timespec="seconds")
            conn.execute(
                "UPDATE computer_sessions SET end_at=?,ended_by=?,note=?,updated_at=? WHERE id=?",
                (now, actor, reason.strip(), now, row["id"]),
            )
            conn.execute(
                "INSERT INTO computer_session_audits "
                "(session_id,action,before_start,before_end,after_start,after_end,actor_name,reason,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (row["id"], "force_off", row["start_at"], None,
                 row["start_at"], now, actor, reason.strip(), now),
            )
            conn.commit()
        return RedirectResponse("/computer?tab=manage&message=使用中会话已结束", status_code=303)

    @app.get("/admin/trash", response_class=HTMLResponse)
    def trash_page(request: Request, error: str = ""):
        if auth.current_role(request) != auth.ROLE_SUPERVISOR:
            return RedirectResponse("/login", status_code=303)
        rows = conn.execute(
            "SELECT * FROM record_deletions WHERE restored_at IS NULL AND purged_at IS NULL "
            "ORDER BY id DESC"
        ).fetchall()
        history = conn.execute(
            "SELECT * FROM record_deletions WHERE restored_at IS NOT NULL OR purged_at IS NOT NULL "
            "ORDER BY id DESC LIMIT 100"
        ).fetchall()
        return templates.TemplateResponse(request, "trash.html", {
            "request": request, "rows": rows, "history": history, "error": error,
            "now_iso": _now().isoformat(timespec="seconds"),
        })

    @app.post("/admin/records/{entity_type}/{record_id}/trash")
    def trash_record_route(
        request: Request, entity_type: str, record_id: int, reason: str = Form(...),
        return_to: str = Form("/admin/trash"),
    ):
        if auth.current_role(request) != auth.ROLE_SUPERVISOR:
            return RedirectResponse("/login", status_code=303)
        if entity_type == "ledger":
            ledger = conn.execute(
                "SELECT * FROM point_ledger WHERE id=? AND deleted_at IS NULL", (record_id,),
            ).fetchone()
            if not ledger or ledger["source_type"] not in ("checkin", "legacy_checkin", "mission"):
                return RedirectResponse("/admin/trash?error=系统流水不能单独删除", status_code=303)
            entity_type = "mission" if ledger["source_type"] == "mission" else "checkin"
            record_id = int(ledger["source_id"])
        try:
            records.trash_record(
                conn, entity_type, record_id, auth.current_name(request) or "监管者",
                reason, _now(),
            )
        except ValueError as exc:
            return RedirectResponse(f"/admin/trash?error={exc}", status_code=303)
        safe_return = return_to if return_to.startswith("/") and not return_to.startswith("//") else "/admin/trash"
        return RedirectResponse(safe_return, status_code=303)

    @app.post("/admin/trash/{deletion_id}/restore")
    def trash_restore(request: Request, deletion_id: int):
        if auth.current_role(request) != auth.ROLE_SUPERVISOR:
            return RedirectResponse("/login", status_code=303)
        try:
            records.restore_record(
                conn, deletion_id, auth.current_name(request) or "监管者", _now(),
            )
        except ValueError as exc:
            return RedirectResponse(f"/admin/trash?error={exc}", status_code=303)
        return RedirectResponse("/admin/trash", status_code=303)

    return app


app = create_app() if os.environ.get("CHORES_DB_PATH") else None
