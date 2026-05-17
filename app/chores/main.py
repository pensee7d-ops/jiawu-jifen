import os
import datetime as dt
from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from chores.config import Config
from chores import db as dbmod
from chores import auth, scoring
from chores.images import save_photo

BASE = os.path.dirname(__file__)


def _now() -> dt.datetime:
    return dt.datetime.now()


def create_app() -> FastAPI:
    cfg = Config.from_env()
    conn = dbmod.connect(cfg.db_path)
    dbmod.init_schema(conn)
    dbmod.seed_defaults(conn)
    os.makedirs(cfg.photo_dir, exist_ok=True)

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key=cfg.secret_key)
    app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
    app.mount("/photos", StaticFiles(directory=cfg.photo_dir), name="photos")
    templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))

    app.state.cfg = cfg
    app.state.conn = conn
    app.state.templates = templates

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
        request.session["name"] = "弟弟"
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
                photo_path = save_photo(raw, cfg.photo_dir)
        now = _now().isoformat(timespec="seconds")
        if kind == "fixed":
            t = conn.execute(
                "SELECT default_points FROM task_catalog WHERE id=?", (task_id,)
            ).fetchone()
            pts = scoring.award_for_fixed(t)
            conn.execute(
                "INSERT INTO checkins (kind, task_id, photo_path, note, mood,"
                " awarded_points, status, created_at) VALUES "
                "(?,?,?,?,?,?,'scored',?)",
                (kind, int(task_id), photo_path, note, mood, pts, now),
            )
        else:
            prop = int(proposed_points) if proposed_points.strip() else None
            conn.execute(
                "INSERT INTO checkins (kind, title, photo_path, note, mood,"
                " proposed_points, status, created_at) VALUES "
                "(?,?,?,?,?,?,'pending',?)",
                ("adhoc", title, photo_path, note, mood, prop, now),
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
        return templates.TemplateResponse(
            "base.html", {"request": request, "body": "dashboard placeholder"}
        )

    return app


app = create_app() if os.environ.get("CHORES_DB_PATH") else None
