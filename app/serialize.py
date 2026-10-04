from app.dograh import empty_extracted
from app.ids import iso
from app.models import Agent, Call, HvacJob, PaymentIntent, Tenant, User, Wallet


def public_user(user: User) -> dict:
    return {
        "id": user.id,
        "tenantId": user.tenant_id,
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "status": user.status,
        "createdAt": iso(user.created_at),
    }


def public_tenant(tenant: Tenant) -> dict:
    return {
        "id": tenant.id,
        "name": tenant.name,
        "slug": tenant.slug,
        "createdAt": iso(tenant.created_at),
        "branding": tenant.branding or {},
        "plan": tenant.plan,
        "status": tenant.status,
        "privacyMode": tenant.privacy_mode,
    }


def public_agent(agent: Agent) -> dict:
    return {
        "id": agent.id,
        "name": agent.name,
        "persona": agent.persona,
        "tts": agent.tts or {},
        "greeting": agent.greeting,
        "telephony": agent.telephony or {},
        "presetId": agent.preset_id or None,
        "createdAt": iso(agent.created_at),
    }


def public_wallet(wallet: Wallet | None, tenant_id: str) -> dict:
    balance = wallet.balance_paise if wallet else 0
    return {
        "id": wallet.id if wallet else None,
        "tenantId": tenant_id,
        "currency": wallet.currency if wallet else "INR",
        "balancePaise": balance,
        "balanceInr": balance / 100,
        "updatedAt": iso(wallet.updated_at) if wallet else None,
    }


def public_intent(intent: PaymentIntent) -> dict:
    return {
        "id": intent.id,
        "provider": intent.provider,
        "txnid": intent.txnid,
        "packId": intent.pack_id,
        "amount": intent.amount,
        "currency": intent.currency,
        "credits": intent.credits,
        "productinfo": intent.productinfo,
        "status": intent.status,
        "amountPaise": intent.amount_paise,
        "createdAt": iso(intent.created_at),
        "updatedAt": iso(intent.updated_at),
    }


def public_hvac(job: HvacJob) -> dict:
    return {
        "id": job.id,
        "callerName": job.caller_name,
        "phone": job.phone,
        "email": job.email or "",
        "service": job.service,
        "urgency": job.urgency,
        "outcome": job.outcome,
        "assignedTo": job.assigned_to or "",
        "notes": job.notes or "",
        "appointment": job.appointment,
        "createdAt": iso(job.created_at),
        "updatedAt": iso(job.updated_at),
    }


def public_call(call: Call) -> dict:
    extracted = empty_extracted()
    extracted.update(call.extracted or {})
    return {
        "id": call.id,
        "to": call.to_number,
        "variables": call.variables or {},
        "workflowId": call.workflow_id,
        "telephonyConfigurationId": call.telephony_configuration_id,
        "fromPhoneNumberId": call.from_phone_number_id,
        "dograhRunId": call.dograh_run_id or None,
        "status": call.status,
        "recordingUrl": call.recording_url or None,
        "transcript": call.transcript or "",
        "extracted": extracted,
        "error": call.error or "",
        "createdAt": iso(call.created_at),
    }
