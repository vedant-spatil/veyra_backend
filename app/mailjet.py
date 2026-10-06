"""Send the sign-up code. The plaintext code is not stored."""

import httpx

from app.settings import get_settings

MAILJET_URL = "https://api.mailjet.com/v3.1/send"


class MailjetError(Exception):
    pass


def send_signup_code(email: str, code: str) -> None:
    settings = get_settings()
    if not settings.mailjet_api_key or not settings.mailjet_secret_key or not settings.mailjet_from_email:
        raise MailjetError("mailjet is not configured")
    response = httpx.post(
        MAILJET_URL,
        auth=(settings.mailjet_api_key, settings.mailjet_secret_key),
        json={
            "Messages": [{
                "From": {"Email": settings.mailjet_from_email, "Name": "Veyra"},
                "To": [{"Email": email}],
                "Subject": "Your Veyra sign-up code",
                "TextPart": f"Your Veyra sign-up code is {code}. It expires in 10 minutes.",
            }]
        },
        timeout=15,
    )
    if response.status_code >= 400:
        raise MailjetError("mailjet send failed")
