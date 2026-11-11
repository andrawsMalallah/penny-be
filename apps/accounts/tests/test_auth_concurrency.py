from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Barrier, Event
from time import monotonic
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.middleware import SessionMiddleware
from django.db import close_old_connections, connection, transaction
from django.test import RequestFactory, TransactionTestCase, override_settings
from django.utils import timezone
from django.utils.crypto import salted_hmac
from rest_framework.exceptions import AuthenticationFailed, ValidationError

from apps.accounts.models import AuthAttemptBucket, DeviceSession, User
from apps.accounts.services.authentication import (
    AuthRateLimited,
    consume_attempts,
    register,
    sign_in,
)

PASSWORD = "Original!Example492"


def auth_request():
    request = RequestFactory().post("/api/v1/auth/login/")
    SessionMiddleware(lambda request: None).process_request(request)
    request.user = AnonymousUser()
    return request


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AuthenticationConcurrencyTests(TransactionTestCase):
    @override_settings(AUTH_LOGIN_ACCOUNT_LIMIT=1)
    def test_concurrent_attempts_cannot_exceed_account_limit(self) -> None:
        key = salted_hmac(
            "penny.auth.attempt", "login:account:alice", algorithm="sha256"
        ).hexdigest()
        bucket = AuthAttemptBucket.objects.create(key=key, window_started_at=timezone.now())
        worker_pids = Queue()

        def attempt() -> str:
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    worker_pids.put(cursor.fetchone()[0])
                consume_attempts(auth_request(), "login", "Alice")
                return "allowed"
            except AuthRateLimited:
                return "blocked"
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            with transaction.atomic():
                AuthAttemptBucket.objects.select_for_update().get(pk=bucket.pk)
                futures = [pool.submit(attempt) for _ in range(2)]
                pids = [worker_pids.get(timeout=5) for _ in range(2)]
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
                self.assertTrue(blocked, "Both attempts must contend for the same counter lock.")
            self.assertEqual(
                sorted(future.result(timeout=10) for future in futures), ["allowed", "blocked"]
            )
        bucket.refresh_from_db()
        self.assertEqual(bucket.attempts, 1)

    def test_concurrent_case_variant_registration_has_one_winner(self) -> None:
        barrier = Barrier(2)
        original_save = User.save

        def coordinated_save(user, *args, **kwargs):
            if user._state.adding:
                barrier.wait(timeout=10)
            return original_save(user, *args, **kwargs)

        def attempt(username: str) -> str:
            close_old_connections()
            try:
                register(auth_request(), username, PASSWORD, "", "Phone")
                return "created"
            except ValidationError:
                return "unavailable"
            finally:
                connection.close()

        with (
            patch.object(User, "save", coordinated_save),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            futures = [pool.submit(attempt, username) for username in ["Alice", "alice"]]
            self.assertEqual(
                sorted(future.result(timeout=15) for future in futures), ["created", "unavailable"]
            )
        self.assertEqual(User.objects.filter(username__iexact="alice").count(), 1)
        self.assertEqual(DeviceSession.objects.count(), 1)

    def test_login_waits_for_password_change_and_rejects_old_password(self) -> None:
        user = User.objects.create_user(username="Alice", password=PASSWORD)
        worker_pid = Queue()

        def attempt() -> str:
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    worker_pid.put(cursor.fetchone()[0])
                sign_in(auth_request(), "alice", PASSWORD, "Phone")
                return "signed-in"
            except AuthenticationFailed:
                return "rejected"
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                locked = User.objects.select_for_update().get(pk=user.pk)
                future = pool.submit(attempt)
                pid = worker_pid.get(timeout=5)
                deadline = monotonic() + 10
                blocked = False
                while monotonic() < deadline:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT pg_blocking_pids(%s)", [pid])
                        blocked = bool(cursor.fetchone()[0])
                    if blocked:
                        break
                    Event().wait(0.01)
                self.assertTrue(blocked, "Login must wait on the password-change row lock.")
                locked.set_password("Changed!Example738")
                locked.save(update_fields=["password"])
            self.assertEqual(future.result(timeout=10), "rejected")
        self.assertFalse(DeviceSession.objects.exists())
