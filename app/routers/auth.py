import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import SIGNUP_CREDITS, create_session, issue_jwt, provision_user, signup_email_ok
from app.db import get_db
from app.deps import require_user
from app.ledger import add_ledger
from app.mailjet import MailjetError, send_signup_code
from app.models import PendingSignup
from app.models import Session as SessionRow
from app.models import Tenant, User
from app.ids import sha256_hex
from app.passwords import hash_password, verify_password
from app.serialize import public_tenant, public_user
from app.settings import get_settings

router = APIRouter()

# Google sign-in is turned off. The handlers stay here so they can be restored.
#
# @router.get("/api/auth/google")
# def google_start():
#     settings = get_settings()
#     if not settings.google_client_id or not settings.google_client_secret:
#         raise HTTPException(status_code=503, detail={"error": "Google sign-in is not configured", "code": "google_unavailable"})
#     return RedirectResponse(google_authorize_url())
#
# @router.get("/api/auth/google/callback")
# def google_callback(code: str = "", state: str = "", db: Session = Depends(get_db)):
#     if not state_ok(state):
#         raise HTTPException(status_code=400, detail={"error": "invalid oauth state", "code": "bad_state"})
#     if not code:
#         raise HTTPException(status_code=400, detail={"error": "missing code", "code": "bad_code"})
#     try:
#         profile = exchange_code(code)
#     except ValueError as exc:
#         raise HTTPException(status_code=502, detail={"error": str(exc), "code": "google_failed"}) from exc
#     if not profile.get("email_verified"):
#         raise HTTPException(status_code=403, detail={"error": "Google email is not verified", "code": "email_unverified"})
#     if not email_allowed(db, profile["email"]):
#         raise HTTPException(status_code=403, detail={"error": "this email is not on the allowlist", "code": "not_allowlisted"})
#     tenant, user = provision_user(db, profile["email"], profile.get("name") or "Owner", profile.get("sub") or "")
#     if user.status != "active" or tenant.status != "active":
#         raise HTTPException(status_code=403, detail={"error": "account is not active", "code": "forbidden"})
#     token = create_session(db, user)
#     access = issue_jwt(user)
#     origin = get_settings().cors_origins[0] if get_settings().cors_origins else get_settings().public_base_url
#     response = RedirectResponse(f"{origin.rstrip('/')}/#token={quote(access)}", status_code=302)
#     response.set_cookie(
#         "veyra_sess",
#         token,
#         httponly=True,
#         samesite="lax",
#         secure=get_settings().cookie_secure,
#         max_age=7 * 24 * 3600,
#         path="/",
#     )
#     return response


def _session_cookie(response: JSONResponse, token: str) -> None:
    response.set_cookie(
        "veyra_sess",
        token,
        httponly=True,
        samesite="lax",
        secure=get_settings().cookie_secure,
        max_age=7 * 24 * 3600,
        path="/",
    )


def _signed_in(db: Session, user: User, tenant: Tenant, status_code: int = 200) -> JSONResponse:
    token = create_session(db, user)
    response = JSONResponse({
        "token": issue_jwt(user),
        "user": public_user(user),
        "tenant": public_tenant(tenant),
    }, status_code=status_code)
    _session_cookie(response, token)
    return response


def _code_hash(code: str) -> str:
    return hmac.new(get_settings().secret_key.encode(), code.encode(), hashlib.sha256).hexdigest()


def _email_taken(db: Session, email: str) -> bool:
    return db.scalar(select(User.id).where(func.lower(User.email) == email)) is not None


def _signup_fields(body: dict) -> tuple[str, str, str]:
    email = str(body.get("email") or "").strip().lower()
    password = str(body.get("password") or "")
    name = str(body.get("name") or "").strip()
    if not signup_email_ok(email):
        raise HTTPException(status_code=422, detail={"error": "use a gmail.com, outlook.com, or hotmail.com address", "code": "bad_email"})
    if not password:
        raise HTTPException(status_code=422, detail={"error": "password is required", "code": "bad_password"})
    if not name:
        raise HTTPException(status_code=422, detail={"error": "name is required", "code": "bad_name"})
    return email, password, name


def _pending_or_401(db: Session, email: str, code: str) -> PendingSignup:
    pending = db.get(PendingSignup, email)
    exp = pending.exp if pending and pending.exp.tzinfo else (pending.exp.replace(tzinfo=timezone.utc) if pending else None)
    if pending is None or exp <= datetime.now(timezone.utc) or not hmac.compare_digest(pending.code_hash, _code_hash(code)):
        raise HTTPException(status_code=401, detail={"error": "that code is not valid", "code": "bad_code"})
    return pending


@router.post("/api/auth/signup", status_code=202)
def signup(body: dict, db: Session = Depends(get_db)):
    email, password, name = _signup_fields(body)
    if _email_taken(db, email):
        raise HTTPException(status_code=409, detail={"error": "an account with this email already exists", "code": "email_taken"})
    code = f"{secrets.randbelow(1000000):06d}"
    now = datetime.now(timezone.utc)
    pending = db.get(PendingSignup, email)
    if pending is None:
        pending = PendingSignup(email=email, name=name[:80], pass_hash="", code_hash="", exp=now, created_at=now)
        db.add(pending)
    pending.name = name[:80]
    pending.pass_hash = hash_password(password)
    pending.code_hash = _code_hash(code)
    pending.exp = now + timedelta(minutes=10)
    db.commit()
    try:
        send_signup_code(email, code)
    except MailjetError as exc:
        db.delete(pending)
        db.commit()
        raise HTTPException(status_code=503, detail={"error": "the sign-up code could not be sent", "code": "mail_failed"}) from exc
    return {"sent": True, "email": email}


@router.post("/api/auth/signup/verify", status_code=201)
def verify_signup(body: dict, db: Session = Depends(get_db)):
    email = str(body.get("email") or "").strip().lower()
    code = str(body.get("code") or "").strip()
    pending = _pending_or_401(db, email, code)
    if _email_taken(db, email):
        db.delete(pending)
        db.commit()
        raise HTTPException(status_code=409, detail={"error": "an account with this email already exists", "code": "email_taken"})
    tenant, user = provision_user(db, email, pending.name)
    user.role = "customer"
    user.pass_hash = pending.pass_hash
    add_ledger(db, tenant.id, SIGNUP_CREDITS, "signup_credit", f"signup:{user.id}", user.id, {})
    db.delete(pending)
    db.commit()
    return _signed_in(db, user, tenant, status_code=201)


@router.post("/api/auth/login")
def login(body: dict, db: Session = Depends(get_db)):
    email = str(body.get("username") or body.get("email") or "").strip().lower()
    password = str(body.get("password") or "")
    user = db.scalar(select(User).where(User.email == email)) if email else None
    if user is None or not verify_password(password, user.pass_hash):
        raise HTTPException(status_code=401, detail={"error": "incorrect username or password", "code": "bad_login"})
    tenant = db.get(Tenant, user.tenant_id)
    if tenant is None or user.status != "active" or tenant.status != "active":
        raise HTTPException(status_code=403, detail={"error": "account is not active", "code": "forbidden"})
    return _signed_in(db, user, tenant)


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
