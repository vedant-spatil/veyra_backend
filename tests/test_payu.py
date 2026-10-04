import hashlib

import pytest

from app import payu

config = {"env": "test", "key": "merchant-key", "salt": "merchant-salt"}
packs = {"starter": {"amount": "200.00", "credits": 200, "currency": "INR", "productinfo": "Starter voice credits"}}
customer = {"firstname": "Shreyas", "email": "billing@example.com", "phone": "9999999999"}


def fixture():
    intent = payu.create_payment_intent(pack_id="starter", packs=packs, tenant_id="tenant-1", user_id="user-1")
    checkout = payu.build_checkout(
        intent=intent,
        customer=customer,
        config=config,
        success_url="https://voice.example.com/api/billing/payu/return",
        failure_url="https://voice.example.com/api/billing/payu/return",
    )
    return intent, checkout


def test_pack_catalog_and_opaque_ids():
    first = payu.create_payment_intent(pack_id="starter", packs=packs, tenant_id="tenant-1", user_id="user-1")
    second = payu.create_payment_intent(pack_id="starter", packs=packs, tenant_id="tenant-1", user_id="user-1")
    assert first["amount"] == "200.00"
    assert first["credits"] == 200
    assert first["txnid"] != second["txnid"]
    assert first["intentToken"] != second["intentToken"]
    with pytest.raises(ValueError, match="unknown pack"):
        payu.create_payment_intent(pack_id="missing", packs=packs, tenant_id="t", user_id="u")


def test_signed_checkout_hides_salt():
    intent, checkout = fixture()
    assert checkout["url"] == payu.ENDPOINTS["test"]["payment"]
    assert checkout["fields"]["udf1"] == intent["intentToken"]
    assert checkout["fields"]["udf2"] == ""
    assert len(checkout["fields"]["hash"]) == 128
    assert config["salt"] not in str(checkout)


def test_captured_callback_requires_verification():
    intent, checkout = fixture()
    payload = {**checkout["fields"], "status": "success", "unmappedstatus": "captured"}
    payload["hash"] = payu.callback_hash(payload, config["salt"])
    result = payu.verify_callback(payload=payload, intent=intent, customer=customer, config=config)
    assert result == {"valid": True, "creditable": True, "reason": "requires verify_payment"}


def test_exact_hash_pipe_sequences():
    _intent, checkout = fixture()
    fields = checkout["fields"]
    request_preimage = "|".join([
        fields["key"], fields["txnid"], fields["amount"], fields["productinfo"], fields["firstname"], fields["email"],
        fields["udf1"], fields["udf2"], fields["udf3"], fields["udf4"], fields["udf5"],
        "", "", "", "", "", config["salt"],
    ])
    assert fields["hash"] == hashlib.sha512(request_preimage.encode()).hexdigest()
    payload = {**fields, "status": "success", "unmappedstatus": "captured"}
    reverse = "|".join([
        config["salt"], "success", "", "", "", "", "",
        payload["udf5"], payload["udf4"], payload["udf3"], payload["udf2"], payload["udf1"],
        payload["email"], payload["firstname"], payload["productinfo"], payload["amount"], payload["txnid"], payload["key"],
    ])
    assert payu.callback_hash(payload, config["salt"]) == hashlib.sha512(reverse.encode()).hexdigest()


def test_rejects_tampering_and_additional_charges():
    intent, checkout = fixture()
    payload = {**checkout["fields"], "status": "success", "unmappedstatus": "captured", "additionalCharges": "5.00"}
    payload["hash"] = payu.callback_hash(payload, config["salt"])
    assert payu.verify_callback(payload=payload, intent=intent, customer=customer, config=config)["valid"] is True
    payload["amount"] = "2000.00"
    assert payu.verify_callback(payload=payload, intent=intent, customer=customer, config=config)["valid"] is False


def test_verify_payment_accepts_only_matching_capture():
    intent, _checkout = fixture()
    request = payu.build_verify_payment_form(intent["txnid"], config)
    assert request["url"] == payu.ENDPOINTS["test"]["postservice"]
    assert len(request["fields"]["hash"]) == 128

    def transport(_url, _fields):
        return {"status": 200, "body": '{"transaction_details": {"%s": {"status": "captured", "amt": "200.00", "mihpayid": "payu-123"}}}' % intent["txnid"]}

    result = payu.verify_payment(intent=intent, config=config, transport=transport)
    assert result["verified"] is True
    assert result["payuId"] == "payu-123"

    def mismatch(_url, _fields):
        return {"status": 200, "body": '{"transaction_details": {"%s": {"status": "captured", "amt": "201.00", "mihpayid": "payu-123"}}}' % intent["txnid"]}

    assert payu.verify_payment(intent=intent, config=config, transport=mismatch)["verified"] is False


def test_browser_return_never_credits():
    assert payu.classify_browser_return() == {"credit": False, "status": "pending_verification"}
