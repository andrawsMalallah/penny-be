from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from queue import Queue
from threading import Event
from time import monotonic
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from django_otp.oath import TOTP
from rest_framework.exceptions import AuthenticationFailed

from apps.accounts.models import (
    AuthenticatorDevice,
    DeviceSession,
    MfaChallenge,
    RecoveryCode,
    User,
)
from apps.accounts.services.authentication import sign_in
from apps.accounts.services.mfa import complete_sign_in, confirm_enrollment, manage_mfa
from apps.accounts.tests.test_auth_concurrency import auth_request

PASSWORD = "Original!Example492"


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    AUTH_LOGIN_ACCOUNT_LIMIT=100,
    AUTH_LOGIN_IP_LIMIT=200,
)
class MfaConcurrencyTests(TransactionTestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="Alice", password=PASSWORD)
        self.current = auth_request()
        sign_in(self.current, "Alice", PASSWORD, "Setup phone")
        self.secret = "12" * 20
        self.authenticator = AuthenticatorDevice.objects.create(
            user=self.user,
            name="Authenticator",
            confirmed=True,
            encrypted_secret=Fernet(settings.PENNY_MFA_ENCRYPTION_KEY)
            .encrypt(self.secret.encode())
            .decode(),
            enrollment_expires_at=timezone.now() + timedelta(minutes=5),
        )
        self.code = "ab" * 16
        self.other_code = "cd" * 16
        RecoveryCode.objects.bulk_create(
            [
                RecoveryCode(user=self.user, code_hash=make_password(value))
                for value in [self.code, self.other_code]
            ]
        )

    def pending_request(self):
        request = auth_request()
        self.assertIsNone(sign_in(request, "Alice", PASSWORD, "New phone"))
        return request

    def contend(
        self, operations: list[Callable], before_release: Callable = lambda: None
    ) -> list[str]:
        worker_pids = Queue()

        def attempt(operation: Callable) -> str:
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    worker_pids.put(cursor.fetchone()[0])
                operation()
                return "allowed"
            except AuthenticationFailed:
                return "denied"
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=len(operations)) as pool:
            with transaction.atomic():
                User.objects.select_for_update().get(pk=self.user.pk)
                futures = [pool.submit(attempt, operation) for operation in operations]
                pids = [worker_pids.get(timeout=5) for _ in operations]
                deadline = monotonic() + 10
                blocked = False
                while monotonic() < deadline:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT bool_and(cardinality(pg_blocking_pids(pid)) > 0) "
                            "FROM unnest(%s::int[]) AS pid",
                            [pids],
                        )
                        blocked = cursor.fetchone()[0]
                    if blocked:
                        break
                    Event().wait(0.01)
                self.assertTrue(blocked, "Every contender must be observed waiting in PostgreSQL.")
                before_release()
            return sorted(future.result(timeout=10) for future in futures)

    def test_recovery_code_has_one_winner_across_two_pending_signins(self) -> None:
        first, second = self.pending_request(), self.pending_request()
        results = self.contend(
            [
                lambda: complete_sign_in(first, self.code),
                lambda: complete_sign_in(second, self.code),
            ]
        )
        self.assertEqual(results, ["allowed", "denied"])
        self.assertEqual(RecoveryCode.objects.count(), 1)
        self.assertEqual(DeviceSession.objects.count(), 2)
        self.assertEqual(MfaChallenge.objects.count(), 1)

    def test_totp_has_one_winner_across_two_pending_signins(self) -> None:
        first, second = self.pending_request(), self.pending_request()
        # Freeze one step for the full contention window, avoiding wall-clock rollover flakiness.
        now = timezone.now()
        verifier = TOTP(bytes.fromhex(self.secret))
        verifier.time = now.timestamp()
        token = str(verifier.token()).zfill(6)
        with patch("django.utils.timezone.now", return_value=now):
            results = self.contend(
                [
                    lambda: complete_sign_in(first, token),
                    lambda: complete_sign_in(second, token),
                ]
            )
        self.assertEqual(results, ["allowed", "denied"])
        self.assertEqual(DeviceSession.objects.count(), 2)

    def test_regeneration_invalidates_waiting_signin_atomically(self) -> None:
        pending = self.pending_request()
        results = self.contend(
            [lambda: complete_sign_in(pending, self.code)],
            lambda: manage_mfa(self.current, self.user, PASSWORD, self.other_code, disable=False),
        )
        self.assertEqual(results, ["denied"])
        self.assertEqual(RecoveryCode.objects.count(), 10)
        self.assertEqual(DeviceSession.objects.count(), 1)
        self.assertFalse(MfaChallenge.objects.exists())

    def test_activation_forces_waiting_password_signin_through_mfa(self) -> None:
        self.authenticator.confirmed = False
        self.authenticator.enrollment_session = DeviceSession.objects.get()
        self.authenticator.save()
        request = auth_request()
        verifier = TOTP(bytes.fromhex(self.secret))
        now = timezone.now()
        verifier.time = now.timestamp()
        with patch("django.utils.timezone.now", return_value=now):
            results = self.contend(
                [lambda: sign_in(request, "Alice", PASSWORD, "Waiting phone")],
                lambda: confirm_enrollment(self.current, self.user, str(verifier.token()).zfill(6)),
            )
        self.assertEqual(results, ["allowed"])
        self.assertEqual(DeviceSession.objects.count(), 1)
        self.assertTrue(
            MfaChallenge.objects.filter(session_key=request.session.session_key).exists()
        )
        self.assertNotIn("_auth_user_id", request.session)

    def test_failed_session_creation_rolls_back_code_and_challenge(self) -> None:
        pending = self.pending_request()
        with patch(
            "apps.accounts.services.mfa.establish_session",
            side_effect=RuntimeError("Injected failure"),
        ):
            with self.assertRaises(RuntimeError):
                complete_sign_in(pending, self.code)
        self.assertEqual(RecoveryCode.objects.count(), 2)
        self.assertEqual(MfaChallenge.objects.count(), 1)
        self.assertEqual(DeviceSession.objects.count(), 1)
        complete_sign_in(pending, self.code)
        self.assertEqual(RecoveryCode.objects.count(), 1)
