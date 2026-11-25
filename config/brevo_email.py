"""Send Penny's plain-text account emails through Brevo's HTTPS API."""

import json
from urllib import error, request

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend

BREVO_EMAIL_URL = "https://api.brevo.com/v3/smtp/email"


class BrevoEmailBackend(BaseEmailBackend):
    def send_messages(self, email_messages) -> int:
        sent = 0
        for message in email_messages:
            if not message.recipients():
                continue
            payload = {
                "sender": {"email": message.from_email},
                "to": [{"email": recipient} for recipient in message.to],
                "subject": message.subject,
                "textContent": message.body,
            }
            call = request.Request(
                BREVO_EMAIL_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "api-key": settings.PENNY_BREVO_API_KEY,
                    "accept": "application/json",
                    "content-type": "application/json",
                },
                method="POST",
            )
            try:
                with request.urlopen(call, timeout=10) as response:
                    if response.status != 201:
                        raise RuntimeError("Brevo rejected the email request.")
            except error.URLError, TimeoutError, OSError, RuntimeError:
                if not self.fail_silently:
                    raise RuntimeError("Brevo email delivery failed.") from None
                continue
            sent += 1
        return sent
