import pytest
from fastapi.testclient import TestClient


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
