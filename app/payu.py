"""PayU hosted checkout. Matches dashboard/lib/payu.js hash rules."""

import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timezone
from urllib.parse import urlencode, urlparse

import httpx

ENDPOINTS = {
    "test": {
        "payment": "https://test.payu.in/_payment",
        "postservice": "https://test.payu.in/merchant/postservice?form=2",
    },
    "production": {
        "payment": "https://secure.payu.in/_payment",
        "postservice": "https://info.payu.in/merchant/postservice.php?form=2",
    },
}

_AMOUNT = re.compile(r"^(?:0|[1-9]\d{0,8})\.\d{2}$")


def sha512(value: str) -> str:
    return hashlib.sha512(str(value).encode("utf-8")).hexdigest()


def required_string(value, name: str, max_len: int = 255) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_len:
        raise ValueError(f"invalid {name}")
    if re.search(r"[|\r\n]", value):
        raise ValueError(f"invalid {name}")
    return value


def canonical_amount(value) -> str:
    raw = f"{value:.2f}" if isinstance(value, (int, float)) and not isinstance(value, bool) else str(value)
    if not _AMOUNT.match(raw) or raw == "0.00":
        raise ValueError("invalid amount")
    return raw


def https_url(value: str, name: str) -> str:
    parsed = urlparse(str(value))
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"{name} must use HTTPS")
    return str(value)


def validate_config(config: dict) -> dict:
    env = "production" if config and config.get("env") == "production" else "test"
    return {
        "env": env,
        "key": required_string(config.get("key") if config else None, "PayU key"),
        "salt": required_string(config.get("salt") if config else None, "PayU salt"),
        "paymentUrl": https_url((config or {}).get("paymentUrl") or ENDPOINTS[env]["payment"], "PayU payment URL"),
        "postserviceUrl": https_url((config or {}).get("postserviceUrl") or ENDPOINTS[env]["postservice"], "PayU postservice URL"),
    }


def resolve_pack(pack_id, packs: dict) -> dict:
    required_string(pack_id, "pack id", 64)
    if not isinstance(packs, dict):
        raise ValueError("server pack catalog required")
    pack = packs.get(pack_id)
    if not isinstance(pack, dict):
        raise ValueError("unknown pack")
    return {
        "id": pack_id,
        "amount": canonical_amount(pack["amount"]),
        "currency": pack.get("currency") or "INR",
        "credits": int(pack["credits"]),
        "productinfo": required_string(pack.get("productinfo") or f"Veyra credits, {pack_id}", "productinfo", 100),
    }


def create_payment_intent(*, pack_id, packs, tenant_id, user_id, now=None) -> dict:
    pack = resolve_pack(pack_id, packs)
    if pack["currency"] != "INR":
        raise ValueError("PayU pack currency must be INR")
    if pack["credits"] <= 0:
        raise ValueError("invalid credits")
    created = (now or datetime.now(timezone.utc)).isoformat()
    return {
        "txnid": "rxv_" + secrets.token_hex(16),
        "intentToken": secrets.token_urlsafe(32),
        "tenantId": required_string(tenant_id, "tenant id", 128),
        "userId": required_string(user_id, "user id", 128),
        "packId": pack["id"],
        "amount": pack["amount"],
        "currency": pack["currency"],
        "credits": pack["credits"],
        "productinfo": pack["productinfo"],
        "status": "pending",
        "createdAt": created,
    }


def build_request_hash(fields: dict, salt: str) -> str:
    preimage = "|".join([
        fields["key"], fields["txnid"], fields["amount"], fields["productinfo"],
        fields["firstname"], fields["email"], fields.get("udf1") or "", fields.get("udf2") or "",
        fields.get("udf3") or "", fields.get("udf4") or "", fields.get("udf5") or "",
        "", "", "", "", "", salt,
    ])
    return sha512(preimage)


def build_checkout(*, intent, customer, success_url, failure_url, config) -> dict:
    cfg = validate_config(config)
    if not intent or intent.get("status") != "pending":
        raise ValueError("invalid payment intent")
    fields = {
        "key": cfg["key"],
        "txnid": required_string(intent["txnid"], "txnid", 50),
        "amount": canonical_amount(intent["amount"]),
        "productinfo": required_string(intent["productinfo"], "productinfo", 100),
        "firstname": required_string((customer or {}).get("firstname"), "firstname", 60),
        "email": required_string((customer or {}).get("email"), "email", 120),
        "phone": required_string(str(customer["phone"]), "phone", 20) if customer and customer.get("phone") else "",
        "surl": https_url(success_url, "success URL"),
        "furl": https_url(failure_url, "failure URL"),
        "udf1": required_string(intent["intentToken"], "intent token", 128),
        "udf2": "",
        "udf3": "",
        "udf4": "",
        "udf5": "",
    }
    fields["hash"] = build_request_hash(fields, cfg["salt"])
    return {"url": cfg["paymentUrl"], "fields": fields}


