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

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        if auth.current_role(request) is None:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(
            "base.html", {"request": request, "body": "dashboard placeholder"}
        )

    return app


app = create_app() if os.environ.get("CHORES_DB_PATH") else None
