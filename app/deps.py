from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.auth import ROLE_LEVEL, user_from_cookie, user_from_jwt
from app.db import get_db
from app.models import Tenant, User


def load_principal(request: Request, db: Session) -> tuple[User, Tenant] | None:
    cookie = request.cookies.get("veyra_sess")
    if cookie:
        found = user_from_cookie(db, cookie)
        if found:
            return found
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return user_from_jwt(db, header[7:].strip())
    return None


def require_user(request: Request, db: Session = Depends(get_db)) -> tuple[User, Tenant]:
    found = load_principal(request, db)
    if not found:
        raise HTTPException(status_code=401, detail={"error": "authentication required", "code": "no_session"})
    user, tenant = found
    if user.status != "active" or tenant.status != "active":
        raise HTTPException(status_code=403, detail={"error": "account is not active", "code": "forbidden"})
    return user, tenant


def require_owner(principal: tuple[User, Tenant] = Depends(require_user)) -> tuple[User, Tenant]:
    user, tenant = principal
    if ROLE_LEVEL.get(user.role, 0) < ROLE_LEVEL["owner"]:
        raise HTTPException(status_code=403, detail={"error": "insufficient role", "code": "forbidden"})
    return user, tenant


def require_admin(principal: tuple[User, Tenant] = Depends(require_user)) -> tuple[User, Tenant]:
    user, tenant = principal
    if ROLE_LEVEL.get(user.role, 0) < ROLE_LEVEL["admin"]:
        raise HTTPException(status_code=403, detail={"error": "insufficient role", "code": "forbidden"})
    return user, tenant
