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
    assert r.status_code == 200 and "惠姐" in r.text
    r = client.post("/whoami", data={"name": "惠姐"})
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
    home = client.get("/")
    assert "home-computer-actions" in home.text
    assert "▶ 开机" in home.text and "■ 关机" in home.text
    r = client.post("/computer/on", data={"return_to": "/"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    r = client.post("/computer/off", data={"return_to": "/"})
    assert r.status_code == 303 and r.headers["location"] == "/"
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
    assert "今日概况" in r.text
    assert "今日电脑" in r.text
    assert "今日打卡" in r.text
    assert "10" in r.text  # 全屋吸尘 10 分


def test_comment_records_author_name_and_role(client):
    _login_checkin(client)
    cid = _make_fixed_checkin(client)
    client.get("/logout")
    client.post("/login", data={"password": "jia"})
    client.post("/whoami", data={"name": "惠姐"})
    r = client.post(f"/checkin/{cid}/comment", data={"body": "干得漂亮！"})
    assert r.status_code == 303
    row = client.app.state.conn.execute(
        "SELECT * FROM comments ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["author_name"] == "惠姐"
    assert row["author_role"] == "supervisor"
    assert row["body"] == "干得漂亮！"


def test_react_records_stamp(client):
    _login_checkin(client)
    cid = _make_fixed_checkin(client)
    client.get("/logout")
    client.post("/login", data={"password": "jia"})
    client.post("/whoami", data={"name": "惠姐"})
    r = client.post(f"/checkin/{cid}/react", data={"kind": "🏅"})
    assert r.status_code == 303
    row = client.app.state.conn.execute(
        "SELECT * FROM reactions ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["kind"] == "🏅" and row["author_name"] == "惠姐"


def test_review_list_shows_pending(client):
    _login_checkin(client)
    client.post("/checkin", data={"kind": "adhoc", "title": "擦窗", "proposed_points": "6"},
                files={"photo": ("a.jpg", _jpg_bytes(), "image/jpeg")})
    client.get("/logout")
    client.post("/login", data={"password": "jia"})
    client.post("/whoami", data={"name": "惠姐"})
    r = client.get("/review")
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/reviews?kind=checkin"
    r = client.get(r.headers["location"])
    assert r.status_code == 200 and "擦窗" in r.text


def test_approve_sets_scored_with_points_and_reviewer(client):
    _login_checkin(client)
    client.post("/checkin", data={"kind": "adhoc", "title": "擦窗", "proposed_points": "6"},
                files={"photo": ("a.jpg", _jpg_bytes(), "image/jpeg")})
    cid = client.app.state.conn.execute(
        "SELECT id FROM checkins ORDER BY id DESC LIMIT 1").fetchone()["id"]
    client.get("/logout")
    client.post("/login", data={"password": "jia"})
    client.post("/whoami", data={"name": "惠姐"})
    r = client.post(f"/review/{cid}", data={"action": "approve", "points": "8"})
    assert r.status_code == 303
    row = client.app.state.conn.execute(
        "SELECT * FROM checkins WHERE id=?", (cid,)).fetchone()
    assert row["status"] == "scored" and row["awarded_points"] == 8
    assert row["reviewed_by"] == "惠姐"


def test_reject_sets_rejected(client):
    _login_checkin(client)
    client.post("/checkin", data={"kind": "adhoc", "title": "x", "proposed_points": "6"},
                files={"photo": ("a.jpg", _jpg_bytes(), "image/jpeg")})
    cid = client.app.state.conn.execute(
        "SELECT id FROM checkins ORDER BY id DESC LIMIT 1").fetchone()["id"]
    client.get("/logout")
    client.post("/login", data={"password": "jia"})
    client.post("/whoami", data={"name": "惠姐"})
    client.post(f"/review/{cid}", data={"action": "reject", "points": "0"})
    row = client.app.state.conn.execute(
        "SELECT status FROM checkins WHERE id=?", (cid,)).fetchone()
    assert row["status"] == "rejected"


def test_supervisor_can_deduct_points(client):
    client.post("/login", data={"password": "jia"})
    client.post("/whoami", data={"name": "惠姐"})
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
    client.post("/whoami", data={"name": "惠姐"})


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


def test_supervisor_sets_period_goal_reflected_on_dashboard(client):
    _sup(client)
    # 默认目标 80
    r = client.get("/")
    assert "/ 80" in r.text
    # 改成 50，无需重启/发版
    r = client.post("/admin/announcement", data={"body": "", "goal": "50"})
    assert r.status_code == 303
    r = client.get("/")
    assert "/ 50" in r.text
    assert client.app.state.conn.execute(
        "SELECT value FROM settings WHERE key='period_goal'"
    ).fetchone()["value"] == "50"


def test_catalog_requires_supervisor(client):
    _login_checkin(client)
    r = client.get("/admin/catalog")
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_checkin_person_display_name_is_haoge(client):
    _login_checkin(client)
    cid = _make_fixed_checkin(client)
    client.post(f"/checkin/{cid}/comment", data={"body": "我做完啦"})
    row = client.app.state.conn.execute(
        "SELECT author_name FROM comments ORDER BY id DESC LIMIT 1").fetchone()
    assert row["author_name"] == "浩哥"


def test_feed_excludes_supervisor_adjustment(client):
    _login_checkin(client)
    _make_fixed_checkin(client)            # 一条正常打卡
    client.get("/logout")
    client.post("/login", data={"password": "jia"})
    client.post("/whoami", data={"name": "惠姐"})
    client.post("/adjust", data={"points": "-5", "reason": "顶嘴扣分"})
    r = client.get("/activity")
    assert r.status_code == 200
    assert "全屋吸尘" in r.text          # 打卡仍在动态
    assert "分值调整" not in r.text      # 监管者调整不在动态
    assert "冒险动态" in r.text


def test_archive_closes_period_and_history_keeps_it(client):
    _login_checkin(client)
    _make_fixed_checkin(client)            # 第1期：10 分
    client.get("/logout")
    client.post("/login", data={"password": "jia"})
    client.post("/whoami", data={"name": "惠姐"})
    r = client.get("/archive")
    assert r.status_code == 200 and "第 1 期" in r.text
    r = client.post("/archive")
    assert r.status_code == 303 and r.headers["location"] == "/"
    # 新周期已创建；首页只展示状态，不再混入旧周期管理信息
    r = client.get("/")
    assert "今日状态" in r.text
    conn = client.app.state.conn
    periods = conn.execute(
        "SELECT * FROM periods ORDER BY id").fetchall()
    assert len(periods) == 2
    assert periods[0]["ended_at"] is not None
    assert periods[1]["ended_at"] is None
    # 历史里能查到第 1 期及其动态
    r = client.get("/history")
    assert r.status_code == 200 and "第 1 期" in r.text
    r = client.get("/history/1")
    assert r.status_code == 200 and "全屋吸尘" in r.text


def test_archive_requires_supervisor(client):
    _login_checkin(client)
    r = client.post("/archive")
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_history_requires_login(client):
    r = client.get("/history")
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_now_is_beijing_time():
    import datetime as dt
    from chores.main import _now
    delta = _now() - dt.datetime.utcnow()
    # 北京 = UTC+8，留 5 分钟容差
    assert dt.timedelta(hours=7, minutes=55) < delta < dt.timedelta(hours=8, minutes=5)


def test_photo_picker_allows_album_and_rejects_bad_image(client):
    _login_checkin(client)
    page = client.get("/checkin")
    assert 'name="photo_camera"' in page.text and 'capture="environment"' in page.text
    assert 'name="photo_album" accept="image/*"' in page.text
    assert "从相册选择" in page.text
    result = client.post(
        "/checkin",
        data={"kind": "fixed", "task_id": "1"},
        files={"photo": ("bad.jpg", b"not-an-image", "image/jpeg")},
    )
    assert result.status_code == 400
    assert "格式不支持" in result.text


def test_stage_and_mission_flow_awards_points_once(client):
    _sup(client)
    client.post("/admin/stages", data={
        "op": "add", "name": "测试暑假", "starts_on": "2020-01-01",
        "ends_on": "2035-12-31", "mode": "daily", "cutoff_hour": "4",
        "window_start_weekday": "4", "window_end_weekday": "0",
        "points_goal": "30", "basic_minutes": "120", "reward_minutes": "120",
    })
    stage_id = client.app.state.conn.execute(
        "SELECT id FROM stages ORDER BY id DESC LIMIT 1"
    ).fetchone()["id"]
    client.post(f"/admin/stages/{stage_id}/switch")
    client.post("/admin/missions", data={
        "title": "整理书桌", "description": "收干净", "points": "40",
        "deadline_at": "", "photo_required": "0",
    })
    mid = client.app.state.conn.execute(
        "SELECT id FROM missions ORDER BY id DESC LIMIT 1"
    ).fetchone()["id"]
    client.get("/logout")
    _login_checkin(client)
    assert client.post(f"/missions/{mid}/claim").status_code == 303
    assert client.post(f"/missions/{mid}/submit", data={"note": "完成"}).status_code == 303
    client.get("/logout")
    _sup(client)
    assert client.post(
        f"/admin/missions/{mid}/review",
        data={"action": "approve", "points": "40", "reason": ""},
    ).status_code == 303
    # 重复审核是无操作，积分来源唯一。
    client.post(
        f"/admin/missions/{mid}/review",
        data={"action": "approve", "points": "40", "reason": ""},
    )
    conn = client.app.state.conn
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM point_ledger WHERE source_type='mission' AND source_id=?",
        (str(mid),),
    ).fetchone()["n"] == 1
    assert conn.execute(
        "SELECT reward_unlocked FROM settlement_windows ORDER BY id DESC LIMIT 1"
    ).fetchone()["reward_unlocked"] == 1
    activity = client.get("/activity?kind=mission")
    assert activity.status_code == 200
    assert activity.text.count("整理书桌") == 1
    assert "发布" in activity.text and "领取" in activity.text
    assert "提交" in activity.text and "通过" in activity.text


def test_computer_adjustment_and_correction_are_audited(client):
    _sup(client)
    client.post("/admin/computer/grant", data={
        "logical_date": "2026-07-23", "minutes": "30", "reason": "表现很好",
    })
    client.post("/admin/computer/session", data={
        "start_at": "2026-07-23T10:00", "end_at": "2026-07-23T11:00",
        "reason": "补录",
    })
    conn = client.app.state.conn
    session = conn.execute(
        "SELECT * FROM computer_sessions WHERE source='manual' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert session is not None
    client.post("/admin/computer/session", data={
        "session_id": str(session["id"]), "start_at": "2026-07-23T10:05",
        "end_at": "2026-07-23T11:00", "reason": "修正开始时间",
    })
    audits = conn.execute(
        "SELECT * FROM computer_session_audits WHERE session_id=? ORDER BY id",
        (session["id"],),
    ).fetchall()
    assert [a["action"] for a in audits] == ["create", "correct"]
    assert audits[-1]["before_start"] == "2026-07-23T10:00"


def test_new_supervisor_pages_render(client):
    _sup(client)
    for path in (
        "/", "/activity", "/points", "/admin", "/admin/reviews",
        "/admin/points", "/admin/missions", "/admin/stages",
        "/computer", "/computer?tab=manage", "/admin/trash",
    ):
        response = client.get(path)
        assert response.status_code == 200, path
    assert client.get("/missions").headers["location"] == "/activity#missions"
    assert client.get("/computer/history").headers["location"] == "/computer"
    assert client.get("/admin/computer").headers["location"].startswith("/computer?tab=manage")


def test_supervisor_cannot_open_or_submit_checkin(client):
    _sup(client)
    assert client.get("/checkin").headers["location"] == "/"
    response = client.post("/checkin", data={"kind": "fixed", "task_id": "1"})
    assert response.status_code == 303 and response.headers["location"] == "/"
    assert client.app.state.conn.execute(
        "SELECT COUNT(*) AS n FROM checkins"
    ).fetchone()["n"] == 0


def test_daily_stage_ignores_forged_weekdays_and_requires_switch(client):
    _sup(client)
    client.post("/admin/stages", data={
        "name": "每日假期", "starts_on": "2020-01-01", "ends_on": "2035-12-31",
        "mode": "daily", "cutoff_hour": "4", "window_start_weekday": "99",
        "window_end_weekday": "-8", "points_goal": "30", "basic_minutes": "60",
        "reward_minutes": "30",
    })
    stage = client.app.state.conn.execute("SELECT * FROM stages").fetchone()
    assert stage["active"] == 0
    assert (stage["window_start_weekday"], stage["window_end_weekday"]) == (4, 0)
    client.post(f"/admin/stages/{stage['id']}/switch")
    stage = client.app.state.conn.execute("SELECT * FROM stages").fetchone()
    assert stage["active"] == 1 and stage["activated_at"]


def test_trash_checkin_hides_points_revokes_reward_and_restores(client):
    _sup(client)
    client.post("/admin/stages", data={
        "name": "测试阶段", "starts_on": "2020-01-01", "ends_on": "2035-12-31",
        "mode": "daily", "cutoff_hour": "4", "points_goal": "10",
        "basic_minutes": "60", "reward_minutes": "30",
    })
    conn = client.app.state.conn
    stage_id = conn.execute("SELECT id FROM stages").fetchone()["id"]
    client.post(f"/admin/stages/{stage_id}/switch")
    client.get("/logout")
    _login_checkin(client)
    client.post("/checkin", data={"kind": "fixed", "task_id": "3"})
    cid = conn.execute("SELECT id FROM checkins ORDER BY id DESC").fetchone()["id"]
    assert conn.execute("SELECT reward_unlocked FROM settlement_windows").fetchone()[0] == 1
    client.get("/logout")
    _sup(client)
    result = client.post(
        f"/admin/records/checkin/{cid}/trash",
        data={"reason": "测试记录", "return_to": "/points"},
    )
    assert result.headers["location"] == "/points"
    assert conn.execute("SELECT deleted_at FROM checkins WHERE id=?", (cid,)).fetchone()[0]
    assert conn.execute("SELECT deleted_at FROM point_ledger WHERE source_id=?", (str(cid),)).fetchone()[0]
    assert conn.execute("SELECT reward_unlocked FROM settlement_windows").fetchone()[0] == 0
    deletion_id = conn.execute("SELECT id FROM record_deletions ORDER BY id DESC").fetchone()[0]
    client.post(f"/admin/trash/{deletion_id}/restore")
    assert conn.execute("SELECT deleted_at FROM checkins WHERE id=?", (cid,)).fetchone()[0] is None
    assert conn.execute("SELECT reward_unlocked FROM settlement_windows").fetchone()[0] == 1


def test_system_ledger_cannot_be_deleted(client):
    _sup(client)
    conn = client.app.state.conn
    conn.execute(
        "INSERT INTO point_ledger (delta,entry_type,description,source_type,source_id,created_at) "
        "VALUES (-10,'settlement','系统兑换','settlement_window','999','2026-01-01T04:00:00')"
    )
    conn.commit()
    ledger_id = conn.execute("SELECT id FROM point_ledger").fetchone()[0]
    response = client.post(
        f"/admin/records/ledger/{ledger_id}/trash", data={"reason": "误删"},
    )
    assert response.headers["location"].startswith("/admin/trash?error=")
    assert conn.execute("SELECT deleted_at FROM point_ledger WHERE id=?", (ledger_id,)).fetchone()[0] is None


def test_feed_uses_logical_date_and_clamps_future(client):
    import datetime as dt
    from chores.main import _now
    from chores.lifecycle import logical_date

    _login_checkin(client)
    conn = client.app.state.conn
    day = logical_date(_now(), 4)
    today_at = dt.datetime.combine(day, dt.time(hour=5))
    yesterday_at = today_at - dt.timedelta(days=1)
    pid = conn.execute("SELECT id FROM periods WHERE ended_at IS NULL").fetchone()[0]
    conn.execute(
        "INSERT INTO checkins (kind,period_id,title,note,awarded_points,status,created_at) "
        "VALUES ('adhoc',?,'今天记录','ONLY_TODAY',1,'scored',?)",
        (pid, today_at.isoformat(timespec="seconds")),
    )
    conn.execute(
        "INSERT INTO checkins (kind,period_id,title,note,awarded_points,status,created_at) "
        "VALUES ('adhoc',?,'昨天记录','ONLY_YESTERDAY',1,'scored',?)",
        (pid, yesterday_at.isoformat(timespec="seconds")),
    )
    conn.commit()
    today_page = client.get(f"/activity?feed_date={day.isoformat()}")
    assert "ONLY_TODAY" in today_page.text and "ONLY_YESTERDAY" not in today_page.text
    yesterday = day - dt.timedelta(days=1)
    old_page = client.get(f"/activity?feed_date={yesterday.isoformat()}")
    assert "ONLY_YESTERDAY" in old_page.text and "ONLY_TODAY" not in old_page.text
    future_page = client.get("/activity?feed_date=2999-01-01")
    assert f'value="{day.isoformat()}"' in future_page.text


def test_supervisor_home_is_status_only_and_admin_has_real_management(client):
    _sup(client)
    home = client.get("/")
    assert "今日状态" in home.text
    assert "COMMAND CENTER" not in home.text
    assert "home-computer-actions" not in home.text
    assert 'href="/admin"' in home.text
    assert 'href="/admin/trash"' not in home.text
    admin = client.get("/admin")
    assert "COMMAND CENTER" in admin.text
    assert "待办审批" in admin.text and "回收站" in admin.text
    assert "退出登录" in admin.text


def test_pending_badges_combine_checkin_mission_and_exchange(client):
    import datetime as dt
    from chores.main import _now
    from chores import lifecycle

    _sup(client)
    now = _now()
    start = (now.date() - dt.timedelta(days=1)).isoformat()
    end = (now.date() + dt.timedelta(days=1)).isoformat()
    client.post("/admin/stages", data={
        "name": "兑换阶段", "starts_on": start, "ends_on": end,
        "mode": "daily", "cutoff_hour": "4", "points_goal": "100",
        "basic_minutes": "0", "reward_minutes": "0", "exchange_enabled": "1",
        "exchange_points_per_unit": "10", "exchange_minutes_per_unit": "30",
        "exchange_daily_limit_minutes": "60",
    })
    conn = client.app.state.conn
    sid = conn.execute("SELECT id FROM stages ORDER BY id DESC").fetchone()[0]
    client.post(f"/admin/stages/{sid}/switch")
    lifecycle.add_points(conn, 50, "earn", "测试积分", "test", "pending-badge", "系统",
                         now.isoformat(timespec="seconds"))
    conn.execute(
        "INSERT INTO missions (title,points,photo_required,status,published_by,created_at,submitted_at) "
        "VALUES ('待验收任务',10,0,'submitted','惠姐',?,?)",
        (now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds")),
    )
    conn.commit()
    client.get("/logout")
    _login_checkin(client)
    client.post("/checkin", data={"kind": "adhoc", "title": "待审核打卡", "proposed_points": "5"})
    result = client.post("/time-exchanges", data={"units": "1"})
    assert "message=" in result.headers["location"]
    client.get("/logout")
    _sup(client)
    home = client.get("/")
    assert "有 3 项等待处理" in home.text
    admin = client.get("/admin")
    assert "待验收任务" not in admin.text
    reviews = client.get("/admin/reviews")
    assert "待验收任务" in reviews.text
    assert "待审核打卡" in reviews.text
    assert "申请 30 分钟" in reviews.text


def test_exchange_approval_atomically_deducts_points_and_grants_time(client):
    import datetime as dt
    from chores.main import _now
    from chores import lifecycle

    _sup(client)
    now = _now()
    client.post("/admin/stages", data={
        "name": "兑换阶段", "starts_on": (now.date()-dt.timedelta(days=1)).isoformat(),
        "ends_on": (now.date()+dt.timedelta(days=1)).isoformat(), "mode": "daily",
        "cutoff_hour": "4", "points_goal": "100", "basic_minutes": "0",
        "reward_minutes": "0", "exchange_enabled": "1",
        "exchange_points_per_unit": "10", "exchange_minutes_per_unit": "30",
        "exchange_daily_limit_minutes": "60",
    })
    conn = client.app.state.conn
    sid = conn.execute("SELECT id FROM stages ORDER BY id DESC").fetchone()[0]
    client.post(f"/admin/stages/{sid}/switch")
    lifecycle.add_points(conn, 50, "earn", "兑换本金", "test", "exchange-funds", "系统",
                         now.isoformat(timespec="seconds"))
    conn.commit()
    client.get("/logout")
    _login_checkin(client)
    response = client.post("/time-exchanges", data={"units": "2"})
    assert response.status_code == 303
    request_row = conn.execute("SELECT * FROM time_exchange_requests").fetchone()
    assert request_row["status"] == "pending" and request_row["points_cost"] == 20
    assert lifecycle.ledger_balance(conn) == 50
    client.get("/logout")
    _sup(client)
    client.post(f"/admin/time-exchanges/{request_row['id']}/review", data={"action": "approve"})
    approved = conn.execute(
        "SELECT * FROM time_exchange_requests WHERE id=?", (request_row["id"],)
    ).fetchone()
    assert approved["status"] == "approved"
    assert approved["point_ledger_id"] and approved["time_grant_id"]
    assert lifecycle.ledger_balance(conn) == 30
    grant = conn.execute("SELECT * FROM time_grants WHERE id=?", (approved["time_grant_id"],)).fetchone()
    assert grant["minutes"] == 60 and grant["grant_type"] == "exchange"
    # 重复审批不产生第二套流水。
    client.post(f"/admin/time-exchanges/{request_row['id']}/review", data={"action": "approve"})
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM point_ledger WHERE source_type='time_exchange'"
    ).fetchone()["n"] == 1


def test_exchange_cannot_spend_points_locked_for_automatic_settlement(client):
    import datetime as dt
    from chores.main import _now
    from chores import lifecycle

    _sup(client)
    now = _now()
    client.post("/admin/stages", data={
        "name": "锁定积分阶段", "starts_on": (now.date()-dt.timedelta(days=1)).isoformat(),
        "ends_on": (now.date()+dt.timedelta(days=1)).isoformat(), "mode": "daily",
        "cutoff_hour": "4", "points_goal": "30", "basic_minutes": "0",
        "reward_minutes": "30", "exchange_enabled": "1",
        "exchange_points_per_unit": "10", "exchange_minutes_per_unit": "30",
        "exchange_daily_limit_minutes": "90",
    })
    conn = client.app.state.conn
    sid = conn.execute("SELECT id FROM stages ORDER BY id DESC").fetchone()[0]
    client.post(f"/admin/stages/{sid}/switch")
    lifecycle.add_points(conn, 40, "earn", "达标积分", "test", "locked-funds", "系统",
                         now.isoformat(timespec="seconds"))
    conn.commit()
    lifecycle.ensure_state(conn, now)
    client.get("/logout")
    _login_checkin(client)
    blocked = client.post("/time-exchanges", data={"units": "2"})
    assert "error=" in blocked.headers["location"]
    assert conn.execute("SELECT COUNT(*) AS n FROM time_exchange_requests").fetchone()["n"] == 0
    allowed = client.post("/time-exchanges", data={"units": "1"})
    assert "message=" in allowed.headers["location"]


def test_old_pending_exchange_expires_on_next_request(client):
    import datetime as dt
    from chores.main import _now
    from chores import lifecycle

    _sup(client)
    now = _now()
    client.post("/admin/stages", data={
        "name": "过期测试阶段", "starts_on": (now.date()-dt.timedelta(days=2)).isoformat(),
        "ends_on": (now.date()+dt.timedelta(days=1)).isoformat(), "mode": "daily",
        "cutoff_hour": "4", "points_goal": "100", "basic_minutes": "0",
        "reward_minutes": "0", "exchange_enabled": "1",
        "exchange_points_per_unit": "10", "exchange_minutes_per_unit": "30",
        "exchange_daily_limit_minutes": "60",
    })
    conn = client.app.state.conn
    sid = conn.execute("SELECT id FROM stages ORDER BY id DESC").fetchone()[0]
    client.post(f"/admin/stages/{sid}/switch")
    old_day = (lifecycle.logical_date(now, 4) - dt.timedelta(days=1)).isoformat()
    conn.execute(
        "INSERT INTO time_exchange_requests (stage_id,logical_date,requester_name,units,"
        "points_per_unit,minutes_per_unit,points_cost,minutes_requested,status,created_at) "
        "VALUES (?,?, '浩哥',1,10,30,10,30,'pending',?)",
        (sid, old_day, (now-dt.timedelta(days=1)).isoformat(timespec="seconds")),
    )
    conn.commit()
    client.get("/")
    row = conn.execute("SELECT * FROM time_exchange_requests").fetchone()
    assert row["status"] == "expired"