def callback_hash(payload: dict, salt: str) -> str:
    parts = []
    extra = payload.get("additionalCharges", payload.get("additional_charges"))
    if extra is not None and str(extra) != "":
        parts.append(str(extra))
    parts.extend([
        salt, payload.get("status") or "", "", "", "", "", "",
        payload.get("udf5") or "", payload.get("udf4") or "", payload.get("udf3") or "",
        payload.get("udf2") or "", payload.get("udf1") or "",
        payload.get("email") or "", payload.get("firstname") or "", payload.get("productinfo") or "",
        payload.get("amount") or "", payload.get("txnid") or "", payload.get("key") or "",
    ])
    return sha512("|".join(parts))


def safe_equal_hex(left: str, right: str) -> bool:
    if not re.fullmatch(r"[a-f0-9]{128}", str(left), re.I) or not re.fullmatch(r"[a-f0-9]{128}", str(right), re.I):
        return False
    return hmac.compare_digest(str(left).lower(), str(right).lower())


def verify_callback(*, payload, intent, customer, config) -> dict:
    cfg = validate_config(config)
    if not payload or not intent or not customer:
        return {"valid": False, "creditable": False, "reason": "missing data"}
    if not safe_equal_hex(payload.get("hash"), callback_hash(payload, cfg["salt"])):
        return {"valid": False, "creditable": False, "reason": "invalid hash"}
    expected = {
        "key": cfg["key"],
        "txnid": intent["txnid"],
        "amount": canonical_amount(intent["amount"]),
        "productinfo": intent["productinfo"],
        "firstname": customer["firstname"],
        "email": customer["email"],
        "udf1": intent["intentToken"],
    }
    for key, value in expected.items():
        if str(payload.get(key) or "") != str(value):
            return {"valid": False, "creditable": False, "reason": f"mismatched {key}"}
    captured = str(payload.get("status")).lower() == "success" and str(payload.get("unmappedstatus") or "").lower() == "captured"
    return {"valid": True, "creditable": captured, "reason": "requires verify_payment" if captured else "not captured"}


def build_verify_payment_form(txnid: str, config: dict) -> dict:
    cfg = validate_config(config)
    command = "verify_payment"
    var1 = required_string(txnid, "txnid", 50)
    return {
        "url": cfg["postserviceUrl"],
        "fields": {
            "key": cfg["key"],
            "command": command,
            "var1": var1,
            "hash": sha512(f"{cfg['key']}|{command}|{var1}|{cfg['salt']}"),
        },
    }


def post_form(url: str, fields: dict, timeout: float = 15) -> dict:
    response = httpx.post(url, data=fields, timeout=timeout)
    return {"status": response.status_code, "body": response.text}


def verify_payment(*, intent, config, transport=post_form) -> dict:
    request = build_verify_payment_form(intent["txnid"], config)
    response = transport(request["url"], request["fields"])
    if not response or response.get("status", 0) < 200 or response.get("status", 0) >= 300:
        return {"verified": False, "reason": "PayU HTTP failure"}
    try:
        parsed = json.loads(response["body"]) if isinstance(response.get("body"), str) else response.get("body")
    except json.JSONDecodeError:
        return {"verified": False, "reason": "invalid PayU JSON"}
    details = (parsed or {}).get("transaction_details") or {}
    detail = details.get(intent["txnid"])
    if not detail:
        return {"verified": False, "reason": "transaction not found"}
    amount = detail.get("amt", detail.get("amount"))
    state = str(detail.get("unmappedstatus") or detail.get("status") or "").lower()
    payu_id = detail.get("mihpayid", detail.get("mihpayupid"))
    try:
        if canonical_amount(amount) != canonical_amount(intent["amount"]):
            return {"verified": False, "reason": "amount mismatch"}
    except ValueError:
        return {"verified": False, "reason": "amount mismatch"}
    if state != "captured":
        return {"verified": False, "reason": f"not captured, {state or 'unknown'}"}
    if not payu_id:
        return {"verified": False, "reason": "missing PayU ID"}
    return {"verified": True, "reason": "captured", "payuId": str(payu_id), "detail": detail}


def classify_browser_return(_payload=None) -> dict:
    return {"credit": False, "status": "pending_verification"}


def form_body(pairs: list[tuple[str, str]]) -> str:
    return urlencode(pairs)
