from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy.orm import Session

from app.dograh import WORKFLOW_ID, DograhError, dograh
from app.ids import gen_id, iso
from app.models import Call, ProviderCharge, ProviderSync, Usage, User
from app.settings import get_settings
from app.usage import usage_cost_inr

STALE_AFTER = timedelta(hours=24)
PROVIDERS = {
    "dograh": "webhook",
    "vobiz": "webhook",
    "rumik": "local",
    "groq": "local",
}


def ensure_providers(db: Session) -> dict[str, ProviderSync]:
    rows = {}
    for provider, mode in PROVIDERS.items():
        row = db.get(ProviderSync, provider)
        if row is None:
            row = ProviderSync(provider=provider, mode=mode, last_synced_at=None)
            db.add(row)
            db.commit()
            db.refresh(row)
        rows[provider] = row
    return rows


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def is_stale(row: ProviderSync, now: datetime) -> bool:
    if row.mode == "local":
        return False
    synced = _aware(row.last_synced_at)
    if synced is None:
        return True
    return now - synced >= STALE_AFTER


def mark_synced(db: Session, provider: str, when: datetime | None = None) -> None:
    row = db.get(ProviderSync, provider)
    if row is None:
        row = ProviderSync(provider=provider, mode=PROVIDERS.get(provider, "local"))
        db.add(row)
    row.last_synced_at = when or datetime.now(timezone.utc)
    db.commit()


def money_to_paise(value) -> int:
    if isinstance(value, dict):
        for key in ("total_cost", "total_cost_inr", "cost", "amount", "amount_inr"):
            if value.get(key) not in (None, ""):
                return money_to_paise(value.get(key))
        return 0
    try:
        return int(round(float(value) * 100))
    except (TypeError, ValueError):
        return 0


