from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.billing_sync import (
    admin_summary,
    customer_detail,
    mark_synced,
    money_to_paise,
    record_dograh_cost,
    tenant_for_phone,
    upsert_charge,
)
from app.db import get_db
from app.deps import require_admin

router = APIRouter()


@router.get("/api/admin/billing")
def admin_billing(_principal=Depends(require_admin), db: Session = Depends(get_db)):
    return admin_summary(db)


@router.get("/api/admin/billing/customers/{tenant_id}")
def admin_customer(tenant_id: str, _principal=Depends(require_admin), db: Session = Depends(get_db)):
    detail = customer_detail(db, tenant_id)
    if detail is None:
        raise HTTPException(status_code=404, detail={"error": "customer not found", "code": "not_found"})
    return detail


@router.post("/api/billing/hooks/dograh")
async def dograh_hook(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    if not isinstance(body, dict):
        body = {}
    run_id = str(body.get("workflow_run_id") or body.get("run_id") or "")
    if not run_id:
        raise HTTPException(status_code=400, detail={"error": "workflow_run_id is required", "code": "bad_payload"})
    record_dograh_cost(db, run_id, body)
    mark_synced(db, "dograh")
    return {"ok": True}


@router.post("/api/billing/hooks/vobiz")
async def vobiz_hook(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    if not isinstance(body, dict):
        body = {}
    external_id = str(body.get("CallUUID") or body.get("call_uuid") or "")
    if not external_id:
        raise HTTPException(status_code=400, detail={"error": "CallUUID is required", "code": "bad_payload"})
    number = str(body.get("To") or body.get("to") or "")
    upsert_charge(
        db,
        "vobiz",
        external_id,
        tenant_for_phone(db, number),
        money_to_paise(0),
        {
            "CallUUID": external_id,
            "To": number,
            "Duration": body.get("Duration") or body.get("duration"),
        },
        False,
    )
    mark_synced(db, "vobiz")
    return {"ok": True}
