from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth import (
    create_session,
    email_allowed,
    exchange_code,
    google_authorize_url,
    issue_jwt,
    provision_user,
    state_ok,
)
from app.db import get_db
from app.deps import require_user
from app.models import Session as SessionRow
from app.ids import sha256_hex
from app.serialize import public_tenant, public_user
from app.settings import get_settings

router = APIRouter()


@router.get("/api/auth/google")
def google_start():
    settings = get_settings()
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(status_code=503, detail={"error": "Google sign-in is not configured", "code": "google_unavailable"})
    return RedirectResponse(google_authorize_url())


@router.get("/api/auth/google/callback")
def google_callback(code: str = "", state: str = "", db: Session = Depends(get_db)):
    if not state_ok(state):
        raise HTTPException(status_code=400, detail={"error": "invalid oauth state", "code": "bad_state"})
    if not code:
        raise HTTPException(status_code=400, detail={"error": "missing code", "code": "bad_code"})
    try:
        profile = exchange_code(code)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail={"error": str(exc), "code": "google_failed"}) from exc
    if not profile.get("email_verified"):
        raise HTTPException(status_code=403, detail={"error": "Google email is not verified", "code": "email_unverified"})
    if not email_allowed(db, profile["email"]):
        raise HTTPException(status_code=403, detail={"error": "this email is not on the allowlist", "code": "not_allowlisted"})
    tenant, user = provision_user(db, profile["email"], profile.get("name") or "Owner", profile.get("sub") or "")
    if user.status != "active" or tenant.status != "active":
        raise HTTPException(status_code=403, detail={"error": "account is not active", "code": "forbidden"})
    token = create_session(db, user)
    access = issue_jwt(user)
    origin = get_settings().cors_origins[0] if get_settings().cors_origins else get_settings().public_base_url
    response = RedirectResponse(f"{origin.rstrip('/')}/#token={quote(access)}", status_code=302)
    response.set_cookie(
        "veyra_sess",
        token,
        httponly=True,
        samesite="lax",
        secure=get_settings().cookie_secure,
        max_age=7 * 24 * 3600,
        path="/",
    )
    return response


@router.post("/api/auth/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    cookie = request.cookies.get("veyra_sess")
    if cookie:
        row = db.get(SessionRow, sha256_hex(cookie))
        if row:
            db.delete(row)
            db.commit()
    return {"ok": True}


@router.get("/api/me")
def me(principal=Depends(require_user)):
    user, tenant = principal
    return {"user": public_user(user), "tenant": public_tenant(tenant)}
