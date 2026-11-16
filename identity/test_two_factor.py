import base64
import re
import time
from datetime import timedelta

import pyotp
from django.conf import settings
from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from .models import CodeRequestLimit, TwoFactorLogin, TwoFactorSettings, User
from .sessions import start_user_session
from .two_factor import LOGIN_KEY


class TwoFactorTests(TestCase):
    def setUp(self) -> None:
        self.password = "Private authenticator phrase 317!"
        self.user = User.objects.create_user(
            "factor@example.com",
            self.password,
            language="en",
            email_verified=True,
        )
        self.client = APIClient(enforce_csrf_checks=True)
        self.assertEqual(self.login(self.client).status_code, 200)

    def post(self, path: str, body: dict, client: APIClient | None = None):
        client = client or self.client
        token = client.get("/api/v1/auth/csrf/").json()["csrf_token"]
        return client.post(f"/api/v1/auth/{path}/", body, format="json", HTTP_X_CSRFTOKEN=token)

    def login(self, client: APIClient):
        return self.post("login", {"email": self.user.email, "password": self.password}, client)

    def setup_factor(self) -> str:
        response = self.post("2fa/setup", {"password": self.password})
        self.assertEqual(response.status_code, 200)
        return response.json()["secret"]

    def enable_factor(self) -> tuple[str, list[str]]:
        secret = self.setup_factor()
        response = self.post("2fa/enable", {"otp": pyotp.TOTP(secret).now()})
        self.assertEqual(response.status_code, 200)
        return secret, response.json()["recovery_codes"]

    def test_setup_is_encrypted_private_and_enable_requires_code(self) -> None:
        self.assertEqual(self.post("2fa/setup", {"password": "wrong"}).status_code, 401)
        response = self.post("2fa/setup", {"password": self.password})
        secret = response.json()["secret"]
        record = TwoFactorSettings.objects.get(user=self.user)
        self.assertNotIn(secret, record.pending_ciphertext)
        self.assertFalse(record.enabled)
        self.assertEqual(self.post("2fa/enable", {"otp": "000000"}).status_code, 400)
        self.assertFalse(self.client.get("/api/v1/auth/2fa/").json()["enabled"])
        svg = base64.b64decode(response.json()["qr_code"].split(",", 1)[1])
        self.assertIn(b"<svg", svg)
        self.assertIn("no-store", response["Cache-Control"])

    def test_enable_shows_ten_codes_once_preserves_current_and_revokes_other_sessions(self) -> None:
        other = APIClient()
        self.assertEqual(self.login(other).status_code, 200)
        secret, codes = self.enable_factor()
        self.assertEqual(len(set(codes)), 10)
        record = TwoFactorSettings.objects.get(user=self.user)
        self.assertNotIn(secret, record.secret_ciphertext)
        self.assertFalse(record.pending_ciphertext)
        self.assertEqual(self.client.get("/api/v1/auth/me/").status_code, 200)
        self.assertEqual(other.get("/api/v1/auth/me/").status_code, 403)
        status = self.client.get("/api/v1/auth/2fa/")
        self.assertEqual(status.json(), {"enabled": True, "recovery_remaining": 10})
        for code in codes:
            self.assertNotIn(code, str(record.recovery_hashes))
            self.assertNotIn(code, status.content.decode())
        self.assertNotIn("secret", self.client.get("/api/v1/auth/me/").json())

    def test_password_step_has_no_authenticated_session_then_totp_completes_login(self) -> None:
        secret, _ = self.enable_factor()
        device = APIClient(enforce_csrf_checks=True)
        self.assertEqual(self.login(device).json(), {"two_factor_required": True})
        self.assertEqual(device.get("/api/v1/auth/me/").status_code, 403)
        self.assertEqual(device.get("/api/v1/auth/sessions/").status_code, 403)
        self.assertNotIn("_auth_user_id", device.session)
        self.assertTrue(device.get("/api/v1/auth/2fa/login/").json()["pending"])
        code = pyotp.TOTP(secret).at(time.time() + 30)
        response = self.post("2fa/login", {"otp": code}, device)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], self.user.pk)
        self.assertNotIn(LOGIN_KEY, device.session)
        self.assertEqual(device.get("/api/v1/auth/me/").status_code, 200)
        self.assertEqual(self.post("2fa/login", {"otp": code}, device).status_code, 400)

    def test_totp_cannot_be_reused_after_setup_or_successful_login(self) -> None:
        secret, _ = self.enable_factor()
        device = APIClient()
        self.login(device)
        current = pyotp.TOTP(secret).now()
        self.assertEqual(self.post("2fa/login", {"otp": current}, device).status_code, 400)
        next_code = pyotp.TOTP(secret).at(time.time() + 30)
        self.assertEqual(self.post("2fa/login", {"otp": next_code}, device).status_code, 200)
        second = APIClient()
        self.login(second)
        self.assertEqual(self.post("2fa/login", {"otp": next_code}, second).status_code, 400)

    def test_recovery_code_works_once_and_does_not_disable_two_factor(self) -> None:
        _, codes = self.enable_factor()
        first = APIClient()
        self.login(first)
        self.assertEqual(
            self.post("2fa/login", {"otp": codes[0], "recovery": True}, first).status_code, 200
        )
        second = APIClient()
        self.login(second)
        self.assertEqual(
            self.post("2fa/login", {"otp": codes[0], "recovery": True}, second).status_code, 400
        )
        status = self.client.get("/api/v1/auth/2fa/").json()
        self.assertEqual(status, {"enabled": True, "recovery_remaining": 9})

    def test_setup_is_bound_to_browser_expires_and_can_be_cancelled(self) -> None:
        secret = self.setup_factor()
        other = APIClient()
        self.login(other)
        code = pyotp.TOTP(secret).now()
        self.assertEqual(self.post("2fa/enable", {"otp": code}, other).status_code, 400)
        TwoFactorSettings.objects.filter(user=self.user).update(
            pending_expires_at=timezone.now() - timedelta(seconds=1)
        )
        self.assertEqual(self.post("2fa/enable", {"otp": code}).status_code, 400)
        self.setup_factor()
        self.assertEqual(self.post("2fa/setup/cancel", {}).status_code, 204)
        self.assertFalse(TwoFactorSettings.objects.get(user=self.user).pending_ciphertext)

    def test_replacement_setup_invalidates_previous_secret(self) -> None:
        old_secret = self.setup_factor()
        new_secret = self.setup_factor()
        self.assertNotEqual(old_secret, new_secret)
        record = TwoFactorSettings.objects.get(user=self.user)
        from .two_factor import decrypt_secret

        self.assertEqual(decrypt_secret(record.pending_ciphertext), new_secret)

    def test_wrong_attempts_persist_and_fifth_failure_ends_challenge(self) -> None:
        secret, _ = self.enable_factor()
        device = APIClient()
        self.login(device)
        wrong = "000000" if not pyotp.TOTP(secret).verify("000000", valid_window=1) else "999999"
        for _ in range(5):
            self.assertEqual(self.post("2fa/login", {"otp": wrong}, device).status_code, 400)
        challenge = TwoFactorLogin.objects.get(pk=device.session[LOGIN_KEY])
        self.assertEqual(challenge.failed_attempts, 5)
        self.assertFalse(device.get("/api/v1/auth/2fa/login/").json()["pending"])
        self.assertEqual(
            self.post(
                "2fa/login", {"otp": pyotp.TOTP(secret).at(time.time() + 30)}, device
            ).status_code,
            400,
        )

    def test_shared_rate_limit_survives_wrong_passwords(self) -> None:
        for _ in range(10):
            self.assertEqual(self.post("2fa/setup", {"password": "wrong"}).status_code, 401)
        self.assertEqual(self.post("2fa/setup", {"password": self.password}).status_code, 429)
        self.assertTrue(CodeRequestLimit.objects.exists())

    def test_challenge_expires_is_browser_bound_and_cancels(self) -> None:
        _, codes = self.enable_factor()
        device = APIClient()
        self.login(device)
        unrelated = APIClient()
        self.assertEqual(
            self.post("2fa/login", {"otp": codes[0], "recovery": True}, unrelated).status_code, 400
        )
        TwoFactorLogin.objects.filter(pk=device.session[LOGIN_KEY]).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        self.assertEqual(
            self.post("2fa/login", {"otp": codes[0], "recovery": True}, device).status_code, 400
        )
        self.login(device)
        self.assertEqual(self.post("2fa/login/cancel", {}, device).status_code, 204)
        self.assertFalse(device.get("/api/v1/auth/2fa/login/").json()["pending"])

    def test_disable_requires_password_and_factor_and_revokes_other_sessions(self) -> None:
        _, codes = self.enable_factor()
        device = APIClient()
        self.login(device)
        self.post("2fa/login", {"otp": codes[0], "recovery": True}, device)
        self.assertEqual(
            self.post(
                "2fa/disable", {"password": "wrong", "otp": codes[1], "recovery": True}
            ).status_code,
            401,
        )
        self.assertEqual(
            self.post(
                "2fa/disable", {"password": self.password, "otp": codes[1], "recovery": True}
            ).status_code,
            204,
        )
        record = TwoFactorSettings.objects.get(user=self.user)
        self.assertFalse(record.enabled)
        self.assertFalse(record.secret_ciphertext)
        self.assertFalse(record.recovery_hashes)
        self.assertEqual(device.get("/api/v1/auth/me/").status_code, 403)
        self.assertEqual(self.client.get("/api/v1/auth/me/").status_code, 200)
        self.assertEqual(self.login(APIClient()).status_code, 200)

    def test_replace_codes_invalidates_old_set_and_existing_login_challenges(self) -> None:
        _, codes = self.enable_factor()
        device = APIClient()
        self.login(device)
        response = self.post(
            "2fa/recovery-codes", {"password": self.password, "otp": codes[0], "recovery": True}
        )
        self.assertEqual(response.status_code, 200)
        new_codes = response.json()["recovery_codes"]
        self.assertFalse(set(codes) & set(new_codes))
        self.assertFalse(device.get("/api/v1/auth/2fa/login/").json()["pending"])
        self.login(device)
        self.assertEqual(
            self.post("2fa/login", {"otp": codes[1], "recovery": True}, device).status_code, 400
        )
        self.assertEqual(
            self.post("2fa/login", {"otp": new_codes[0], "recovery": True}, device).status_code, 200
        )

    def test_password_reset_preserves_factor_and_recovery_codes_but_revokes_pending_login(
        self,
    ) -> None:
        _, codes = self.enable_factor()
        device = APIClient()
        self.login(device)
        reset = APIClient()
        self.post("password-reset", {"email": self.user.email}, reset)
        email_code = re.search(r"[0-9]{6}", mail.outbox[-1].body).group()
        self.post("password-reset/verify", {"code": email_code}, reset)
        new_password = "A different private phrase 582!"
        self.assertEqual(
            self.post("password-reset/complete", {"password": new_password}, reset).status_code, 204
        )
        self.assertEqual(self.client.get("/api/v1/auth/me/").status_code, 403)
        self.assertFalse(device.get("/api/v1/auth/2fa/login/").json()["pending"])
        record = TwoFactorSettings.objects.get(user=self.user)
        self.assertTrue(record.enabled)
        self.assertEqual(len(record.recovery_hashes), 10)
        self.assertEqual(
            self.post(
                "login", {"email": self.user.email, "password": new_password}, reset
            ).status_code,
            202,
        )
        self.assertEqual(
            self.post("2fa/login", {"otp": codes[0], "recovery": True}, reset).status_code, 200
        )

    def test_no_password_only_session_can_bypass_the_factor_gate(self) -> None:
        from django.contrib.sessions.backends.db import SessionStore
        from django.test import RequestFactory

        from .errors import InvalidCredentials

        self.enable_factor()
        request = RequestFactory().post("/")
        request.session = SessionStore()
        self.user.refresh_from_db()
        with self.assertRaises(InvalidCredentials):
            start_user_session(request, self.user)

    def test_private_actions_require_authentication_and_csrf(self) -> None:
        anonymous = APIClient(enforce_csrf_checks=True)
        for path in [
            "2fa/setup",
            "2fa/enable",
            "2fa/disable",
            "2fa/recovery-codes",
            "2fa/setup/cancel",
        ]:
            self.assertEqual(self.post(path, {}, anonymous).status_code, 403)
            self.assertEqual(
                self.client.post(f"/api/v1/auth/{path}/", {}, format="json").status_code, 403
            )
        self.assertEqual(anonymous.get("/api/v1/auth/2fa/").status_code, 403)
        self.assertEqual(
            anonymous.post("/api/v1/auth/2fa/login/", {}, format="json").status_code, 403
        )

    def test_wrong_encryption_key_fails_closed_without_private_details(self) -> None:
        _, codes = self.enable_factor()
        device = APIClient()
        self.login(device)
        original = settings.TWO_FACTOR_ENCRYPTION_KEY
        with override_settings(
            TWO_FACTOR_ENCRYPTION_KEY=base64.urlsafe_b64encode(b"x" * 32).decode()
        ):
            response = self.post("2fa/login", {"otp": "123456"}, device)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(original, response.content.decode())
        self.assertEqual(
            self.post("2fa/login", {"otp": codes[0], "recovery": True}, device).status_code, 200
        )
