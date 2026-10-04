from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app import payu
from app.db import get_db
from app.deps import require_user
from app.ids import gen_id
from app.ledger import add_audit, add_ledger
from app.models import Ledger, PaymentIntent, Wallet
from app.serialize import public_intent, public_wallet
from app.settings import get_settings

router = APIRouter()

CREDIT_PACKS = {
    "starter": {"amount": "200.00", "currency": "INR", "credits": 20000, "productinfo": "Veyra Starter Credits"},
    "growth": {"amount": "500.00", "currency": "INR", "credits": 50000, "productinfo": "Veyra Growth Credits"},
    "scale": {"amount": "1000.00", "currency": "INR", "credits": 100000, "productinfo": "Veyra Scale Credits"},
}


def payu_config() -> dict | None:
    settings = get_settings()
    if not settings.payu_key or not settings.payu_salt:
        return None
    return {"key": settings.payu_key, "salt": settings.payu_salt, "env": "production" if settings.payu_env == "production" else "test"}


@router.get("/api/wallet")
def wallet(principal=Depends(require_user), db: Session = Depends(get_db)):
    _user, tenant = principal
    row = db.query(Wallet).filter_by(tenant_id=tenant.id).one_or_none()
    ledger = db.query(Ledger).filter_by(tenant_id=tenant.id).order_by(Ledger.created_at.desc()).limit(100).all()
    return {
        "wallet": public_wallet(row, tenant.id),
        "ledger": [
            {
                "id": item.id,
                "type": item.entry_type,
                "amountPaise": item.amount_paise,
                "balanceAfterPaise": item.balance_after_paise,
                "createdAt": item.created_at.isoformat(),
            }
            for item in ledger
        ],
    }


@router.get("/api/payment-intents")
def list_intents(principal=Depends(require_user), db: Session = Depends(get_db)):
    _user, tenant = principal
    rows = db.query(PaymentIntent).filter_by(tenant_id=tenant.id).order_by(PaymentIntent.created_at.desc()).all()
    return {"paymentIntents": [public_intent(row) for row in rows]}


@router.post("/api/payment-intents", status_code=201)
def create_intent(body: dict, principal=Depends(require_user), db: Session = Depends(get_db)):
    user, tenant = principal
    try:
        base = payu.create_payment_intent(pack_id=str(body.get("packId") or ""), packs=CREDIT_PACKS, tenant_id=tenant.id, user_id=user.id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"error": str(exc), "code": "bad_pack"}) from exc
    customer = {
        "firstname": str(body.get("firstname") or user.name or "Customer").strip()[:60],
        "email": user.email,
        "phone": str(body.get("phone") or "").strip()[:20],
    }
    now = datetime.now(timezone.utc)
    intent = PaymentIntent(
        id=gen_id("pay_"),
        tenant_id=tenant.id,
        user_id=user.id,
        txnid=base["txnid"],
        intent_token=base["intentToken"],
        pack_id=base["packId"],
        amount=base["amount"],
        currency=base["currency"],
        credits=base["credits"],
        productinfo=base["productinfo"],
        status="pending",
        customer=customer,
        amount_paise=int(round(float(base["amount"]) * 100)),
        created_at=now,
        updated_at=now,
    )
    checkout = None
    cfg = payu_config()
    origin = get_settings().public_base_url.rstrip("/")
    if cfg and origin.startswith("https://"):
        try:
            checkout = payu.build_checkout(
                intent={"txnid": intent.txnid, "amount": intent.amount, "productinfo": intent.productinfo, "intentToken": intent.intent_token, "status": "pending"},
                customer=customer,
                success_url=f"{origin}/api/payu/callback",
                failure_url=f"{origin}/api/payu/return",
                config=cfg,
            )
        except ValueError as exc:
            raise HTTPException(status_code=503, detail={"error": "PayU checkout configuration is invalid", "code": "payu_config"}) from exc
    db.add(intent)
    add_audit(db, tenant.id, user.id, "billing.payment_intent.created", "payment_intent", intent.id, {"packId": intent.pack_id})
    db.commit()
    return {
        "paymentIntent": public_intent(intent),
        "checkoutReady": bool(checkout),
        "checkout": checkout,
        "message": None if checkout else "PayU is not configured. The intent is saved but cannot be paid yet.",
    }


@router.post("/api/payu/callback")
async def payu_callback(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    payload = {key: str(value) for key, value in form.items()}
    cfg = payu_config()
    if not cfg:
        raise HTTPException(status_code=503, detail={"error": "PayU is not configured", "code": "payu_unavailable"})
    intent = db.query(PaymentIntent).filter_by(txnid=str(payload.get("txnid") or "")).one_or_none()
    if not intent:
        raise HTTPException(status_code=404, detail={"error": "payment intent not found", "code": "not_found"})
    callback = payu.verify_callback(payload=payload, intent={
        "txnid": intent.txnid,
        "amount": intent.amount,
        "productinfo": intent.productinfo,
        "intentToken": intent.intent_token,
    }, customer=intent.customer, config=cfg)
    if not callback["valid"] or not callback["creditable"]:
        raise HTTPException(status_code=400, detail={"error": callback["reason"], "code": "payu_callback_rejected"})
    verification = payu.verify_payment(intent={"txnid": intent.txnid, "amount": intent.amount}, config=cfg)
    if not verification["verified"]:
        raise HTTPException(status_code=409, detail={"error": verification["reason"], "code": "payu_not_verified"})
    duplicate = intent.status == "credited"
    if not duplicate:
        entry = add_ledger(db, intent.tenant_id, int(intent.credits), "payment_credit", f"payu:{intent.txnid}", intent.user_id, {"paymentIntentId": intent.id, "payuId": verification["payuId"]})
        intent.status = "credited"
        intent.payu_id = verification["payuId"]
        intent.updated_at = datetime.now(timezone.utc)
        if entry is None:
            duplicate = True
        db.commit()
    return {"ok": True, "credited": not duplicate, "duplicate": duplicate}


@router.post("/api/payu/return")
async def payu_return(request: Request):
    form = await request.form()
    payload = {key: str(value) for key, value in form.items()}
    result = payu.classify_browser_return(payload)
    return JSON_PENDING(result)


def JSON_PENDING(result: dict):
    from fastapi.responses import JSONResponse
    return JSONResponse({**result, "message": "Payment is pending server verification. A browser return never credits the wallet."}, status_code=202)
