from app.auth import create_session, provision_user
from app.db import SessionLocal
from app.dograh import build_initiate_payload, dograh
from app.seed import seed


def _login(client, email: str, name: str = "Asha"):
    db = SessionLocal()
    try:
        _tenant, user = provision_user(db, email, name)
        token = create_session(db, user)
    finally:
        db.close()
    client.cookies.set("veyra_sess", token)


def test_login_rejects_unknown_password(client):
    response = client.post("/api/auth/login", json={"username": "admin@test.com", "password": "nope"})
    assert response.status_code == 401
    assert response.json()["code"] == "bad_login"


def test_seeded_admin_sees_billing_and_customer_does_not(client):
    db = SessionLocal()
    try:
        seed(db)
    finally:
        db.close()
    denied = client.post("/api/auth/login", json={"username": "customer@test.com", "password": "customer123"})
    assert denied.status_code == 200
    blocked = client.get("/api/admin/billing")
    assert blocked.status_code == 403
    client.cookies.clear()
    allowed = client.post("/api/auth/login", json={"username": "admin@test.com", "password": "admin123"})
    assert allowed.status_code == 200
    assert allowed.json()["user"]["role"] == "admin"
    report = client.get("/api/admin/billing")
    assert report.status_code == 200
    body = report.json()
    assert body["chargedInr"] == 0
    assert any(row["email"] == "customer@test.com" for row in body["customers"])
    assert any(row["provider"] == "rumik" and row["mode"] == "local" for row in body["providers"])


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
    assert seen["payload"]["telephony_configuration_id"] == 2
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
