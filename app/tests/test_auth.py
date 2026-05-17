from chores.auth import verify_password, ROLE_CHECKIN, ROLE_SUPERVISOR


def test_verify_checkin_password():
    role = verify_password("didi", supervisor_pw="jia", checkin_pw="didi")
    assert role == ROLE_CHECKIN


def test_verify_supervisor_password():
    role = verify_password("jia", supervisor_pw="jia", checkin_pw="didi")
    assert role == ROLE_SUPERVISOR


def test_verify_wrong_password_returns_none():
    assert verify_password("nope", supervisor_pw="jia", checkin_pw="didi") is None


def test_supervisor_password_wins_if_equal_precedence():
    # 监管者口令优先判定
    role = verify_password("same", supervisor_pw="same", checkin_pw="same")
    assert role == ROLE_SUPERVISOR
