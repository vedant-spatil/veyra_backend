import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
import jwt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ids import gen_id, sha256_hex
from app.models import AllowedEmail, Session as SessionRow, Tenant, User, Wallet
from app.settings import get_settings

ROLE_LEVEL = {"member": 1, "owner": 2, "admin": 3, "super_admin": 4}
SESSION_DAYS = 7
JWT_MINUTES = 20


def serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="veyra-oauth")


def sign_state() -> str:
    return serializer().dumps({"purpose": "google"})


def read_state(value: str) -> dict:
    return serializer().loads(value, max_age=600)


def email_allowed(db: Session, email: str) -> bool:
    normalized = email.strip().lower()
    if normalized in get_settings().allowed_email_set:
        return True
    return db.get(AllowedEmail, normalized) is not None


def slugify(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "workspace"
    slug = base
    index = 2
    while slug in taken:
        slug = f"{base}-{index}"
        index += 1
    return slug


def provision_user(db: Session, email: str, name: str, google_sub: str = "") -> tuple[Tenant, User]:
    normalized = email.strip().lower()
    user = db.scalar(select(User).where(User.email == normalized))
    if user:
        if google_sub and not user.google_sub:
            user.google_sub = google_sub
        db.commit()
        tenant = db.get(Tenant, user.tenant_id)
        return tenant, user
    taken = {row for row in db.scalars(select(Tenant.slug))}
    tenant = Tenant(
        id=gen_id("t_"),
        name=name.strip()[:80] or "Veyra",
        slug=slugify(name or normalized.split("@")[0], taken),
        branding={"color": "#B88A2D"},
        plan="studio",
        status="active",
        privacy_mode="standard",
    )
    user = User(
        id=gen_id("u_"),
        tenant_id=tenant.id,
        email=normalized,
        name=(name or "Owner").strip()[:80] or "Owner",
        role="owner",
        status="active",
        google_sub=google_sub or "",
    )
    wallet = Wallet(id=gen_id("wal_"), tenant_id=tenant.id, currency="INR", balance_paise=0)
    db.add(tenant)
    db.add(user)
    db.add(wallet)
    db.commit()
    db.refresh(tenant)
    db.refresh(user)
    return tenant, user


def create_session(db: Session, user: User) -> str:
    token = gen_id("") + gen_id("")
    row = SessionRow(
        token_hash=sha256_hex(token),
        user_id=user.id,
        tenant_id=user.tenant_id,
        exp=datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS),
    )
    db.add(row)
    db.commit()
    return token


def issue_jwt(user: User) -> str:
    payload = {
        "sub": user.id,
        "tid": user.tenant_id,
        "email": user.email,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=JWT_MINUTES),
    }
    return jwt.encode(payload, get_settings().secret_key, algorithm="HS256")


def user_from_jwt(db: Session, token: str) -> tuple[User, Tenant] | None:
    try:
        payload = jwt.decode(token, get_settings().secret_key, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    user = db.get(User, payload.get("sub"))
    tenant = db.get(Tenant, payload.get("tid")) if user else None
    if not user or not tenant or user.tenant_id != tenant.id:
        return None
    return user, tenant


def user_from_cookie(db: Session, token: str) -> tuple[User, Tenant] | None:
    row = db.get(SessionRow, sha256_hex(token))
    if not row:
        return None
    exp = row.exp if row.exp.tzinfo else row.exp.replace(tzinfo=timezone.utc)
    if exp <= datetime.now(timezone.utc):
        return None
    user = db.get(User, row.user_id)
    tenant = db.get(Tenant, row.tenant_id)
    if not user or not tenant:
        return None
    return user, tenant


def google_redirect_uri() -> str:
    return get_settings().public_base_url.rstrip("/") + "/api/auth/google/callback"


def google_authorize_url() -> str:
    settings = get_settings()
    query = urlencode({
        "client_id": settings.google_client_id,
        "redirect_uri": google_redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": sign_state(),
        "prompt": "select_account",
    })
    return "https://accounts.google.com/o/oauth2/v2/auth?" + query


def exchange_code(code: str) -> dict:
    settings = get_settings()
    token_response = httpx.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri": google_redirect_uri(),
            "grant_type": "authorization_code",
        },
        timeout=15,
    )
    if token_response.status_code >= 400:
        raise ValueError("google token exchange failed")
    access = token_response.json().get("access_token")
    if not access:
        raise ValueError("google token exchange failed")
    info = httpx.get(
        "https://openidconnect.googleapis.com/v1/userinfo",
        headers={"Authorization": f"Bearer {access}"},
        timeout=15,
    )
    if info.status_code >= 400:
        raise ValueError("google profile failed")
    profile = info.json()
    verified = profile.get("email_verified")
    if isinstance(verified, str):
        verified = verified.lower() == "true"
    return {
        "email": str(profile.get("email") or "").lower(),
        "email_verified": bool(verified),
        "name": str(profile.get("name") or ""),
        "sub": str(profile.get("sub") or ""),
    }


def state_ok(value: str | None) -> bool:
    if not value:
        return False
    try:
        read_state(value)
        return True
    except (BadSignature, SignatureExpired):
        return False
