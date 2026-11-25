import json
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from django.core.mail import send_mail
from django.test import SimpleTestCase, override_settings


@override_settings(
    EMAIL_BACKEND="config.brevo_email.BrevoEmailBackend",
    PENNY_BREVO_API_KEY="test-only-api-key",
    DEFAULT_FROM_EMAIL="sender@example.com",
)
class BrevoEmailBackendTests(SimpleTestCase):
    @patch("config.brevo_email.request.urlopen")
    def test_sends_plain_text_with_bounded_https_request(self, open_url: MagicMock) -> None:
        open_url.return_value.__enter__.return_value.status = 201

        self.assertEqual(
            send_mail("Your Penny code", "Code: 123456", None, ["reader@example.com"]), 1
        )

        call = open_url.call_args.args[0]
        self.assertEqual(call.full_url, "https://api.brevo.com/v3/smtp/email")
        self.assertEqual(call.get_method(), "POST")
        self.assertEqual(open_url.call_args.kwargs["timeout"], 10)
        self.assertEqual(call.get_header("Api-key"), "test-only-api-key")
        self.assertEqual(
            json.loads(call.data),
            {
                "sender": {"email": "sender@example.com"},
                "to": [{"email": "reader@example.com"}],
                "subject": "Your Penny code",
                "textContent": "Code: 123456",
            },
        )

    @patch("config.brevo_email.request.urlopen")
    def test_provider_errors_do_not_expose_private_response(self, open_url: MagicMock) -> None:
        open_url.side_effect = HTTPError(
            "https://api.brevo.com/v3/smtp/email", 401, "private provider detail", None, None
        )

        with self.assertRaisesMessage(RuntimeError, "Brevo email delivery failed.") as caught:
            send_mail("Code", "123456", None, ["reader@example.com"])
        self.assertNotIn("private provider detail", str(caught.exception))
