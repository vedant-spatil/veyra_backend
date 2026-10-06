import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_user
from app.dograh import EXTRACTED_FIELDS, TELEPHONY_CONFIGURATION_ID, WORKFLOW_ID, DograhError, dograh, empty_extracted
from app.ids import gen_id
from app.ledger import add_ledger
from app.models import Call, Wallet
from app.serialize import public_call
from app.usage import bump_usage

router = APIRouter()
PHONE_RE = re.compile(r"^\+[1-9]\d{7,14}$")
TEST_CALL_CREDITS = 5
OPEN_STATUSES = {"dialing", "ringing", "in_progress"}


def _merge_extracted(saved: dict | None, incoming: dict | None) -> dict:
    merged = empty_extracted()
    if isinstance(saved, dict):
        merged.update({key: saved.get(key) for key in EXTRACTED_FIELDS})
    for key in EXTRACTED_FIELDS:
        value = (incoming or {}).get(key)
        if key == "opted_out":
            if value:
                merged[key] = True
            continue
        if value:
            merged[key] = value
    return merged


def _sync_call(call: Call) -> None:
    if not call.dograh_run_id:
        return
    detail = dograh.fetch_run(call.dograh_run_id)
    if detail.get("status"):
        call.status = detail["status"]
    if detail.get("transcript"):
        call.transcript = detail["transcript"]
    call.extracted = _merge_extracted(call.extracted, detail.get("extracted"))
    url = detail.get("recording_url") or ""
    if url.startswith("http") and url != (call.recording_url or ""):
        try:
            dograh.fetch_recording(url)
        except DograhError:
            url = ""
        if url:
            call.recording_url = url


def _queue_retry(call_id: str) -> None:
    from worker.tasks import retry_dial

    retry_dial.delay(call_id)


@router.get("/api/calls")
def list_calls(principal=Depends(require_user), db: Session = Depends(get_db)):
    _user, tenant = principal
    rows = db.query(Call).filter(Call.tenant_id == tenant.id).order_by(Call.created_at.desc()).limit(100).all()
    for row in rows:
        if row.dograh_run_id and row.status in OPEN_STATUSES:
            try:
                with db.begin_nested():
                    _sync_call(row)
            except DograhError:
                continue
    db.commit()
    return {"calls": [public_call(row) for row in rows]}


@router.post("/api/calls", status_code=201)
def place_call(body: dict, principal=Depends(require_user), db: Session = Depends(get_db)):
    user, tenant = principal
    phone = str(body.get("to") or "").strip()
    if not PHONE_RE.match(phone):
        raise HTTPException(status_code=422, detail={"error": "a destination number in international format is required", "code": "bad_number"})
    variables = body.get("variables") if isinstance(body.get("variables"), dict) else {}
    clean = {str(key)[:40]: str(value)[:200] for key, value in variables.items()}
    customer = user.role == "customer"
    if customer:
        wallet = db.query(Wallet).filter_by(tenant_id=tenant.id).one_or_none()
        if wallet is None or wallet.balance_paise < TEST_CALL_CREDITS:
            raise HTTPException(status_code=402, detail={"error": "not enough credits", "code": "insufficient_credits"})
    call = Call(
        id=gen_id("call_"),
        tenant_id=tenant.id,
        user_id=user.id,
        to_number=phone,
        variables=clean,
        workflow_id=WORKFLOW_ID,
        telephony_configuration_id=TELEPHONY_CONFIGURATION_ID,
        from_phone_number_id=1,
        status="dialing",
        extracted=empty_extracted(),
    )
    db.add(call)
    db.flush()
    if customer:
        try:
            add_ledger(db, tenant.id, -TEST_CALL_CREDITS, "test_call", f"call:{call.id}", user.id, {})
        except ValueError as exc:
            db.rollback()
            raise HTTPException(status_code=402, detail={"error": "not enough credits", "code": "insufficient_credits"}) from exc
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
            _sync_call(call)
            db.commit()
            db.refresh(call)
        except DograhError:
            db.rollback()
            call = db.get(Call, call_id)
    return {"call": public_call(call)}


@router.get("/api/calls/{call_id}/recording")
def get_recording(call_id: str, principal=Depends(require_user), db: Session = Depends(get_db)):
    _user, tenant = principal
    call = db.get(Call, call_id)
    if not call or call.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail={"error": "call not found", "code": "not_found"})
    if not call.dograh_run_id:
        raise HTTPException(status_code=404, detail={"error": "the recording is not ready yet", "code": "recording_not_ready"})
    try:
        detail = dograh.fetch_run(call.dograh_run_id)
    except DograhError as exc:
        raise HTTPException(status_code=404, detail={"error": "the recording is not ready yet", "code": "recording_not_ready"}) from exc
    url = detail["recording_url"]
    if not url:
        raise HTTPException(status_code=404, detail={"error": "the recording is not ready yet", "code": "recording_not_ready"})
    try:
        body, media = dograh.fetch_recording(url)
    except DograhError as exc:
        raise HTTPException(status_code=502, detail={"error": str(exc), "code": "recording_failed"}) from exc
    call.recording_url = url
    db.commit()
    return Response(content=body, media_type=media)
