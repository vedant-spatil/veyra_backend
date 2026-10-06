from sqlalchemy import select

from app.auth import create_session, provision_user
from app.db import SessionLocal
from app.dograh import DograhError, build_initiate_payload, dograh
from app.ledger import add_ledger
from app.models import Ledger, User
from app.seed import seed


def _login(client, email: str, name: str = "Asha"):
    db = SessionLocal()
    try:
        _tenant, user = provision_user(db, email, name)
        token = create_session(db, user)
    finally:
        db.close()
    client.cookies.set("veyra_sess", token)


def _fund(email: str, amount: int = 10):
    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.email == email))
        add_ledger(db, user.tenant_id, amount, "test_grant", f"test-grant:{user.id}", user.id, {})
        db.commit()
    finally:
        db.close()


def _balance(client) -> int:
    return client.get("/api/wallet").json()["wallet"]["balancePaise"]


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
    _fund("allowed@example.com")
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


def test_signup_grants_ten_credits_and_calls_charge_five(client, monkeypatch):
    sent = {}

    def fake_send(email, code):
        sent["email"] = email
        sent["code"] = code

    monkeypatch.setattr("app.routers.auth.send_signup_code", fake_send)
    created = client.post("/api/auth/signup", json={"name": "Asha", "email": "asha@gmail.com", "password": "secret"})
    assert created.status_code == 202
    assert created.json() == {"sent": True, "email": "asha@gmail.com"}
    wrong_code = "111111" if sent["code"] == "000000" else "000000"
    rejected = client.post("/api/auth/signup/verify", json={"email": "asha@gmail.com", "code": wrong_code})
    assert rejected.status_code == 401
    assert rejected.json()["code"] == "bad_code"
    db = SessionLocal()
    try:
        assert db.scalar(select(User).where(User.email == "asha@gmail.com")) is None
    finally:
        db.close()
    verified = client.post("/api/auth/signup/verify", json={"email": "asha@gmail.com", "code": sent["code"]})
    assert verified.status_code == 201
    assert verified.json()["user"]["role"] == "customer"
    assert _balance(client) == 10
    yahoo = client.post("/api/auth/signup", json={"name": "Asha", "email": "asha@yahoo.com", "password": "secret"})
    assert yahoo.status_code == 422
    assert yahoo.json()["code"] == "bad_email"
    org = client.post("/api/auth/signup", json={"name": "Asha", "email": "asha@gmail.org", "password": "secret"})
    assert org.status_code == 422
    plain = client.post("/api/auth/signup", json={"name": "Asha", "email": "ashagmail.com", "password": "secret"})
    assert plain.status_code == 422

    def succeed(_method, _path, _payload):
        return {"workflow_run_id": "run-1"}

    monkeypatch.setattr(dograh, "transport", succeed)
    monkeypatch.setattr("app.routers.calls._queue_retry", lambda _call_id: None)
    paid = client.post("/api/calls", json={"to": "+919876543210", "credits": 999, "balance": 999})
    assert paid.status_code == 201
    assert _balance(client) == 5

    def fail(_method, _path, _payload):
        raise DograhError("line busy")

    monkeypatch.setattr(dograh, "transport", fail)
    missed = client.post("/api/calls", json={"to": "+919876543210", "credits": 999})
    assert missed.status_code == 502
    assert _balance(client) == 0

    blocked = client.post("/api/calls", json={"to": "+919876543210", "credits": 999})
    assert blocked.status_code == 402
    assert blocked.json()["code"] == "insufficient_credits"
    assert _balance(client) == 0

    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.email == "asha@gmail.com"))
        kinds = [row.entry_type for row in db.query(Ledger).filter_by(tenant_id=user.tenant_id).all()]
    finally:
        db.close()
    assert kinds.count("signup_credit") == 1
    assert kinds.count("test_call") == 2


def test_success_message_stores_numeric_run_id(client, monkeypatch):
    def transport(method, path, payload):
        if method == "POST":
            return {"message": "Call initiated successfully with run name WR-TEL-OUT-00001234"}
        return {"runs": [{"id": 77, "name": "WR-TEL-OUT-00001234"}, {"id": 3, "name": "WR-TEL-OUT-00000001"}]}

    monkeypatch.setattr(dograh, "transport", transport)
    _login(client, "allowed@example.com")
    response = client.post("/api/calls", json={"to": "+919876543210"})
    assert response.status_code == 201
    assert response.json()["call"]["dograhRunId"] == "77"


def test_signup_rejects_existing_email_ignoring_case(client, monkeypatch):
    sent = {"count": 0}

    def fake_send(email, code):
        sent["count"] += 1
        sent["code"] = code

    monkeypatch.setattr("app.routers.auth.send_signup_code", fake_send)
    created = client.post("/api/auth/signup", json={"name": "Asha", "email": "asha@gmail.com", "password": "secret"})
    assert created.status_code == 202
    verified = client.post("/api/auth/signup/verify", json={"email": "asha@gmail.com", "code": sent["code"]})
    assert verified.status_code == 201
    again = client.post("/api/auth/signup", json={"name": "Asha", "email": "Asha@gmail.com", "password": "secret"})
    assert again.status_code == 409
    assert again.json()["code"] == "email_taken"
    assert sent["count"] == 1


