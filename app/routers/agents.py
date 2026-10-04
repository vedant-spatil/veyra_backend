from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_user
from app.ids import gen_id
from app.models import Agent
from app.presets import PRESETS, find_preset
from app.serialize import public_agent

router = APIRouter()

TTS_SPEAKERS = {"speaker_1", "speaker_2", "speaker_3", "speaker_4"}


def _tts(body: dict) -> dict:
    incoming = body.get("tts") if isinstance(body.get("tts"), dict) else {}
    model = "muga" if incoming.get("model") == "muga" else "mulberry"
    speaker = incoming.get("speaker") if incoming.get("speaker") in TTS_SPEAKERS else "speaker_1"
    try:
        pitch = int(incoming.get("f0_up_key") or 0)
    except (TypeError, ValueError):
        pitch = 0
    return {"provider": "rumik", "model": model, "speaker": speaker, "f0_up_key": max(-12, min(12, pitch))}


@router.get("/api/presets")
def presets(_principal=Depends(require_user)):
    return {"presets": PRESETS}


@router.get("/api/agents")
def list_agents(principal=Depends(require_user), db: Session = Depends(get_db)):
    _user, tenant = principal
    rows = db.query(Agent).filter(Agent.tenant_id == tenant.id).order_by(Agent.created_at.desc()).all()
    return {"agents": [public_agent(row) for row in rows]}


@router.post("/api/agents")
def create_agent(body: dict, principal=Depends(require_user), db: Session = Depends(get_db)):
    _user, tenant = principal
    preset = find_preset(body.get("presetId"))
    if body.get("presetId") and not preset:
        raise HTTPException(status_code=404, detail={"error": "preset not found", "code": "not_found"})
    name = str(body.get("name") or (preset or {}).get("name") or "Untitled Agent")[:60]
    persona_default = ""
    if preset:
        persona_default = f"{preset['name']}. Collect: {', '.join(preset['fields'])}. Guardrails: {'; '.join(preset['guardrails'])}."
    agent = Agent(
        id=gen_id("ag_"),
        tenant_id=tenant.id,
        name=name,
        persona=str(body.get("persona") or persona_default)[:1500],
        greeting=str(body.get("greeting") or (preset or {}).get("greeting") or "")[:300],
        tts=_tts(body),
        telephony={"did": "".join(ch for ch in str(body.get("did") or "") if ch.isdigit())},
        preset_id=preset["id"] if preset else "",
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)
    return {"agent": public_agent(agent)}


@router.post("/api/agents/update")
def update_agent(body: dict, principal=Depends(require_user), db: Session = Depends(get_db)):
    _user, tenant = principal
    agent = db.get(Agent, str(body.get("id") or ""))
    if not agent:
        raise HTTPException(status_code=404, detail={"error": "agent not found", "code": "not_found"})
    if agent.tenant_id != tenant.id:
        raise HTTPException(status_code=403, detail={"error": "not your agent", "code": "forbidden"})
    if body.get("name") is not None:
        agent.name = str(body["name"])[:60]
    if body.get("persona") is not None:
        agent.persona = str(body["persona"])[:1500]
    if body.get("greeting") is not None:
        agent.greeting = str(body["greeting"])[:300]
    if body.get("did") is not None:
        agent.telephony = {**(agent.telephony or {}), "did": "".join(ch for ch in str(body["did"]) if ch.isdigit())}
    if isinstance(body.get("tts"), dict):
        merged = {**(agent.tts or {}), **body["tts"]}
        agent.tts = _tts({"tts": merged})
    db.commit()
    db.refresh(agent)
    return {"agent": public_agent(agent)}


@router.post("/api/agents/delete")
def delete_agent(body: dict, principal=Depends(require_user), db: Session = Depends(get_db)):
    _user, tenant = principal
    agent = db.get(Agent, str(body.get("id") or ""))
    if not agent:
        raise HTTPException(status_code=404, detail={"error": "agent not found", "code": "not_found"})
    if agent.tenant_id != tenant.id:
        raise HTTPException(status_code=403, detail={"error": "not your agent", "code": "forbidden"})
    db.delete(agent)
    db.commit()
    return {"ok": True}
