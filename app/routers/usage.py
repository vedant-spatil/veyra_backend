from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_user
from app.models import Usage

router = APIRouter()

INR_PER_1K_CHARS = 0.12
INR_PER_CALL = 0.9


@router.get("/api/usage")
def usage(principal=Depends(require_user), db: Session = Depends(get_db)):
    _user, tenant = principal
    rows = db.query(Usage).filter(Usage.tenant_id == tenant.id).order_by(Usage.day.asc()).all()
    days = []
    for row in rows:
        cost = round((row.chars / 1000 * INR_PER_1K_CHARS + row.calls * INR_PER_CALL) * 100) / 100
        days.append({"day": row.day, "chars": row.chars, "calls": row.calls, "llmTokens": row.llm_tokens, "costInr": cost})
    totals = {"chars": 0, "calls": 0, "llmTokens": 0, "costInr": 0.0}
    for day in days:
        totals["chars"] += day["chars"]
        totals["calls"] += day["calls"]
        totals["llmTokens"] += day["llmTokens"]
        totals["costInr"] = round((totals["costInr"] + day["costInr"]) * 100) / 100
    return {"days": days, "totals": totals}
