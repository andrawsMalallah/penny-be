from base64 import b32decode
from datetime import timedelta
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib.auth.hashers import check_password
from django.contrib.sessions.models import Session
from django.test import TestCase, override_settings
from django.utils import timezone
from django_otp.oath import TOTP
from rest_framework.test import APIClient

from apps.accounts.models import (
    AuthenticatorDevice,
    DeviceSession,
    MfaChallenge,
    RecoveryCode,
    User,
)

PASSWORD = "Original!Example492"


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    AUTH_LOGIN_ACCOUNT_LIMIT=100,
    AUTH_LOGIN_IP_LIMIT=200,
)
class MfaApiTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="Alice", password=PASSWORD)
        self.client = APIClient(enforce_csrf_checks=True)
        self.login(self.client)

    def post(self, client: APIClient, endpoint: str, data: dict):
        csrf = client.get("/api/v1/auth/csrf/").json()["csrf_token"]
        return client.post(f"/api/v1/auth/{endpoint}/", data, format="json", HTTP_X_CSRFTOKEN=csrf)

    def login(self, client: APIClient):
        return self.post(client, "login", {"username": "Alice", "password": PASSWORD})

    def setup_mfa(self) -> dict:
        response = self.post(self.client, "mfa/setup", {"password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def token(self, manual_key: str, offset: int = 0) -> str:
        verifier = TOTP(b32decode(manual_key))
        verifier.time = timezone.now().timestamp() + offset
        return str(verifier.token()).zfill(6)

    def enable(self) -> tuple[dict, list[str]]:
        setup = self.setup_mfa()
        response = self.post(self.client, "mfa/confirm", {"code": self.token(setup["manual_key"])})
        self.assertEqual(response.status_code, 200, response.content)
        return setup, response.json()["recovery_codes"]

    def test_enrollment_encrypts_secret_and_activates_only_after_proof(self) -> None:
        other = APIClient(enforce_csrf_checks=True)
        self.login(other)
        setup = self.setup_mfa()
        authenticator = AuthenticatorDevice.objects.get(user=self.user)
        self.assertFalse(authenticator.confirmed)
        decrypted = Fernet(settings.PENNY_MFA_ENCRYPTION_KEY).decrypt(
            authenticator.encrypted_secret.encode()
        )
        self.assertEqual(bytes.fromhex(decrypted.decode()), b32decode(setup["manual_key"]))
        self.assertNotIn(decrypted.decode(), authenticator.encrypted_secret)
        self.assertIn("issuer=Penny", setup["enrollment_uri"])
        self.assertEqual(self.client.get("/api/v1/auth/mfa/").json()["enabled"], False)
        response = self.post(self.client, "mfa/confirm", {"code": self.token(setup["manual_key"])})
        self.assertEqual(response.status_code, 200)
        codes = response.json()["recovery_codes"]
        self.assertEqual(len(set(codes)), 10)
        hashes = list(
            RecoveryCode.objects.filter(user=self.user).values_list("code_hash", flat=True)
        )
        self.assertTrue(
            all(any(check_password(code, hashed) for hashed in hashes) for code in codes)
        )
        self.assertTrue(all(code not in hashes for code in codes))
        self.assertEqual(other.get("/api/v1/auth/me/").status_code, 403)
        self.assertEqual(self.client.get("/api/v1/auth/me/").status_code, 200)
        self.assertEqual(DeviceSession.objects.filter(user=self.user).count(), 1)
        self.assertEqual(
            self.post(self.client, "mfa/setup", {"password": PASSWORD}).status_code, 400
        )
        self.assertEqual(self.post(self.client, "mfa/confirm", {"code": codes[0]}).status_code, 403)

    def test_setup_requires_password_and_confirmation_is_bound_to_device_and_deadline(self) -> None:
        self.assertEqual(
            self.post(self.client, "mfa/setup", {"password": "wrong"}).status_code, 403
        )
        self.assertFalse(AuthenticatorDevice.objects.exists())
        setup = self.setup_mfa()
        other = APIClient(enforce_csrf_checks=True)
        self.login(other)
        code = self.token(setup["manual_key"])
        self.assertEqual(self.post(other, "mfa/confirm", {"code": code}).status_code, 403)
        AuthenticatorDevice.objects.update(enrollment_expires_at=timezone.now())
        self.assertEqual(self.post(self.client, "mfa/confirm", {"code": code}).status_code, 403)
        self.assertFalse(RecoveryCode.objects.exists())

    def test_pending_mfa_is_unauthenticated_and_recovery_is_single_use(self) -> None:
        _, codes = self.enable()
        pending = APIClient(enforce_csrf_checks=True)
        response = self.login(pending)
        self.assertTrue(response.json()["mfa_required"])
        self.assertNotIn("user", response.json())
        key = pending.cookies[settings.SESSION_COOKIE_NAME].value
        self.assertNotIn("_auth_user_id", Session.objects.get(pk=key).get_decoded())
        for endpoint in ["me", "devices", "mfa"]:
            self.assertEqual(pending.get(f"/api/v1/auth/{endpoint}/").status_code, 403)
        self.assertFalse(DeviceSession.objects.filter(session_key=key).exists())
        response = self.post(pending, "mfa/login", {"code": codes[0]})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertNotEqual(pending.cookies[settings.SESSION_COOKIE_NAME].value, key)
        self.assertEqual(pending.get("/api/v1/auth/me/").status_code, 200)
        self.assertEqual(RecoveryCode.objects.count(), 9)
        self.assertEqual(self.post(pending, "mfa/login", {"code": codes[1]}).status_code, 403)
        replay = APIClient(enforce_csrf_checks=True)
        self.login(replay)
        self.assertEqual(self.post(replay, "mfa/login", {"code": codes[0]}).status_code, 403)

    def test_totp_replay_is_rejected_but_next_step_signs_in(self) -> None:
        setup, _ = self.enable()
        pending = APIClient(enforce_csrf_checks=True)
        self.login(pending)
        authenticator = AuthenticatorDevice.objects.get()
        verifier = TOTP(b32decode(setup["manual_key"]))
        verifier.time = authenticator.last_step * 30
        replay = str(verifier.token()).zfill(6)
        self.assertEqual(self.post(pending, "mfa/login", {"code": replay}).status_code, 403)
        with patch(
            "django.utils.timezone.now", return_value=timezone.now() + timedelta(seconds=30)
        ):
            self.assertEqual(
                self.post(
                    pending, "mfa/login", {"code": self.token(setup["manual_key"])}
                ).status_code,
                200,
            )

    def test_expired_challenges_and_inactive_users_are_denied(self) -> None:
        _, codes = self.enable()
        pending = APIClient(enforce_csrf_checks=True)
        self.login(pending)
        MfaChallenge.objects.update(expires_at=timezone.now())
        self.assertEqual(self.post(pending, "mfa/login", {"code": codes[0]}).status_code, 403)
        self.login(pending)
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertEqual(self.post(pending, "mfa/login", {"code": codes[0]}).status_code, 403)
        self.assertEqual(RecoveryCode.objects.count(), 10)

    def test_regeneration_invalidates_old_codes_and_pending_signins(self) -> None:
        _, codes = self.enable()
        pending = APIClient(enforce_csrf_checks=True)
        self.login(pending)
        response = self.post(
            self.client, "mfa/recovery-codes", {"password": PASSWORD, "code": codes[0]}
        )
        self.assertEqual(response.status_code, 200, response.content)
        fresh = response.json()["recovery_codes"]
        self.assertTrue(set(codes).isdisjoint(fresh))
        self.assertFalse(MfaChallenge.objects.exists())
        self.assertEqual(self.post(pending, "mfa/login", {"code": fresh[0]}).status_code, 403)
        self.login(pending)
        self.assertEqual(self.post(pending, "mfa/login", {"code": codes[1]}).status_code, 403)
        self.assertEqual(self.post(pending, "mfa/login", {"code": fresh[0]}).status_code, 200)

    def test_disable_requires_password_and_proof_and_removes_secrets(self) -> None:
        _, codes = self.enable()
        for data in [
            {"password": "wrong", "code": codes[0]},
            {"password": PASSWORD, "code": "bad"},
        ]:
            self.assertEqual(self.post(self.client, "mfa/disable", data).status_code, 403)
        self.assertEqual(RecoveryCode.objects.count(), 10)
        response = self.post(self.client, "mfa/disable", {"password": PASSWORD, "code": codes[0]})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(AuthenticatorDevice.objects.exists())
        self.assertFalse(RecoveryCode.objects.exists())
        self.assertEqual(self.login(APIClient(enforce_csrf_checks=True)).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/auth/me/").status_code, 200)

    def test_failed_transition_rolls_back_recovery_consumption_and_changes(self) -> None:
        _, codes = self.enable()
        hashes = list(RecoveryCode.objects.values_list("code_hash", flat=True))
        with patch(
            "apps.accounts.services.mfa.rotate_current",
            side_effect=RuntimeError("Injected failure"),
        ):
            with self.assertRaises(RuntimeError):
                self.post(self.client, "mfa/disable", {"password": PASSWORD, "code": codes[0]})
        self.assertTrue(AuthenticatorDevice.objects.get().confirmed)
        self.assertCountEqual(RecoveryCode.objects.values_list("code_hash", flat=True), hashes)

    def test_password_change_invalidates_pending_mfa(self) -> None:
        _, codes = self.enable()
        pending = APIClient(enforce_csrf_checks=True)
        self.login(pending)
        response = self.post(
            self.client,
            "password",
            {"current_password": PASSWORD, "new_password": "Changed!Example738"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(MfaChallenge.objects.exists())
        self.assertEqual(self.post(pending, "mfa/login", {"code": codes[0]}).status_code, 403)

    def test_mfa_endpoints_require_csrf_strict_inputs_and_authentication(self) -> None:
        anonymous = APIClient(enforce_csrf_checks=True)
        for endpoint in [
            "mfa/setup",
            "mfa/confirm",
            "mfa/login",
            "mfa/disable",
            "mfa/recovery-codes",
        ]:
            self.assertEqual(
                anonymous.post(f"/api/v1/auth/{endpoint}/", {}, format="json").status_code, 403
            )
        response = self.post(self.client, "mfa/setup", {"password": PASSWORD, "extra": "value"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_wrong_key_fails_closed(self) -> None:
        self.setup_mfa()
        with override_settings(PENNY_MFA_ENCRYPTION_KEY=Fernet.generate_key()):
            from cryptography.fernet import InvalidToken

            with self.assertRaises(InvalidToken):
                self.post(self.client, "mfa/confirm", {"code": "123456"})
        self.assertFalse(AuthenticatorDevice.objects.get().confirmed)

    def test_other_users_cannot_consume_recovery_codes_or_view_secrets(self) -> None:
        setup, codes = self.enable()
        User.objects.create_user(username="Bob", password=PASSWORD)
        other = APIClient(enforce_csrf_checks=True)
        self.post(other, "login", {"username": "Bob", "password": PASSWORD})
        response = self.post(other, "mfa/disable", {"password": PASSWORD, "code": codes[0]})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(RecoveryCode.objects.filter(user=self.user).count(), 10)
        status = other.get("/api/v1/auth/mfa/")
        self.assertEqual(status.json(), {"enabled": False, "recovery_codes_remaining": 0})
        pending = APIClient(enforce_csrf_checks=True)
        self.login(pending)
        with self.assertLogs("django.request", level="WARNING") as recorded:
            failed = self.post(pending, "mfa/login", {"code": "sensitive-invalid-code"})
        public_output = str(recorded.output) + failed.content.decode() + status.content.decode()
        for secret in [PASSWORD, setup["manual_key"], codes[0], "sensitive-invalid-code"]:
            self.assertNotIn(secret, public_output)

    def test_expired_mfa_challenges_are_pruned_without_removing_live_ones(self) -> None:
        from io import StringIO

        from django.core.management import call_command

        self.enable()
        first, second = APIClient(enforce_csrf_checks=True), APIClient(enforce_csrf_checks=True)
        self.login(first)
        self.login(second)
        expired = MfaChallenge.objects.get(
            session_key=first.cookies[settings.SESSION_COOKIE_NAME].value
        )
        MfaChallenge.objects.filter(pk=expired.pk).update(expires_at=timezone.now())
        call_command("prune_auth_state", stdout=StringIO())
        self.assertFalse(MfaChallenge.objects.filter(pk=expired.pk).exists())
        self.assertEqual(MfaChallenge.objects.count(), 1)

    def test_mfa_attempt_limit_persists_on_failed_proofs(self) -> None:
        self.enable()
        pending = APIClient(enforce_csrf_checks=True)
        self.login(pending)
        from apps.accounts.models import AuthAttemptBucket

        AuthAttemptBucket.objects.all().delete()
        with override_settings(AUTH_LOGIN_ACCOUNT_LIMIT=2):
            self.assertEqual(self.post(pending, "mfa/login", {"code": "invalid"}).status_code, 403)
            self.assertEqual(self.post(pending, "mfa/login", {"code": "invalid"}).status_code, 403)
            self.assertEqual(self.post(pending, "mfa/login", {"code": "invalid"}).status_code, 429)
