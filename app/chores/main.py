import os
import datetime as dt
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.concurrency import run_in_threadpool

from chores.config import Config
from chores import db as dbmod
from chores import auth, scoring
from chores.images import save_photo

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
    dbmod.ensure_open_period(conn, _now().isoformat(timespec="seconds"))
    os.makedirs(cfg.photo_dir, exist_ok=True)

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key=cfg.secret_key)
    app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
    app.mount("/photos", StaticFiles(directory=cfg.photo_dir), name="photos")
    templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))

    app.state.cfg = cfg
    app.state.conn = conn
    app.state.templates = templates

    def _goal() -> int:
        v = dbmod.get_setting(conn, "period_goal", str(cfg.weekly_goal))
        try:
            return int(v)
        except (TypeError, ValueError):
            return cfg.weekly_goal

    def _period_rows(pid: int):
        rows = conn.execute(
            "SELECT c.*, t.name AS task_name FROM checkins c "
            "LEFT JOIN task_catalog t ON c.task_id=t.id WHERE c.period_id=?",
            (pid,),
        ).fetchall()
        return [dict(r) for r in rows]

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
            "login.html", {"request": request, "error": error}
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
            "whoami.html", {"request": request, "supervisors": sups}
        )

    @app.post("/whoami")
    def whoami_set(request: Request, name: str = Form(...)):
        if auth.current_role(request) != auth.ROLE_SUPERVISOR:
            return RedirectResponse("/login", status_code=303)
        request.session["name"] = name
        return RedirectResponse("/", status_code=303)

    @app.get("/checkin", response_class=HTMLResponse)
    def checkin_form(request: Request):
        if auth.current_role(request) is None:
            return RedirectResponse("/login", status_code=303)
        tasks = conn.execute(
            "SELECT id, name, default_points FROM task_catalog "
            "WHERE active=1 ORDER BY sort_order, id"
        ).fetchall()
        return templates.TemplateResponse(
            "checkin_form.html", {"request": request, "tasks": tasks}
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
        photo: UploadFile = File(None),
    ):
        if auth.current_role(request) is None:
            return RedirectResponse("/login", status_code=303)
        photo_path = None
        if photo is not None:
            raw = await photo.read()
            if raw:
                # 压缩在线程池跑，不阻塞单进程事件循环。
                photo_path = await run_in_threadpool(save_photo, raw, cfg.photo_dir)
        now = _now().isoformat(timespec="seconds")
        pid = dbmod.ensure_open_period(conn, now)
        if kind == "fixed":
            t = conn.execute(
                "SELECT default_points FROM task_catalog WHERE id=?", (task_id,)
            ).fetchone()
            pts = scoring.award_for_fixed(t)
            conn.execute(
                "INSERT INTO checkins (kind, task_id, period_id, photo_path, note,"
                " mood, awarded_points, status, created_at) VALUES "
                "(?,?,?,?,?,?,?,'scored',?)",
                (kind, int(task_id), pid, photo_path, note, mood, pts, now),
            )
        else:
            prop = int(proposed_points) if proposed_points.strip() else None
            conn.execute(
                "INSERT INTO checkins (kind, period_id, title, photo_path, note,"
                " mood, proposed_points, status, created_at) VALUES "
                "(?,?,?,?,?,?,?,'pending',?)",
                ("adhoc", pid, title, photo_path, note, mood, prop, now),
            )
        conn.commit()
        return RedirectResponse("/", status_code=303)

    @app.post("/computer/on")
    def computer_on(request: Request):
        if auth.current_role(request) is None:
            return RedirectResponse("/login", status_code=303)
        open_row = conn.execute(
            "SELECT id FROM computer_sessions WHERE end_at IS NULL"
        ).fetchone()
        if open_row is None:
            conn.execute(
                "INSERT INTO computer_sessions (start_at) VALUES (?)",
                (_now().isoformat(timespec="seconds"),),
            )
            conn.commit()
        return RedirectResponse("/", status_code=303)

    @app.post("/computer/off")
    def computer_off(request: Request):
        if auth.current_role(request) is None:
            return RedirectResponse("/login", status_code=303)
        open_row = conn.execute(
            "SELECT id FROM computer_sessions WHERE end_at IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if open_row is not None:
            conn.execute(
                "UPDATE computer_sessions SET end_at=? WHERE id=?",
                (_now().isoformat(timespec="seconds"), open_row["id"]),
            )
            conn.commit()
        return RedirectResponse("/", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        if auth.current_role(request) is None:
            return RedirectResponse("/login", status_code=303)
        now = _now()
        today = now.date()
        period = dbmod.current_period(conn)
        if period is None:
            dbmod.ensure_open_period(conn, now.isoformat(timespec="seconds"))
            period = dbmod.current_period(conn)
        rows = _period_rows(period["id"])
        sessions = [
            dict(r) for r in conn.execute(
                "SELECT start_at, end_at FROM computer_sessions"
            ).fetchall()
        ]
        comp_min, comp_seg = scoring.computer_today(sessions, now)
        ann = conn.execute(
            "SELECT body FROM announcements WHERE active=1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        feed = _build_feed(rows)
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
            "comp_min": comp_min,
            "comp_seg": comp_seg,
            "announcement": ann["body"] if ann else "",
            "feed": feed,
        }
        return templates.TemplateResponse("dashboard.html", ctx)

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
                "FROM checkins WHERE period_id=? AND status='scored'", (p["id"],)
            ).fetchone()
            items.append({
                "id": p["id"], "seq": p["seq"],
                "started": p["started_at"][:10], "ended": p["ended_at"][:10],
                "total": agg["total"], "n": agg["n"] or 0,
            })
        return templates.TemplateResponse(
            "history.html", {"request": request, "items": items}
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
        return templates.TemplateResponse("history_detail.html", ctx)

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
        return templates.TemplateResponse("archive_confirm.html", ctx)

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
        if body.strip():
            conn.execute(
                "INSERT INTO comments (checkin_id, author_name, author_role, body,"
                " created_at) VALUES (?,?,?,?,?)",
                (cid, name, role, body.strip(), _now().isoformat(timespec="seconds")),
            )
            conn.commit()
        return RedirectResponse("/", status_code=303)

    @app.post("/checkin/{cid}/react")
    def add_react(request: Request, cid: int, kind: str = Form(...)):
        if auth.current_role(request) is None:
            return RedirectResponse("/login", status_code=303)
        role, name = _author(request)
        conn.execute(
            "INSERT INTO reactions (checkin_id, author_name, kind, created_at)"
            " VALUES (?,?,?,?)",
            (cid, name, kind, _now().isoformat(timespec="seconds")),
        )
        conn.commit()
        return RedirectResponse("/", status_code=303)

    @app.get("/review", response_class=HTMLResponse)
    def review_list(request: Request):
        if auth.current_role(request) != auth.ROLE_SUPERVISOR:
            return RedirectResponse("/login", status_code=303)
        pend = conn.execute(
            "SELECT * FROM checkins WHERE status='pending' ORDER BY id"
        ).fetchall()
        return templates.TemplateResponse(
            "review.html", {"request": request, "pending": pend}
        )

    @app.post("/review/{cid}")
    def review_act(
        request: Request, cid: int,
        action: str = Form(...), points: str = Form("0")
    ):
        if auth.current_role(request) != auth.ROLE_SUPERVISOR:
            return RedirectResponse("/login", status_code=303)
        reviewer = auth.current_name(request) or "监管者"
        now = _now().isoformat(timespec="seconds")
        if action == "approve":
            conn.execute(
                "UPDATE checkins SET status='scored', awarded_points=?, "
                "reviewed_by=?, reviewed_at=? WHERE id=?",
                (int(points or 0), reviewer, now, cid),
            )
        else:
            conn.execute(
                "UPDATE checkins SET status='rejected', awarded_points=0, "
                "reviewed_by=?, reviewed_at=? WHERE id=?",
                (reviewer, now, cid),
            )
        conn.commit()
        return RedirectResponse("/review", status_code=303)

    @app.post("/adjust")
    def adjust(request: Request, points: str = Form(...), reason: str = Form("")):
        if auth.current_role(request) != auth.ROLE_SUPERVISOR:
            return RedirectResponse("/login", status_code=303)
        reviewer = auth.current_name(request) or "监管者"
        now = _now().isoformat(timespec="seconds")
        pid = dbmod.ensure_open_period(conn, now)
        conn.execute(
            "INSERT INTO checkins (kind, period_id, title, note, awarded_points,"
            " status, created_at, reviewed_by, reviewed_at) VALUES "
            "('adjustment',?,?,?,?, 'scored', ?, ?, ?)",
            (pid, "分值调整", reason, int(points), now, reviewer, now),
        )
        conn.commit()
        return RedirectResponse("/", status_code=303)

    @app.get("/admin/catalog", response_class=HTMLResponse)
    def catalog_page(request: Request):
        if auth.current_role(request) != auth.ROLE_SUPERVISOR:
            return RedirectResponse("/login", status_code=303)
        tasks = conn.execute(
            "SELECT * FROM task_catalog ORDER BY sort_order, id"
        ).fetchall()
        return templates.TemplateResponse(
            "catalog_admin.html", {"request": request, "tasks": tasks}
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
            "announcement.html",
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

    return app


app = create_app() if os.environ.get("CHORES_DB_PATH") else None
