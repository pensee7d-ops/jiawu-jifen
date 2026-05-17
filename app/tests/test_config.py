from chores.config import Config


def test_config_reads_env(monkeypatch):
    monkeypatch.setenv("CHORES_SECRET_KEY", "sek")
    monkeypatch.setenv("CHORES_CHECKIN_PASSWORD", "cp")
    monkeypatch.setenv("CHORES_SUPERVISOR_PASSWORD", "sp")
    monkeypatch.setenv("CHORES_WEEKLY_GOAL", "250")
    cfg = Config.from_env()
    assert cfg.secret_key == "sek"
    assert cfg.checkin_password == "cp"
    assert cfg.supervisor_password == "sp"
    assert cfg.weekly_goal == 250
