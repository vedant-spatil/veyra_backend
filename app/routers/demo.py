import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.demo_links import create_demo_token, demo_link_status, normalize_demo_limits, parse_demo_token, public_demo_link, safe_hash_equal
from app.deps import require_owner
from app.ledger import add_audit
from app.models import Agent, DemoLink, Tenant

router = APIRouter()
COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


@router.get("/api/demo-links")
def list_links(principal=Depends(require_owner), db: Session = Depends(get_db)):
    _user, tenant = principal
    rows = db.query(DemoLink).filter_by(tenant_id=tenant.id).order_by(DemoLink.created_at.desc()).all()
    return {"demoLinks": [public_demo_link(row) for row in rows]}


@router.post("/api/demo-links", status_code=201)
def create_link(body: dict, principal=Depends(require_owner), db: Session = Depends(get_db)):
    user, tenant = principal
    agent = db.get(Agent, str(body.get("agentId") or ""))
    if not agent or agent.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail={"error": "agent not found", "code": "not_found"})
    generated = create_demo_token()
    limits = normalize_demo_limits(body)
    link = DemoLink(
        id=generated["id"],
        token_hash=generated["tokenHash"],
        tenant_id=tenant.id,
        agent_id=agent.id,
        label=(str(body.get("label") or f"{agent.name} demo").strip()[:80] or f"{agent.name} demo"),
        status="active",
        starts=0,
        created_by=user.id,
        expires_at=limits["expires_at"],
        max_session_seconds=limits["max_session_seconds"],
        max_starts=limits["max_starts"],
    )
    db.add(link)
    add_audit(db, tenant.id, user.id, "demo_link.created", "demo_link", link.id, {"agentId": agent.id})
    db.commit()
    db.refresh(link)
    return {"demoLink": public_demo_link(link), "sharePath": f"/demo/{generated['token']}"}


@router.post("/api/demo-links/revoke")
def revoke_link(body: dict, principal=Depends(require_owner), db: Session = Depends(get_db)):
    user, tenant = principal
    link = db.get(DemoLink, str(body.get("id") or ""))
    if not link or link.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail={"error": "demo link not found", "code": "not_found"})
    link.status = "revoked"
    link.revoked_at = datetime.now(timezone.utc)
    link.revoked_by = user.id
    add_audit(db, tenant.id, user.id, "demo_link.revoked", "demo_link", link.id, {"agentId": link.agent_id})
    db.commit()
    return {"ok": True}


def _public_context(db: Session, token: str):
    parsed = parse_demo_token(token)
    if not parsed:
        return None
    link = db.get(DemoLink, parsed["id"])
    if not link or not safe_hash_equal(parsed["tokenHash"], link.token_hash):
        return None
    tenant = db.get(Tenant, link.tenant_id)
    agent = db.get(Agent, link.agent_id)
    if not tenant or tenant.status != "active" or not agent or agent.tenant_id != tenant.id:
        return None
    color = str((tenant.branding or {}).get("color") or "#B88A2D")
    return link, tenant, agent, color if COLOR_RE.match(color) else "#B88A2D"


@router.get("/api/public/demo/{token}")
def public_meta(token: str, db: Session = Depends(get_db)):
    found = _public_context(db, token)
    if not found:
        raise HTTPException(status_code=404, detail={"error": "demo link not found", "code": "not_found"})
    link, tenant, agent, color = found
    return {
        "demo": {
            "id": link.id,
            "label": link.label,
            "status": demo_link_status(link),
            "expiresAt": link.expires_at.isoformat(),
            "maxSessionSeconds": link.max_session_seconds,
        },
        "brand": {"name": tenant.name, "color": color},
        "agent": {"name": agent.name, "greeting": (agent.greeting or "")[:300]},
    }
