import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone

from app.models import DemoLink

TOKEN_ID_RE = re.compile(r"^[a-f0-9]{16}$")
TOKEN_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


def sha256(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def create_demo_token() -> dict:
    link_id = secrets.token_hex(8)
    secret = secrets.token_urlsafe(32)
    token = f"{link_id}.{secret}"
    return {"id": link_id, "token": token, "tokenHash": sha256(token)}


def parse_demo_token(token: str) -> dict | None:
    parts = str(token or "").split(".")
    if len(parts) != 2 or not TOKEN_ID_RE.match(parts[0]) or not TOKEN_SECRET_RE.match(parts[1]):
        return None
    full = f"{parts[0]}.{parts[1]}"
    return {"id": parts[0], "tokenHash": sha256(full)}


def safe_hash_equal(left: str, right: str) -> bool:
    try:
        a = bytes.fromhex(str(left))
        b = bytes.fromhex(str(right))
        return len(a) == 32 and len(b) == 32 and hmac.compare_digest(a, b)
    except ValueError:
        return False


def demo_link_status(link: DemoLink, now: datetime | None = None) -> str:
    if not link or link.status == "revoked":
        return "revoked"
    moment = now or datetime.now(timezone.utc)
    expires = link.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= moment:
        return "expired"
    if int(link.starts or 0) >= int(link.max_starts or 0):
        return "exhausted"
    return "active"


def clamp_integer(value, fallback: int, minimum: int, maximum: int) -> int:
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return fallback
    return min(maximum, max(minimum, number))


def normalize_demo_limits(body: dict | None, now: datetime | None = None) -> dict:
    moment = now or datetime.now(timezone.utc)
    days = clamp_integer((body or {}).get("expiresInDays"), 7, 1, 30)
    return {
        "expires_at": moment + timedelta(days=days),
        "max_session_seconds": clamp_integer((body or {}).get("maxSessionSeconds"), 300, 60, 600),
        "max_starts": clamp_integer((body or {}).get("maxStarts"), 25, 1, 1000),
    }


def public_demo_link(link: DemoLink, now: datetime | None = None) -> dict:
    return {
        "id": link.id,
        "agentId": link.agent_id,
        "label": link.label,
        "status": demo_link_status(link, now),
        "expiresAt": link.expires_at.isoformat(),
        "maxSessionSeconds": link.max_session_seconds,
        "maxStarts": link.max_starts,
        "starts": int(link.starts or 0),
        "createdAt": link.created_at.isoformat(),
    }