def test_admin_dials_without_credits(client, monkeypatch):
    db = SessionLocal()
    try:
        seed(db)
    finally:
        db.close()

    def transport(method, path, payload):
        return {"workflow_run_id": "run-admin"}

    monkeypatch.setattr(dograh, "transport", transport)
    signed_in = client.post("/api/auth/login", json={"username": "admin@test.com", "password": "admin123"})
    assert signed_in.status_code == 200
    assert _balance(client) == 0
    response = client.post("/api/calls", json={"to": "+919876543210"})
    assert response.status_code == 201
    assert _balance(client) == 0
    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.email == "admin@test.com"))
        charges = db.query(Ledger).filter_by(tenant_id=user.tenant_id, entry_type="test_call").count()
    finally:
        db.close()
    assert charges == 0


def test_call_recording_is_streamed(client, monkeypatch):
    db = SessionLocal()
    try:
        seed(db)
    finally:
        db.close()
    _fund("customer@test.com")
    signed_in = client.post("/api/auth/login", json={"username": "customer@test.com", "password": "customer123"})
    assert signed_in.status_code == 200

    def transport(method, path, payload):
        return {"workflow_run_id": "run-rec"}

    monkeypatch.setattr(dograh, "transport", transport)
    placed = client.post("/api/calls", json={"to": "+919876543210"})
    assert placed.status_code == 201
    call_id = placed.json()["call"]["id"]

    def fetch_run(run_id):
        assert run_id == "run-rec"
        return {"recording_url": "http://minio.local/rec.mp3", "transcript": "hello", "extracted": {}}

    monkeypatch.setattr(dograh, "fetch_run", fetch_run)
    monkeypatch.setattr(dograh, "fetch_recording", lambda url: (b"audio-bytes", "audio/mpeg"))
    audio = client.get(f"/api/calls/{call_id}/recording")
    assert audio.status_code == 200
    assert audio.content == b"audio-bytes"
    assert audio.headers["content-type"].startswith("audio/mpeg")


def test_finished_run_fills_status_transcript_and_fields(client, monkeypatch):
    run = {
        "is_completed": True,
        "logs": {
            "telephony_status_callbacks": [{"status": "ringing"}, {"status": "completed"}],
            "realtime_feedback_events": [
                {"type": "rtf-bot-text", "payload": {"text": "Who am I speaking with?"}},
                {"type": "rtf-user-transcription", "payload": {"text": "Vedant", "final": True}},
                {"type": "rtf-user-transcription", "payload": {"text": "ignore partial", "final": False}},
            ],
        },
        "gathered_context": {
            "call_status": "end_call",
            "extracted_variables": {
                "caller_name": "Vedant",
                "requirement": "feedback",
                "language": "Hindi",
                "next_step": "call back",
                "opted_out": False,
            },
        },
    }

    def transport(method, path, payload):
        if method == "POST":
            return {"workflow_run_id": "run-done"}
        return run

    monkeypatch.setattr(dograh, "transport", transport)
    _login(client, "owner-sync@example.com")
    placed = client.post("/api/calls", json={"to": "+919876543210"})
    assert placed.status_code == 201
    call_id = placed.json()["call"]["id"]
    listed = client.get("/api/calls")
    assert listed.status_code == 200
    body = listed.json()["calls"][0]
    assert body["status"] == "completed"
    assert body["transcript"] == "Agent: Who am I speaking with?\nCaller: Vedant"
    assert body["extracted"]["caller_name"] == "Vedant"
    assert body["extracted"]["requirement"] == "feedback"
    assert body["extracted"]["language"] == "Hindi"
    assert body["extracted"]["next_step"] == "call back"
    assert body["extracted"]["opted_out"] is False

    run["gathered_context"] = {"extracted_variables": {"caller_name": ""}}
    again = client.get(f"/api/calls/{call_id}").json()["call"]
    assert again["status"] == "completed"
    assert again["extracted"]["caller_name"] == "Vedant"
    assert again["extracted"]["requirement"] == "feedback"


def test_json_recording_is_not_audio(client, monkeypatch):
    def transport(method, path, payload):
        return {"workflow_run_id": "run-bad-audio"}

    monkeypatch.setattr(dograh, "transport", transport)
    _login(client, "owner-audio@example.com")
    placed = client.post("/api/calls", json={"to": "+919876543210"})
    call_id = placed.json()["call"]["id"]

    class _Response:
        status_code = 200
        headers = {"content-type": "application/json"}
        content = b'{"detail":"not found"}'
        text = '{"detail":"not found"}'

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None):
            return _Response()

    monkeypatch.setattr("app.dograh.httpx.Client", _Client)
    monkeypatch.setattr(
        dograh,
        "fetch_run",
        lambda run_id: {
            "status": "completed",
            "recording_url": "http://minio.local/rec.wav",
            "transcript": "",
            "extracted": {},
        },
    )
    audio = client.get(f"/api/calls/{call_id}/recording")
    assert audio.status_code == 502
    assert audio.json()["code"] == "recording_failed"
    stored = client.get(f"/api/calls/{call_id}").json()["call"]
    assert stored["recordingUrl"] is None


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
