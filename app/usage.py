from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.ids import gen_id
from app.models import Usage

INR_PER_1K_CHARS = 0.12
INR_PER_CALL = 0.9
INR_PER_1K_TOKENS = 0.01


def usage_cost_inr(chars: int, calls: int, llm_tokens: int) -> float:
    raw = (
        (chars or 0) / 1000 * INR_PER_1K_CHARS
        + (calls or 0) * INR_PER_CALL
        + (llm_tokens or 0) / 1000 * INR_PER_1K_TOKENS
    )
    return round(raw * 100) / 100


def bump_usage(db: Session, tenant_id: str, field: str, amount: int) -> None:
    day = datetime.now(timezone.utc).date().isoformat()
    row = db.query(Usage).filter_by(tenant_id=tenant_id, day=day).one_or_none()
    if row is None:
        row = Usage(id=gen_id("use_"), tenant_id=tenant_id, day=day, chars=0, calls=0, llm_tokens=0)
        db.add(row)
    current = getattr(row, field)
    setattr(row, field, int(current or 0) + amount)
