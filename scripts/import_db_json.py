"""One-time import of a copied db.json into Veyra Postgres. Does not write the JSON file."""

import json
import sys
from pathlib import Path

from app.db import SessionLocal
from app.ids import gen_id
from app.models import Agent, Tenant, User, Wallet


def main(path: str) -> None:
    payload = json.loads(Path(path).read_text())
    db = SessionLocal()
    try:
        for raw in payload.get("tenants") or []:
            if db.get(Tenant, raw["id"]):
                continue
            db.add(Tenant(
                id=raw["id"],
                name=raw.get("name") or "Workspace",
                slug=raw.get("slug") or raw["id"],
                status=raw.get("status") or "active",
                privacy_mode=raw.get("privacyMode") or "standard",
                branding=raw.get("branding") or {},
                plan=raw.get("plan") or "studio",
            ))
        for raw in payload.get("users") or []:
            if db.get(User, raw["id"]):
                continue
            db.add(User(
                id=raw["id"],
                tenant_id=raw["tenantId"],
                email=str(raw.get("email") or "").lower(),
                name=raw.get("name") or "Owner",
                pass_hash=raw.get("passHash") or "",
                role=raw.get("role") or "member",
                status=raw.get("status") or "active",
            ))
        for raw in payload.get("wallets") or []:
            if db.get(Wallet, raw["id"]):
                continue
            db.add(Wallet(id=raw["id"], tenant_id=raw["tenantId"], currency=raw.get("currency") or "INR", balance_paise=int(raw.get("balancePaise") or 0)))
        for raw in payload.get("agents") or []:
            if db.get(Agent, raw["id"]):
                continue
            db.add(Agent(
                id=raw.get("id") or gen_id("ag_"),
                tenant_id=raw["tenantId"],
                name=str(raw.get("name") or "Agent")[:60],
                persona=str(raw.get("persona") or "")[:1500],
                greeting=str(raw.get("greeting") or "")[:300],
                tts=raw.get("tts") or {},
                telephony=raw.get("telephony") or {},
                preset_id=raw.get("presetId") or "",
            ))
        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    main(sys.argv[1])
