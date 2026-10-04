PRESETS = [
    {
        "id": "preset_personal_injury_v1",
        "slug": "personal-injury-intake",
        "name": "Personal Injury Intake",
        "isSystem": True,
        "greeting": "Thank you for calling. I am an AI intake assistant and this call may be recorded. Are you in immediate danger or need emergency medical help?",
        "fields": ["caller_name", "callback_number", "incident_date", "injuries", "preferred_appointment"],
        "guardrails": ["No legal advice", "No case valuation", "Escalate emergencies"],
    },
    {
        "id": "preset_receptionist_v1",
        "slug": "general-receptionist",
        "name": "AI Receptionist",
        "isSystem": True,
        "greeting": "Thank you for calling. I am the AI receptionist. How may I direct your call today?",
        "fields": ["caller_name", "callback_number", "reason", "preferred_follow_up"],
        "guardrails": ["Disclose AI identity", "Escalate emergencies"],
    },
]


def find_preset(preset_id: str | None):
    if not preset_id:
        return None
    return next((item for item in PRESETS if item["id"] == preset_id), None)
