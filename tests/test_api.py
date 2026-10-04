from app.auth import create_session, provision_user, sign_state
from app.db import SessionLocal
from app.dograh import build_initiate_payload, dograh


def _login(client, email: str, name: str = "Asha"):
    db = SessionLocal()
    try:
        _tenant, user = provision_user(db, email, name)
        token = create_session(db, user)
    finally:
        db.close()
    client.cookies.set("veyra_sess", token)


def test_allowlist_rejects_unknown_email(client, monkeypatch):
    monkeypatch.setattr("app.routers.auth.exchange_code", lambda code: {
        "email": "nope@example.com",
        "email_verified": True,
        "name": "Nope",
        "sub": "google-nope",
    })
    response = client.get("/api/auth/google/callback", params={"code": "abc", "state": sign_state()})
    assert response.status_code == 403
    assert response.json()["code"] == "not_allowlisted"


def test_calls_reject_signed_out(client):
    response = client.post("/api/calls", json={"to": "+919876543210", "variables": {"contact_name": "Asha"}})
    assert response.status_code == 401
    assert response.json()["code"] == "no_session"


def test_signed_in_call_uses_workflow_1(client, monkeypatch):
    seen = {}

    def transport(method, path, payload):
        seen["method"] = method
        seen["path"] = path
        seen["payload"] = payload
        return {"workflow_run_id": "run-9"}

    monkeypatch.setattr(dograh, "transport", transport)
    _login(client, "allowed@example.com")
    response = client.post("/api/calls", json={"to": "+919876543210", "variables": {"contact_name": "Asha"}})
    assert response.status_code == 201
    body = response.json()["call"]
    assert body["workflowId"] == 1
    assert body["dograhRunId"] == "run-9"
    assert seen["path"] == "/api/v1/telephony/initiate-call"
    assert seen["payload"] == build_initiate_payload("+919876543210", {"contact_name": "Asha"})
    assert seen["payload"]["workflow_id"] == 1
    assert seen["payload"]["telephony_configuration_id"] == 1
    assert seen["payload"]["from_phone_number_id"] == 1


def test_tenant_cannot_read_other_agents(client):
    _login(client, "allowed@example.com", "Tenant A")
    created = client.post("/api/agents", json={"name": "Desk"}).json()["agent"]
    _login(client, "other-allowed@example.com", "Tenant B")
    listing = client.get("/api/agents")
    assert listing.status_code == 200
    assert all(item["id"] != created["id"] for item in listing.json()["agents"])
    denied = client.post("/api/agents/update", json={"id": created["id"], "name": "Stolen"})
    assert denied.status_code == 403
    assert denied.json()["code"] == "forbidden"
