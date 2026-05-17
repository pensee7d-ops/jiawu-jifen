import hmac
from fastapi import Request, HTTPException

ROLE_CHECKIN = "checkin"
ROLE_SUPERVISOR = "supervisor"


def verify_password(given: str, supervisor_pw: str, checkin_pw: str):
    if hmac.compare_digest(given, supervisor_pw):
        return ROLE_SUPERVISOR
    if hmac.compare_digest(given, checkin_pw):
        return ROLE_CHECKIN
    return None


def current_role(request: Request):
    return request.session.get("role")


def current_name(request: Request):
    return request.session.get("name")


def require_login(request: Request):
    role = current_role(request)
    if role not in (ROLE_CHECKIN, ROLE_SUPERVISOR):
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return role


def require_supervisor(request: Request):
    if current_role(request) != ROLE_SUPERVISOR:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return ROLE_SUPERVISOR