def _digits(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def tenant_for_phone(db: Session, number: str) -> str | None:
    needle = _digits(number)
    if len(needle) < 8:
        return None
    tail = needle[-10:]
    match = None
    for call in db.query(Call).order_by(Call.created_at.desc()).all():
        if _digits(call.to_number)[-10:] == tail:
            match = call
            break
    return match.tenant_id if match else None


def upsert_charge(
    db: Session,
    provider: str,
    external_id: str,
    tenant_id: str | None,
    charged_paise: int,
    raw: dict,
    overwrite_amount: bool,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
) -> None:
    if not external_id:
        return
    existing = (
        db.query(ProviderCharge)
        .filter(ProviderCharge.provider == provider, ProviderCharge.external_id == external_id)
        .one_or_none()
    )
    now = datetime.now(timezone.utc)
    if existing:
        if overwrite_amount:
            existing.charged_paise = charged_paise
            existing.raw = raw or {}
            existing.synced_at = now
            if tenant_id and not existing.tenant_id:
                existing.tenant_id = tenant_id
            if period_start:
                existing.period_start = period_start
            if period_end:
                existing.period_end = period_end
            db.commit()
        return
    db.add(ProviderCharge(
        id=gen_id("chg_"),
        provider=provider,
        external_id=external_id,
        tenant_id=tenant_id,
        charged_paise=charged_paise,
        period_start=period_start,
        period_end=period_end,
        synced_at=now,
        raw=raw or {},
    ))
    db.commit()


def record_dograh_cost(db: Session, run_id: str, raw: dict, tenant_id: str | None = None) -> None:
    cost = {}
    if isinstance(raw, dict):
        cost = raw.get("cost_info") if isinstance(raw.get("cost_info"), dict) else raw
    if tenant_id is None and run_id:
        call = db.query(Call).filter(Call.dograh_run_id == run_id).one_or_none()
        tenant_id = call.tenant_id if call else None
    upsert_charge(db, "dograh", run_id, tenant_id, money_to_paise(cost), raw if isinstance(raw, dict) else {}, True)


def sync_dograh(db: Session) -> None:
    calls = db.query(Call).filter(Call.dograh_run_id != "").all()
    for call in calls:
        existing = (
            db.query(ProviderCharge)
            .filter(ProviderCharge.provider == "dograh", ProviderCharge.external_id == call.dograh_run_id)
            .one_or_none()
        )
        if existing:
            continue
        fetched = dograh.fetch_run(call.dograh_run_id)
        record_dograh_cost(db, call.dograh_run_id, fetched.get("raw") or {}, call.tenant_id)
    mark_synced(db, "dograh")


def _vobiz_rows(payload) -> list[dict]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("objects", "cdrs", "calls", "data", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def sync_vobiz(db: Session, since: datetime | None) -> None:
    settings = get_settings()
    if not settings.vobiz_auth_id or not settings.vobiz_auth_token:
        return
    start = (since or datetime.now(timezone.utc) - STALE_AFTER).date().isoformat()
    url = f"https://api.vobiz.ai/api/v1/Account/{settings.vobiz_auth_id}/Call/"
    with httpx.Client(timeout=30) as client:
        response = client.get(
            url,
            params={"start_date": start},
            headers={
                "X-Auth-ID": settings.vobiz_auth_id,
                "X-Auth-Token": settings.vobiz_auth_token,
            },
        )
    if response.status_code >= 400:
        raise RuntimeError(f"Vobiz CDR request failed ({response.status_code})")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("Vobiz CDR response was not JSON") from exc
    for row in _vobiz_rows(payload):
        external_id = str(row.get("call_uuid") or row.get("CallUUID") or row.get("uuid") or "")
        number = str(row.get("to") or row.get("To") or row.get("to_number") or "")
        cost = row.get("total_cost")
        if cost is None:
            cost = row.get("totalCost")
        upsert_charge(
            db,
            "vobiz",
            external_id,
            tenant_for_phone(db, number),
            money_to_paise(cost),
            row,
            True,
        )
    mark_synced(db, "vobiz")


def sync_if_stale(db: Session) -> None:
    now = datetime.now(timezone.utc)
    rows = ensure_providers(db)
    if is_stale(rows["dograh"], now):
        try:
            sync_dograh(db)
        except (DograhError, httpx.HTTPError, RuntimeError):
            db.rollback()
    rows = ensure_providers(db)
    if is_stale(rows["vobiz"], now):
        try:
            sync_vobiz(db, _aware(rows["vobiz"].last_synced_at))
        except (httpx.HTTPError, RuntimeError):
            db.rollback()


def _expected_inr(db: Session, tenant_id: str) -> float:
    total = 0.0
    for row in db.query(Usage).filter(Usage.tenant_id == tenant_id).all():
        total = round((total + usage_cost_inr(row.chars, row.calls, row.llm_tokens)) * 100) / 100
    return total


def _charged_paise(db: Session, tenant_id: str | None = None, allocated_only: bool = False) -> int:
    query = db.query(ProviderCharge)
    if tenant_id is not None:
        query = query.filter(ProviderCharge.tenant_id == tenant_id)
    elif allocated_only:
        query = query.filter(ProviderCharge.tenant_id.is_not(None))
    return int(sum(row.charged_paise or 0 for row in query.all()))


def customer_rows(db: Session) -> list[dict]:
    providers = ensure_providers(db)
    customers = db.query(User).filter(User.role == "customer").order_by(User.email.asc()).all()
    rows = []
    for user in customers:
        charges = db.query(ProviderCharge).filter(ProviderCharge.tenant_id == user.tenant_id).all()
        used = {row.provider for row in charges}
        synced = [
            _aware(providers[name].last_synced_at)
            for name in used
            if name in providers and providers[name].last_synced_at is not None
        ]
        latest = max(synced) if synced else None
        rows.append({
            "tenantId": user.tenant_id,
            "name": user.name,
            "email": user.email,
            "chargedInr": round(_charged_paise(db, user.tenant_id) / 100, 2),
            "expectedInr": _expected_inr(db, user.tenant_id),
            "lastSyncedAt": iso(latest),
        })
    return rows


def admin_summary(db: Session) -> dict:
    sync_if_stale(db)
    providers = ensure_providers(db)
    customers = customer_rows(db)
    return {
        "chargedInr": round(_charged_paise(db) / 100, 2),
        "expectedInr": round(sum(row["expectedInr"] for row in customers) * 100) / 100,
        "providers": [
            {"provider": name, "mode": row.mode, "lastSyncedAt": iso(_aware(row.last_synced_at))}
            for name, row in providers.items()
        ],
        "customers": customers,
        "webhookUrls": {
            "dograh": "/api/billing/hooks/dograh",
            "vobiz": "/api/billing/hooks/vobiz",
        },
        "workflowId": WORKFLOW_ID,
    }


def customer_detail(db: Session, tenant_id: str) -> dict | None:
    user = db.query(User).filter(User.tenant_id == tenant_id, User.role == "customer").one_or_none()
    if user is None:
        return None
    charges = (
        db.query(ProviderCharge)
        .filter(ProviderCharge.tenant_id == tenant_id)
        .order_by(ProviderCharge.synced_at.desc())
        .all()
    )
    days = []
    for row in db.query(Usage).filter(Usage.tenant_id == tenant_id).order_by(Usage.day.asc()).all():
        days.append({
            "day": row.day,
            "chars": row.chars,
            "calls": row.calls,
            "llmTokens": row.llm_tokens,
            "costInr": usage_cost_inr(row.chars, row.calls, row.llm_tokens),
        })
    return {
        "customer": {
            "tenantId": user.tenant_id,
            "name": user.name,
            "email": user.email,
            "chargedInr": round(_charged_paise(db, tenant_id) / 100, 2),
            "expectedInr": _expected_inr(db, tenant_id),
        },
        "charges": [
            {
                "id": row.id,
                "provider": row.provider,
                "externalId": row.external_id,
                "chargedInr": round((row.charged_paise or 0) / 100, 2),
                "syncedAt": iso(_aware(row.synced_at)),
            }
            for row in charges
        ],
        "days": days,
    }
