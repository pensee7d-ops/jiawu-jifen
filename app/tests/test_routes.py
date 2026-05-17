import io
import pytest
from PIL import Image
from fastapi.testclient import TestClient


def _jpg_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (1200, 900), (90, 90, 90)).save(buf, format="JPEG")
    return buf.getvalue()


def _make_fixed_checkin(client):
    client.post(
        "/checkin",
        data={"kind": "fixed", "task_id": "3", "note": "", "mood": ""},
        files={"photo": ("a.jpg", _jpg_bytes(), "image/jpeg")},
    )
    return client.app.state.conn.execute(
        "SELECT id FROM checkins ORDER BY id DESC LIMIT 1"
    ).fetchone()["id"]


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


def test_dashboard_shows_week_and_today_totals(client):
    _login_checkin(client)
    client.post(
        "/checkin",
        data={"kind": "fixed", "task_id": "3", "note": "", "mood": ""},
        files={"photo": ("a.jpg", _jpg_bytes(), "image/jpeg")},
    )
    r = client.get("/")
    assert r.status_code == 200
    assert "本周累计" in r.text
    assert "今日电脑使用" in r.text
    assert "10" in r.text  # 全屋吸尘 10 分


def test_comment_records_author_name_and_role(client):
    client.post("/login", data={"password": "jia"})
    client.post("/whoami", data={"name": "姐姐"})
    cid = _make_fixed_checkin(client)
    r = client.post(f"/checkin/{cid}/comment", data={"body": "干得漂亮！"})
    assert r.status_code == 303
    row = client.app.state.conn.execute(
        "SELECT * FROM comments ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["author_name"] == "姐姐"
    assert row["author_role"] == "supervisor"
    assert row["body"] == "干得漂亮！"


def test_react_records_stamp(client):
    client.post("/login", data={"password": "jia"})
    client.post("/whoami", data={"name": "姐姐"})
    cid = _make_fixed_checkin(client)
    r = client.post(f"/checkin/{cid}/react", data={"kind": "🏅"})
    assert r.status_code == 303
    row = client.app.state.conn.execute(
        "SELECT * FROM reactions ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["kind"] == "🏅" and row["author_name"] == "姐姐"


def test_review_list_shows_pending(client):
    _login_checkin(client)
    client.post("/checkin", data={"kind": "adhoc", "title": "擦窗", "proposed_points": "6"},
                files={"photo": ("a.jpg", _jpg_bytes(), "image/jpeg")})
    client.get("/logout")
    client.post("/login", data={"password": "jia"})
    client.post("/whoami", data={"name": "姐姐"})
    r = client.get("/review")
    assert r.status_code == 200 and "擦窗" in r.text


def test_approve_sets_scored_with_points_and_reviewer(client):
    _login_checkin(client)
    client.post("/checkin", data={"kind": "adhoc", "title": "擦窗", "proposed_points": "6"},
                files={"photo": ("a.jpg", _jpg_bytes(), "image/jpeg")})
    cid = client.app.state.conn.execute(
        "SELECT id FROM checkins ORDER BY id DESC LIMIT 1").fetchone()["id"]
    client.get("/logout")
    client.post("/login", data={"password": "jia"})
    client.post("/whoami", data={"name": "姐姐"})
    r = client.post(f"/review/{cid}", data={"action": "approve", "points": "8"})
    assert r.status_code == 303
    row = client.app.state.conn.execute(
        "SELECT * FROM checkins WHERE id=?", (cid,)).fetchone()
    assert row["status"] == "scored" and row["awarded_points"] == 8
    assert row["reviewed_by"] == "姐姐"


def test_reject_sets_rejected(client):
    _login_checkin(client)
    client.post("/checkin", data={"kind": "adhoc", "title": "x", "proposed_points": "6"},
                files={"photo": ("a.jpg", _jpg_bytes(), "image/jpeg")})
    cid = client.app.state.conn.execute(
        "SELECT id FROM checkins ORDER BY id DESC LIMIT 1").fetchone()["id"]
    client.get("/logout"); client.post("/login", data={"password": "jia"})
    client.post("/whoami", data={"name": "姐姐"})
    client.post(f"/review/{cid}", data={"action": "reject", "points": "0"})
    row = client.app.state.conn.execute(
        "SELECT status FROM checkins WHERE id=?", (cid,)).fetchone()
    assert row["status"] == "rejected"


def test_supervisor_can_deduct_points(client):
    client.post("/login", data={"password": "jia"})
    client.post("/whoami", data={"name": "姐姐"})
    r = client.post("/adjust", data={"points": "-5", "reason": "顶嘴扣分"})
    assert r.status_code == 303
    row = client.app.state.conn.execute(
        "SELECT * FROM checkins ORDER BY id DESC LIMIT 1").fetchone()
    assert row["kind"] == "adjustment" and row["awarded_points"] == -5
    assert row["status"] == "scored"


def test_review_requires_supervisor(client):
    _login_checkin(client)
    r = client.get("/review")
    assert r.status_code == 303 and r.headers["location"] == "/login"


def _sup(client):
    client.post("/login", data={"password": "jia"})
    client.post("/whoami", data={"name": "姐姐"})


def test_add_task_to_catalog(client):
    _sup(client)
    r = client.post("/admin/catalog", data={
        "op": "add", "name": "遛狗", "points": "4", "is_vocab": "0"})
    assert r.status_code == 303
    row = client.app.state.conn.execute(
        "SELECT * FROM task_catalog WHERE name='遛狗'").fetchone()
    assert row["default_points"] == 4


def test_deactivate_task(client):
    _sup(client)
    client.post("/admin/catalog", data={"op": "deactivate", "task_id": "1"})
    row = client.app.state.conn.execute(
        "SELECT active FROM task_catalog WHERE id=1").fetchone()
    assert row["active"] == 0


def test_set_announcement(client):
    _sup(client)
    r = client.post("/admin/announcement", data={"body": "五一：每日 60 分玩 3h"})
    assert r.status_code == 303
    row = client.app.state.conn.execute(
        "SELECT * FROM announcements WHERE active=1 ORDER BY id DESC LIMIT 1").fetchone()
    assert "五一" in row["body"]


def test_catalog_requires_supervisor(client):
    _login_checkin(client)
    r = client.get("/admin/catalog")
    assert r.status_code == 303 and r.headers["location"] == "/login"
