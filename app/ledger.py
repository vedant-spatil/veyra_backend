from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.ids import gen_id
from app.models import AuditEvent, Ledger, Wallet


def add_ledger(
    db: Session,
    tenant_id: str,
    amount_paise: int,
    entry_type: str,
    reference: str,
    actor_user_id: str,
    metadata: dict | None = None,
) -> Ledger | None:
    if reference:
        existing = db.query(Ledger).filter_by(tenant_id=tenant_id, idempotency_key=reference).one_or_none()
        if existing:
            return None
    wallet = db.query(Wallet).filter_by(tenant_id=tenant_id).with_for_update().one_or_none()
    now = datetime.now(timezone.utc)
    if wallet is None:
        wallet = Wallet(id=gen_id("wal_"), tenant_id=tenant_id, currency="INR", balance_paise=0, created_at=now, updated_at=now)
        db.add(wallet)
        db.flush()
    if not isinstance(amount_paise, int) or wallet.balance_paise + amount_paise < 0:
        raise ValueError("invalid wallet adjustment")
    wallet.balance_paise += amount_paise
    wallet.updated_at = now
    entry = Ledger(
        id=gen_id("led_"),
        tenant_id=tenant_id,
        entry_type=entry_type,
        amount_paise=amount_paise,
        balance_after_paise=wallet.balance_paise,
        idempotency_key=reference or gen_id("idem_"),
        actor_user_id=actor_user_id or "",
        meta=metadata or {},
        created_at=now,
    )
    db.add(entry)
    return entry


def add_audit(db: Session, tenant_id: str, actor_user_id: str, action: str, target_type: str, target_id: str, metadata: dict | None = None) -> None:
    db.add(AuditEvent(
        id=gen_id("aud_"),
        tenant_id=tenant_id,
        actor_user_id=actor_user_id or "",
        action=action,
        target_type=target_type,
        target_id=target_id,
        meta=metadata or {},
    ))
