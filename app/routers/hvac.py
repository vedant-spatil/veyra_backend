import re
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_user
from app.ids import gen_id
from app.ledger import add_audit
from app.models import HvacJob
from app.serialize import public_hvac
from app.settings import get_settings

router = APIRouter()
HVAC_TIMEZONE = "Asia/Kolkata"
HVAC_OUTCOMES = {"new", "booked", "routed", "follow_up", "closed", "abandoned"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
START_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T")


def cal_request(method: str, path: str, version: str, payload: dict | None = None):
    key = get_settings().calcom_api_key
    if not key:
        raise HTTPException(status_code=503, detail={"error": "Cal.com is not configured", "code": "calendar_not_configured"})
    headers = {"Authorization": f"Bearer {key}", "cal-api-version": version, "Accept": "application/json"}
    response = httpx.request(method, "https://api.cal.com" + path, headers=headers, json=payload, timeout=20)
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code >= 300:
        message = body.get("message") or body.get("error") or "Cal.com request failed"
        raise HTTPException(status_code=response.status_code or 502, detail={"error": message, "code": "calendar_upstream"})
    return body


@router.get("/api/hvac/desk")
def desk(principal=Depends(require_user), db: Session = Depends(get_db)):
    _user, tenant = principal
    jobs = db.query(HvacJob).filter_by(tenant_id=tenant.id).order_by(HvacJob.updated_at.desc()).all()
    def count(outcome: str) -> int:
        return sum(1 for job in jobs if job.outcome == outcome)
    return {
        "timezone": HVAC_TIMEZONE,
        "calendarConfigured": bool(get_settings().calcom_api_key),
        "jobs": [public_hvac(job) for job in jobs],
        "stats": {"calls": len(jobs), "booked": count("booked"), "routed": count("routed"), "followUp": count("follow_up")},
    }


@router.get("/api/hvac/event-types")
def event_types(_principal=Depends(require_user)):
    result = cal_request("GET", "/v2/event-types", "2024-06-14")
    events = [
        {
            "id": event.get("id"),
            "title": event.get("title"),
            "slug": event.get("slug"),
            "lengthInMinutes": event.get("lengthInMinutes"),
            "locations": event.get("locations") or [],
        }
        for event in (result.get("data") or [])
    ]
    return {"eventTypes": events}


@router.get("/api/hvac/slots")
def slots(request: Request, _principal=Depends(require_user)):
    event_type_id = request.query_params.get("eventTypeId") or ""
    start = request.query_params.get("start") or ""
    end = request.query_params.get("end") or ""
    if not event_type_id.isdigit() or not DATE_RE.match(start) or not DATE_RE.match(end):
        raise HTTPException(status_code=422, detail={"error": "event type and date range required", "code": "bad_calendar_query"})
    result = cal_request("GET", f"/v2/slots?eventTypeId={event_type_id}&start={start}&end={end}&timeZone={HVAC_TIMEZONE}&format=range", "2024-09-04")
    return {"timezone": HVAC_TIMEZONE, "slots": result.get("data") or {}}


@router.post("/api/hvac/jobs")
def save_job(body: dict, principal=Depends(require_user), db: Session = Depends(get_db)):
    user, tenant = principal
    caller = str(body.get("callerName") or "").strip()[:100]
    phone = str(body.get("phone") or "").strip()[:32]
    if not caller or not phone:
        raise HTTPException(status_code=422, detail={"error": "caller name and phone are required", "code": "missing_contact"})
    outcome = body.get("outcome") if body.get("outcome") in HVAC_OUTCOMES else "new"
    now = datetime.now(timezone.utc)
    job = db.get(HvacJob, str(body.get("id") or "")) if body.get("id") else None
    if job and job.tenant_id != tenant.id:
        job = None
    if job is None:
        job = HvacJob(id=gen_id("hvac_"), tenant_id=tenant.id, caller_name=caller, phone=phone, created_at=now, appointment=None)
        db.add(job)
    job.caller_name = caller
    job.phone = phone
    job.email = str(body.get("email") or "").strip()[:180]
    job.service = str(body.get("service") or "General HVAC").strip()[:80]
    job.urgency = str(body.get("urgency") or "normal").strip()[:30]
    job.outcome = outcome
    job.assigned_to = str(body.get("assignedTo") or "").strip()[:80]
    job.notes = str(body.get("notes") or "").strip()[:2000]
    job.updated_at = now
    add_audit(db, tenant.id, user.id, "hvac.job.saved", "hvac_job", job.id, {"outcome": job.outcome})
    db.commit()
    db.refresh(job)
    return {"job": public_hvac(job)}


@router.post("/api/hvac/book", status_code=201)
def book(body: dict, principal=Depends(require_user), db: Session = Depends(get_db)):
    user, tenant = principal
    try:
        event_type_id = int(body.get("eventTypeId"))
    except (TypeError, ValueError):
        event_type_id = 0
    start = str(body.get("start") or "")
    attendee = body.get("attendee") if isinstance(body.get("attendee"), dict) else {}
    if event_type_id <= 0 or not START_RE.match(start):
        raise HTTPException(status_code=422, detail={"error": "event type and appointment time are required", "code": "bad_booking"})
    name = str(attendee.get("name") or "").strip()
    email = str(attendee.get("email") or "").strip().lower()
    phone = str(attendee.get("phone") or "").strip()
    if not name or "@" not in email or not phone:
        raise HTTPException(status_code=422, detail={"error": "attendee name, email and phone are required for Cal.com booking", "code": "missing_booking_contact"})
    booking = cal_request("POST", "/v2/bookings", "2026-02-25", {
        "eventTypeId": event_type_id,
        "start": start,
        "attendee": {"name": name, "email": email, "phoneNumber": phone, "timeZone": HVAC_TIMEZONE, "language": "en"},
        "metadata": {"source": "veyra_hvac_desk", "jobId": str(body.get("jobId") or "")},
    })
    data = booking.get("data") or {}
    now = datetime.now(timezone.utc)
    job = db.get(HvacJob, str(body.get("jobId") or "")) if body.get("jobId") else None
    if job and job.tenant_id != tenant.id:
        job = None
    if job is None:
        job = HvacJob(id=gen_id("hvac_"), tenant_id=tenant.id, caller_name=name, phone=phone, email=email, created_at=now)
        db.add(job)
    job.outcome = "booked"
    job.updated_at = now
    job.appointment = {
        "calBookingUid": data.get("uid"),
        "eventTypeId": event_type_id,
        "start": data.get("start"),
        "end": data.get("end"),
        "status": data.get("status"),
        "timezone": HVAC_TIMEZONE,
    }
    add_audit(db, tenant.id, user.id, "hvac.booking.created", "hvac_job", job.id, {"eventTypeId": event_type_id})
    db.commit()
    db.refresh(job)
    return {"booking": data, "job": public_hvac(job)}
