import re

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_user
from app.dograh import DograhError, dograh, empty_extracted
from app.ids import gen_id
from app.models import Call
from app.serialize import public_call
from app.usage import bump_usage

router = APIRouter()
PHONE_RE = re.compile(r"^\+[1-9]\d{7,14}$")


def _queue_retry(call_id: str) -> None:
    from worker.tasks import retry_dial

    retry_dial.delay(call_id)


@router.get("/api/calls")
def list_calls(principal=Depends(require_user), db: Session = Depends(get_db)):
    _user, tenant = principal
    rows = db.query(Call).filter(Call.tenant_id == tenant.id).order_by(Call.created_at.desc()).limit(100).all()
    return {"calls": [public_call(row) for row in rows]}


@router.post("/api/calls", status_code=201)
def place_call(body: dict, principal=Depends(require_user), db: Session = Depends(get_db)):
    user, tenant = principal
    phone = str(body.get("to") or "").strip()
    if not PHONE_RE.match(phone):
        raise HTTPException(status_code=422, detail={"error": "a destination number in international format is required", "code": "bad_number"})
    variables = body.get("variables") if isinstance(body.get("variables"), dict) else {}
    clean = {str(key)[:40]: str(value)[:200] for key, value in variables.items()}
    call = Call(
        id=gen_id("call_"),
        tenant_id=tenant.id,
        user_id=user.id,
        to_number=phone,
        variables=clean,
        workflow_id=1,
        telephony_configuration_id=1,
        from_phone_number_id=1,
        status="dialing",
        extracted=empty_extracted(),
    )
    db.add(call)
    db.commit()
    try:
        result = dograh.initiate_call(phone, clean)
    except DograhError as exc:
        call.status = "failed"
        call.error = str(exc)[:500]
        db.commit()
        try:
            _queue_retry(call.id)
            call.error = (call.error + " Retry queued.").strip()
            db.commit()
        except Exception:
            db.rollback()
        raise HTTPException(status_code=502, detail={"error": str(exc), "code": "dial_failed", "call": public_call(call)}) from exc
    call.dograh_run_id = result["run_id"]
    call.status = "dialing"
    bump_usage(db, tenant.id, "calls", 1)
    db.commit()
    db.refresh(call)
    return {"call": public_call(call)}


@router.get("/api/calls/{call_id}")
def get_call(call_id: str, principal=Depends(require_user), db: Session = Depends(get_db)):
    _user, tenant = principal
    call = db.get(Call, call_id)
    if not call or call.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail={"error": "call not found", "code": "not_found"})
    if call.dograh_run_id:
        try:
            detail = dograh.fetch_run(call.dograh_run_id)
            if detail["recording_url"]:
                call.recording_url = detail["recording_url"]
            if detail["transcript"]:
                call.transcript = detail["transcript"]
            call.extracted = detail["extracted"]
            db.commit()
            db.refresh(call)
        except DograhError:
            db.rollback()
            call = db.get(Call, call_id)
    return {"call": public_call(call)}
