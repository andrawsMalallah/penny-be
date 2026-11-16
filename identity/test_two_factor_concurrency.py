"""Run against an isolated PostgreSQL test DB to verify one-time proofs under contention."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless

import pyotp
from django.contrib.auth.hashers import make_password
from django.db import close_old_connections, connection
from django.test import TransactionTestCase
from rest_framework.test import APIClient

from .models import TwoFactorSettings, User
from .two_factor import encryption


@skipUnless(connection.vendor == "postgresql", "Requires isolated PostgreSQL test database")
class FactorConcurrencyTests(TransactionTestCase):
    def setUp(self) -> None:
        self.password = "Private concurrent phrase 593!"
        self.user = User.objects.create_user(
            "concurrent@example.com", self.password, language="en", email_verified=True
        )
        self.secret = pyotp.random_base32()
        self.recovery_code = "0123456789abcdef"
        TwoFactorSettings.objects.create(
            user=self.user,
            enabled=True,
            secret_ciphertext=encryption().encrypt(self.secret.encode()).decode(),
            recovery_hashes=[make_password(self.recovery_code)],
        )

    def attempt_simultaneously(self, code: str, recovery: bool) -> list[int]:
        devices = []
        for _ in range(2):
            client = APIClient(enforce_csrf_checks=True)
            token = client.get("/api/v1/auth/csrf/").json()["csrf_token"]
            response = client.post(
                "/api/v1/auth/login/",
                {"email": self.user.email, "password": self.password},
                format="json",
                HTTP_X_CSRFTOKEN=token,
            )
            self.assertEqual(response.status_code, 202)
            devices.append(client)
        barrier = Barrier(2, timeout=15)

        def attempt(client: APIClient) -> int:
            close_old_connections()
            try:
                token = client.get("/api/v1/auth/csrf/").json()["csrf_token"]
                barrier.wait()
                return client.post(
                    "/api/v1/auth/2fa/login/",
                    {"otp": code, "recovery": recovery},
                    format="json",
                    HTTP_X_CSRFTOKEN=token,
                ).status_code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            return list(executor.map(attempt, devices))

    def test_same_recovery_code_authenticates_exactly_one_concurrent_browser(self) -> None:
        self.assertEqual(sorted(self.attempt_simultaneously(self.recovery_code, True)), [200, 400])
        self.assertEqual(TwoFactorSettings.objects.get(user=self.user).recovery_hashes, [])

    def test_same_authenticator_code_authenticates_exactly_one_concurrent_browser(self) -> None:
        self.assertEqual(
            sorted(self.attempt_simultaneously(pyotp.TOTP(self.secret).now(), False)), [200, 400]
        )
