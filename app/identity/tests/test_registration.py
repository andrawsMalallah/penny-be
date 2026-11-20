import re
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.hashers import check_password
from django.core import mail
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from app.identity.models import EmailVerification, Registration, User
from app.identity.services.registration import prepare_code, send_registration_code


class RegistrationTests(TestCase):
    def setUp(self) -> None:
        self.client = APIClient(enforce_csrf_checks=True)
        self.data = {
            "display_name": "Test Person",
            "email": "test@example.com",
            "password": "A long private phrase 487!",
            "language": "en",
            "timezone": "Asia/Baghdad",
        }
        self.refresh_csrf()

    def refresh_csrf(self) -> None:
        response = self.client.get("/api/v1/auth/csrf/")
        self.csrf = response.json()["csrf_token"]

    def post(self, path: str, data: dict | None = None):
        return self.client.post(
            f"/api/v1/auth/{path}/", data or {}, format="json", HTTP_X_CSRFTOKEN=self.csrf
        )

    def register_and_receive(self) -> str:
        self.assertEqual(self.post("register", self.data).status_code, 202)
        return re.search(r"[0-9]{6}", mail.outbox[-1].body).group()

    def test_registration_sends_without_worker_but_does_not_create_user(self) -> None:
        code = self.register_and_receive()
        self.assertFalse(User.objects.exists())
        draft = Registration.objects.get()
        self.assertTrue(check_password(self.data["password"], draft.password_hash))
        challenge = EmailVerification.objects.get()
        self.assertTrue(check_password(code, challenge.code_hash))
        self.assertNotEqual(code, challenge.code_hash)
        self.assertEqual(self.client.get("/api/v1/auth/me/").status_code, 403)

    def test_verification_creates_private_session_and_consumes_code(self) -> None:
        code = self.register_and_receive()
        self.assertEqual(self.post("verify-email", {"code": code}).status_code, 200)
        user = User.objects.get()
        self.assertTrue(user.email_verified)
        self.assertTrue(user.check_password(self.data["password"]))
        self.assertNotIn("password", self.client.get("/api/v1/auth/me/").json())
        self.assertEqual(EmailVerification.objects.get().code_hash, "")
        self.assertEqual(Registration.objects.get().password_hash, "")
        self.refresh_csrf()
        self.assertEqual(self.post("verify-email", {"code": code}).status_code, 400)
        self.assertEqual(self.post("logout").status_code, 204)
        self.assertEqual(self.client.get("/api/v1/auth/me/").status_code, 403)

    def test_five_failures_commit_and_invalidate_code_without_extending_expiry(self) -> None:
        code = self.register_and_receive()
        wrong_code = "111111" if code != "111111" else "222222"
        expires_at = EmailVerification.objects.get().expires_at
        for attempt in range(5):
            self.assertEqual(self.post("verify-email", {"code": wrong_code}).status_code, 400)
            challenge = EmailVerification.objects.get()
            self.assertEqual(challenge.failed_attempts, attempt + 1)
            self.assertEqual(challenge.expires_at, expires_at)
        self.assertEqual(self.post("verify-email", {"code": code}).status_code, 400)
        self.assertFalse(User.objects.exists())

    def test_expired_code_cannot_create_user(self) -> None:
        code = self.register_and_receive()
        EmailVerification.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.post("verify-email", {"code": code}).status_code, 400)
        self.assertFalse(User.objects.exists())

    def test_replacement_invalidates_code_and_keeps_request_limit(self) -> None:
        with patch(
            "app.identity.services.registration.secrets.randbelow",
            side_effect=[111111, 222222, 333333],
        ):
            code = self.register_and_receive()
            self.assertEqual(self.post("resend-code").status_code, 202)
            self.assertEqual(self.post("verify-email", {"code": code}).status_code, 400)
            self.assertEqual(self.post("resend-code").status_code, 202)
        self.assertEqual(self.post("resend-code").status_code, 429)

    def test_new_draft_cannot_use_previous_draft_code(self) -> None:
        with patch(
            "app.identity.services.registration.secrets.randbelow", side_effect=[111111, 222222]
        ):
            code = self.register_and_receive()
            self.assertEqual(self.post("register", self.data).status_code, 202)
        self.assertEqual(self.post("verify-email", {"code": code}).status_code, 400)
        self.assertEqual(self.post("verify-email", {"code": "222222"}).status_code, 200)

    def test_existing_user_is_not_overwritten_or_disclosed(self) -> None:
        original = User.objects.create_user(
            self.data["email"], "Original passphrase 337!", display_name="Original", language="en"
        )
        response = self.post("register", self.data)
        self.assertEqual(response.status_code, 202)
        self.assertFalse(hasattr(mail, "outbox") and mail.outbox)
        original.refresh_from_db()
        self.assertEqual(original.display_name, "Original")
        self.assertTrue(original.check_password("Original passphrase 337!"))

    def test_validation_rejects_weak_password_and_bad_timezone(self) -> None:
        response = self.post(
            "register", {**self.data, "password": "123", "timezone": "Invalid/Zone"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Registration.objects.exists())
        self.assertIn("password", self.post("register", {**self.data, "password": "123"}).json())

    def test_code_requires_exactly_six_ascii_digits(self) -> None:
        for code in ["12345", "1234567", "123456\n", "١٢٣٤٥٦", "abcdef"]:
            self.assertEqual(self.post("verify-email", {"code": code}).status_code, 400)

    def test_anonymous_mutations_require_csrf_and_no_store(self) -> None:
        anonymous = APIClient(enforce_csrf_checks=True)
        for endpoint in ["register", "verify-email", "resend-code"]:
            response = anonymous.post(f"/api/v1/auth/{endpoint}/", self.data, format="json")
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response["Cache-Control"], "no-store")

    def test_normalized_email_uniqueness_is_enforced_in_database(self) -> None:
        self.post("register", {**self.data, "email": "TEST@EXAMPLE.COM"})
        self.assertEqual(Registration.objects.get().email, "test@example.com")
        User.objects.create_user("test@example.com", language="en")
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create(email="TEST@EXAMPLE.COM", language="en")

    def test_new_device_cannot_verify_another_devices_draft(self) -> None:
        code = self.register_and_receive()
        other = APIClient(enforce_csrf_checks=True)
        token = other.get("/api/v1/auth/csrf/").json()["csrf_token"]
        response = other.post(
            "/api/v1/auth/verify-email/", {"code": code}, format="json", HTTP_X_CSRFTOKEN=token
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.exists())

    def test_request_limit_is_shared_across_browser_sessions(self) -> None:
        for _ in range(3):
            self.assertEqual(self.post("register", self.data).status_code, 202)
        other = APIClient(enforce_csrf_checks=True)
        token = other.get("/api/v1/auth/csrf/").json()["csrf_token"]
        response = other.post(
            "/api/v1/auth/register/", self.data, format="json", HTTP_X_CSRFTOKEN=token
        )
        self.assertEqual(response.status_code, 429)

    def test_unverified_session_is_not_authorized_for_workspace(self) -> None:
        unverified = User.objects.create_user("unverified@example.com", language="en")
        self.client.force_login(unverified)
        self.assertEqual(self.client.get("/api/v1/auth/me/").status_code, 403)

    def test_allowance_returns_after_one_hour(self) -> None:
        current_time = timezone.now()
        for _ in range(3):
            self.assertEqual(self.post("register", self.data).status_code, 202)
        self.assertEqual(self.post("register", self.data).status_code, 429)
        with patch(
            "app.identity.services.registration.timezone.now",
            return_value=current_time + timedelta(hours=1, seconds=1),
        ):
            self.assertEqual(self.post("register", self.data).status_code, 202)

    def test_smtp_failure_has_generic_response_and_counts_toward_limit(self) -> None:
        with patch(
            "app.identity.services.registration.send_mail",
            side_effect=RuntimeError("private credentials"),
        ):
            for _ in range(3):
                response = self.post("register", self.data)
                self.assertEqual(response.status_code, 503)
                self.assertNotIn("private credentials", response.content.decode())
        self.assertEqual(self.post("register", self.data).status_code, 429)
        self.assertEqual(EmailVerification.objects.get().code_hash, "")
        self.assertFalse(User.objects.exists())

    def test_failed_resend_invalidates_old_code(self) -> None:
        code = self.register_and_receive()
        with patch("app.identity.services.registration.send_mail", side_effect=TimeoutError):
            self.assertEqual(self.post("resend-code").status_code, 503)
        self.assertEqual(self.post("verify-email", {"code": code}).status_code, 400)

    def test_old_delivery_cannot_reactivate_superseded_challenge_for_same_draft(self) -> None:
        self.register_and_receive()
        draft = Registration.objects.get()
        old_code, old_generation = prepare_code(draft)
        new_code, new_generation = prepare_code(draft)
        send_registration_code(draft, new_code, new_generation)
        send_registration_code(draft, old_code, old_generation)
        self.assertTrue(check_password(new_code, EmailVerification.objects.get().code_hash))

    def test_verification_does_not_read_cached_user_profile(self) -> None:
        code = self.register_and_receive()
        self.post("verify-email", {"code": code})
        User.objects.update(display_name="Changed in database")
        response = self.client.get("/api/v1/auth/me/")
        self.assertEqual(response.json()["display_name"], "Changed in database")
        self.assertIn("no-store", response["Cache-Control"])
