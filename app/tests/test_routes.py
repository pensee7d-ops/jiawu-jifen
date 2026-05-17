import io
import pytest
from PIL import Image
from fastapi.testclient import TestClient


def _jpg_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (1200, 900), (90, 90, 90)).save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CHORES_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("CHORES_PHOTO_DIR", str(tmp_path / "photos"))
    monkeypatch.setenv("CHORES_SECRET_KEY", "testsecret")
    monkeypatch.setenv("CHORES_CHECKIN_PASSWORD", "didi")
    monkeypatch.setenv("CHORES_SUPERVISOR_PASSWORD", "jia")
    monkeypatch.setenv("CHORES_WEEKLY_GOAL", "300")
    from chores.main import create_app
    return TestClient(create_app(), follow_redirects=False)


def test_login_page_renders(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert "口令" in r.text


def test_login_checkin_redirects_to_dashboard(client):
    r = client.post("/login", data={"password": "didi"})
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_login_supervisor_redirects_to_pick_name(client):
    r = client.post("/login", data={"password": "jia"})
    assert r.status_code == 303
    assert r.headers["location"] == "/whoami"


def test_login_wrong_password_shows_error(client):
    r = client.post("/login", data={"password": "x"}, follow_redirects=True)
    assert "口令错误" in r.text


def test_dashboard_requires_login(client):
    r = client.get("/")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_supervisor_picks_name_and_can_open_dashboard(client):
    client.post("/login", data={"password": "jia"})
    r = client.get("/whoami")
    assert r.status_code == 200 and "姐姐" in r.text
    r = client.post("/whoami", data={"name": "姐姐"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    r = client.get("/")
    assert r.status_code == 200


def _login_checkin(client):
    client.post("/login", data={"password": "didi"})


def test_checkin_form_lists_tasks(client):
    _login_checkin(client)
    r = client.get("/checkin")
    assert r.status_code == 200
    assert "全屋拖地" in r.text


def test_fixed_checkin_auto_scores(client):
    _login_checkin(client)
    r = client.post(
        "/checkin",
        data={"kind": "fixed", "task_id": "3", "note": "扫干净了", "mood": "😀"},
        files={"photo": ("a.jpg", _jpg_bytes(), "image/jpeg")},
    )
    assert r.status_code == 303
    row = client.app.state.conn.execute(
        "SELECT * FROM checkins ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["status"] == "scored"
    assert row["awarded_points"] == 10  # 全屋吸尘默认 10
    assert row["photo_path"].endswith(".jpg")


def test_adhoc_checkin_is_pending(client):
    _login_checkin(client)
    r = client.post(
        "/checkin",
        data={"kind": "adhoc", "title": "帮邻居取快递", "proposed_points": "4",
              "note": "顺手", "mood": "🙂"},
        files={"photo": ("a.jpg", _jpg_bytes(), "image/jpeg")},
    )
    assert r.status_code == 303
    row = client.app.state.conn.execute(
        "SELECT * FROM checkins ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["status"] == "pending"
    assert row["proposed_points"] == 4
    assert row["awarded_points"] is None


def test_computer_on_then_off_creates_closed_session(client):
    _login_checkin(client)
    r = client.post("/computer/on")
    assert r.status_code == 303
    r = client.post("/computer/off")
    assert r.status_code == 303
    rows = client.app.state.conn.execute(
        "SELECT * FROM computer_sessions ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert rows["start_at"] is not None and rows["end_at"] is not None


def test_computer_off_without_open_is_noop(client):
    _login_checkin(client)
    r = client.post("/computer/off")
    assert r.status_code == 303
    n = client.app.state.conn.execute(
        "SELECT COUNT(*) AS c FROM computer_sessions"
    ).fetchone()["c"]
    assert n == 0
