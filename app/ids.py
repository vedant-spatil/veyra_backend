import hashlib
import secrets
from datetime import datetime, timezone


def gen_id(prefix: str) -> str:
    return prefix + secrets.token_hex(8)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()
