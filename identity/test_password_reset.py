import re
from datetime import timedelta
from unittest.mock import patch

from django.core import mail
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from .models import CodeRequestLimit, PasswordReset, SignedInSession, User


class PasswordResetTests(TestCase):
    def setUp(self) -> None:
        self.old_password = "Old private phrase 371!"
        self.new_password = "New private phrase 582!"
        self.user = User.objects.create_user(
            "recovery@example.com",
            self.old_password,
            display_name="Recovery test",
            language="en",
            email_verified=True,
        )
        self.client = APIClient(enforce_csrf_checks=True)

    def post(self, path: str, body: dict, client: APIClient | None = None):
        client = client or self.client
        token = client.get("/api/v1/auth/csrf/").json()["csrf_token"]
        return client.post(f"/api/v1/auth/{path}", body, format="json", HTTP_X_CSRFTOKEN=token)

    def request_code(self, client: APIClient | None = None) -> str:
        response = self.post("password-reset/", {"email": self.user.email}, client)
        self.assertEqual(response.status_code, 202)
        return re.search(r"[0-9]{6}", mail.outbox[-1].body).group()

    def verify(self, code: str, client: APIClient | None = None):
        return self.post("password-reset/verify/", {"code": code}, client)

    def complete(self, password: str | None = None, client: APIClient | None = None):
        return self.post(
            "password-reset/complete/", {"password": password or self.new_password}, client
        )

    def test_full_flow_revokes_all_sessions_and_requires_new_password(self) -> None:
        devices = [APIClient(enforce_csrf_checks=True) for _ in range(2)]
        for device in devices:
            self.assertEqual(
                self.post(
                    "login/", {"email": self.user.email, "password": self.old_password}, device
                ).status_code,
                200,
            )
        other_user = User.objects.create_user(
            "other@example.com", self.old_password, language="en", email_verified=True
        )
        other_device = APIClient()
        other_device.force_login(other_user)
        code = self.request_code(devices[0])
        challenge = PasswordReset.objects.get(user=self.user)
        self.assertNotEqual(challenge.code_hash, code)
        self.assertTrue(challenge.code_hash)
        self.assertEqual(self.verify(code, devices[0]).status_code, 204)
        self.assertEqual(self.complete(client=devices[0]).status_code, 204)
        self.assertFalse(SignedInSession.objects.filter(user=self.user).exists())
        for device in devices:
            self.assertEqual(device.get("/api/v1/auth/me/").status_code, 403)
        self.assertEqual(other_device.get("/api/v1/auth/me/").status_code, 200)
        self.assertEqual(
            self.post(
                "login/", {"email": self.user.email, "password": self.old_password}
            ).status_code,
            401,
        )
        self.assertEqual(
            self.post(
                "login/", {"email": self.user.email, "password": self.new_password}
            ).status_code,
            200,
        )
        challenge.refresh_from_db()
        self.assertFalse(challenge.verified)
        self.assertEqual(challenge.code_hash, "")
        self.assertIsNone(challenge.expires_at)

    def test_unknown_unverified_inactive_and_delivery_failure_have_generic_response(self) -> None:
        unknown = self.post("password-reset/", {"email": "unknown@example.com"})
        for changes in [{"email_verified": False}, {"email_verified": True, "is_active": False}]:
            User.objects.filter(pk=self.user.pk).update(**changes)
            response = self.post("password-reset/", {"email": self.user.email})
            self.assertEqual(
                (response.status_code, response.json()), (unknown.status_code, unknown.json())
            )
        self.assertEqual(len(mail.outbox), 0)
        User.objects.filter(pk=self.user.pk).update(is_active=True, email_verified=True)
        with patch(
            "identity.password_reset.send_mail", side_effect=RuntimeError("private SMTP error")
        ):
            response = self.post("password-reset/", {"email": self.user.email})
        self.assertEqual(response.json(), unknown.json())
        self.assertEqual(response.status_code, 202)
        self.assertEqual(PasswordReset.objects.get().code_hash, "")
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(self.post("password-reset/", {"email": self.user.email}).status_code, 429)

    def test_normalizes_email_and_uses_user_language(self) -> None:
        User.objects.filter(pk=self.user.pk).update(language="ar")
        self.assertEqual(
            self.post("password-reset/", {"email": self.user.email.upper()}).status_code, 202
        )
        self.assertIn("إعادة تعيين", mail.outbox[-1].subject)

    def test_five_wrong_attempts_persist_and_invalidate_the_code(self) -> None:
        code = self.request_code()
        wrong = "000000" if code != "000000" else "111111"
        for attempt in range(1, 6):
            self.assertEqual(self.verify(wrong).status_code, 400)
            self.assertEqual(PasswordReset.objects.get().failed_attempts, attempt)
        self.assertEqual(self.verify(code).status_code, 400)
        self.assertEqual(PasswordReset.objects.get().code_hash, "")

    def test_expiry_applies_to_code_and_verified_proof(self) -> None:
        code = self.request_code()
        PasswordReset.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.verify(code).status_code, 400)
        code = self.request_code()
        self.assertEqual(self.verify(code).status_code, 204)
        PasswordReset.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.complete().status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.old_password))

    def test_resend_invalidates_code_and_previously_verified_browser(self) -> None:
        first = self.request_code()
        self.assertEqual(self.verify(first).status_code, 204)
        another_browser = APIClient(enforce_csrf_checks=True)
        self.request_code(another_browser)
        self.assertEqual(self.complete().status_code, 400)
        self.assertEqual(self.verify(first).status_code, 400)

    def test_unverified_code_and_another_browser_cannot_set_password(self) -> None:
        code = self.request_code()
        self.assertEqual(self.complete().status_code, 400)
        other = APIClient(enforce_csrf_checks=True)
        self.assertEqual(self.verify(code, other).status_code, 400)
        self.assertEqual(self.verify(code).status_code, 204)
        self.assertEqual(self.complete(client=other).status_code, 400)
        self.assertEqual(self.complete().status_code, 204)
        self.assertEqual(self.complete().status_code, 400)
        self.assertEqual(self.verify(code).status_code, 400)

    def test_password_policy_and_old_password_reuse_leave_proof_available_for_retry(self) -> None:
        self.assertEqual(self.verify(self.request_code()).status_code, 204)
        for password in ["short", "12345678901234", self.user.email, self.old_password]:
            response = self.complete(password)
            self.assertEqual(response.status_code, 400)
            self.assertIn("password", response.json())
        self.assertEqual(self.complete().status_code, 204)

    def test_request_limits_apply_across_browsers_and_to_unknown_addresses(self) -> None:
        for _ in range(3):
            self.assertEqual(
                self.post(
                    "password-reset/",
                    {"email": "unknown@example.com"},
                    APIClient(enforce_csrf_checks=True),
                ).status_code,
                202,
            )
        self.assertEqual(
            self.post("password-reset/", {"email": "UNKNOWN@example.com"}).status_code, 429
        )
        CodeRequestLimit.objects.all().delete()
        for number in range(20):
            self.assertEqual(
                self.post(
                    "password-reset/", {"email": f"unknown-{number}@example.com"}
                ).status_code,
                202,
            )
        self.assertEqual(
            self.post("password-reset/", {"email": "last@example.com"}).status_code, 429
        )

    def test_all_endpoints_require_csrf_even_for_anonymous_visitors(self) -> None:
        for path, body in [
            ("password-reset/", {"email": self.user.email}),
            ("password-reset/verify/", {"code": "123456"}),
            ("password-reset/complete/", {"password": self.new_password}),
        ]:
            response = APIClient(enforce_csrf_checks=True).post(
                f"/api/v1/auth/{path}", body, format="json"
            )
            self.assertEqual(response.status_code, 403)

    def test_registration_and_recovery_codes_are_not_interchangeable(self) -> None:
        with patch("identity.services.secrets.randbelow", return_value=123456):
            response = self.post(
                "register/",
                {
                    "email": "draft@example.com",
                    "display_name": "Draft test",
                    "password": self.old_password,
                    "language": "en",
                    "timezone": "UTC",
                },
            )
        self.assertEqual(response.status_code, 202)
        with patch("identity.password_reset.secrets.randbelow", return_value=654321):
            reset_code = self.request_code()
        self.assertEqual(self.verify("123456").status_code, 400)
        self.assertEqual(self.post("verify-email/", {"code": reset_code}).status_code, 400)
        self.assertEqual(self.verify(reset_code).status_code, 204)

    def test_disabling_user_after_verification_blocks_completion(self) -> None:
        self.assertEqual(self.verify(self.request_code()).status_code, 204)
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertEqual(self.complete().status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.old_password))

    def test_failed_replacement_and_superseded_smtp_cannot_reactivate_code(self) -> None:
        first = self.request_code()
        with patch("identity.password_reset.send_mail", return_value=0):
            self.post("password-reset/", {"email": self.user.email})
        self.assertEqual(self.verify(first).status_code, 400)

        def superseded(*args, **kwargs):
            import uuid

            PasswordReset.objects.update(generation=uuid.uuid4())
            return 1

        with patch("identity.password_reset.send_mail", side_effect=superseded):
            self.post("password-reset/", {"email": self.user.email})
        self.assertEqual(PasswordReset.objects.get().code_hash, "")
