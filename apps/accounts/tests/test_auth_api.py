from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.conf import settings
from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import AuthAttemptBucket, DeviceSession, User

PASSWORD = "Original!Example492"
NEW_PASSWORD = "Changed!Example738"


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AuthenticationApiTests(TestCase):
    def test_auth_cleanup_preserves_active_records(self) -> None:
        user = self.create_user()
        self.login()
        live = DeviceSession.objects.get(user=user)
        expired = DeviceSession.objects.create(
            user=user,
            session_key="expired-fixture",
            device_name="Old phone",
            last_active_at=timezone.now() - timedelta(days=91),
            expires_at=timezone.now() - timedelta(days=1),
        )
        old = AuthAttemptBucket.objects.create(
            key="old-fixture", window_started_at=timezone.now() - timedelta(days=2)
        )
        current = AuthAttemptBucket.objects.create(
            key="current-fixture", window_started_at=timezone.now()
        )
        call_command("prune_auth_state", stdout=StringIO())
        self.assertFalse(DeviceSession.objects.filter(pk=expired.pk).exists())
        self.assertTrue(DeviceSession.objects.filter(pk=live.pk).exists())
        self.assertFalse(AuthAttemptBucket.objects.filter(pk=old.pk).exists())
        self.assertTrue(AuthAttemptBucket.objects.filter(pk=current.pk).exists())

    def setUp(self) -> None:
        self.client = APIClient(enforce_csrf_checks=True)

    def post(self, client: APIClient, endpoint: str, data: dict):
        csrf = client.get("/api/v1/auth/csrf/").json()["csrf_token"]
        return client.post(f"/api/v1/auth/{endpoint}/", data, format="json", HTTP_X_CSRFTOKEN=csrf)

    def create_user(self, username: str = "Alice") -> User:
        return User.objects.create_user(username=username, password=PASSWORD)

    def login(self, client: APIClient | None = None, username: str = "Alice"):
        return self.post(
            client or self.client,
            "login",
            {
                "username": username,
                "password": PASSWORD,
                "device_name": "Test phone",
            },
        )

    def test_public_write_endpoints_require_csrf_even_when_anonymous(self) -> None:
        for endpoint in ["register", "login"]:
            response = self.client.post(
                f"/api/v1/auth/{endpoint}/",
                {
                    "username": "Alice",
                    "password": PASSWORD,
                },
                format="json",
            )
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json(), {"detail": "CSRF verification failed."})
        self.assertFalse(User.objects.exists())

    def test_registration_establishes_session_without_returning_secrets(self) -> None:
        response = self.post(
            self.client,
            "register",
            {
                "username": "Alice",
                "password": PASSWORD,
                "display_name": "  علي Smith  ",
            },
        )
        self.assertEqual(response.status_code, 201, response.content)
        user = User.objects.get(username="Alice")
        self.assertTrue(user.check_password(PASSWORD))
        self.assertEqual(user.email, "")
        self.assertEqual(response.json()["user"]["display_name"], "علي Smith")
        self.assertNotIn(PASSWORD, response.content.decode())
        key = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        self.assertNotIn(key, response.content.decode())
        self.assertTrue(self.client.cookies[settings.SESSION_COOKIE_NAME]["httponly"])
        self.assertEqual(self.client.get("/api/v1/auth/me/").json()["username"], "Alice")
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_registration_rejects_case_variant_and_unknown_fields(self) -> None:
        self.create_user()
        response = self.post(self.client, "register", {"username": "alice", "password": PASSWORD})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(User.objects.count(), 1)
        response = self.post(
            self.client,
            "register",
            {
                "username": "Other",
                "password": PASSWORD,
                "is_superuser": True,
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(username="Other").exists())

    def test_registration_rejects_weak_password_and_invalid_identity(self) -> None:
        for data in [
            {"username": "Alice", "password": "short"},
            {"username": "علي", "password": PASSWORD},
            {"username": "Alice", "password": PASSWORD, "display_name": "Name123"},
        ]:
            response = self.post(self.client, "register", data)
            self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.exists())

    def test_sign_in_is_case_insensitive_and_rotates_session_and_csrf(self) -> None:
        self.create_user()
        anonymous_key = self.client.session.session_key
        csrf_before = self.client.get("/api/v1/auth/csrf/").cookies[settings.CSRF_COOKIE_NAME].value
        response = self.login(username="aLiCe")
        self.assertEqual(response.status_code, 200, response.content)
        first_key = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        self.assertNotEqual(first_key, anonymous_key)
        self.assertNotEqual(csrf_before, self.client.cookies[settings.CSRF_COOKIE_NAME].value)
        self.assertEqual(self.login().status_code, 200)
        self.assertNotEqual(first_key, self.client.cookies[settings.SESSION_COOKIE_NAME].value)
        self.assertFalse(Session.objects.filter(session_key=first_key).exists())
        self.assertEqual(DeviceSession.objects.count(), 1)

    def test_failed_sign_ins_are_generic_and_do_not_issue_sessions(self) -> None:
        user = self.create_user()
        responses = []
        for username, password in [("Unknown", PASSWORD), ("Alice", "Wrong!Example983")]:
            responses.append(
                self.post(self.client, "login", {"username": username, "password": password})
            )
        user.is_active = False
        user.save(update_fields=["is_active"])
        responses.append(self.login())
        self.assertTrue(all(response.status_code == 403 for response in responses))
        self.assertEqual(
            {str(response.json()) for response in responses},
            {str({"detail": "Invalid username or password."})},
        )
        self.assertFalse(DeviceSession.objects.exists())

    def test_logout_invalidates_the_current_cookie(self) -> None:
        self.create_user()
        self.assertEqual(self.login().status_code, 200)
        old_key = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        self.assertEqual(self.post(self.client, "logout", {}).status_code, 204)
        self.assertFalse(Session.objects.filter(session_key=old_key).exists())
        self.assertFalse(DeviceSession.objects.exists())
        self.client.cookies[settings.SESSION_COOKIE_NAME] = old_key
        self.assertEqual(self.client.get("/api/v1/auth/me/").status_code, 403)

    def test_authenticated_mutations_require_csrf(self) -> None:
        self.create_user()
        self.login()
        response = self.client.post(
            "/api/v1/auth/password/",
            {
                "current_password": PASSWORD,
                "new_password": NEW_PASSWORD,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(User.objects.get(username="Alice").check_password(PASSWORD))

    def test_password_change_preserves_current_device_and_revokes_others(self) -> None:
        user = self.create_user()
        other = APIClient(enforce_csrf_checks=True)
        self.login(other)
        self.login()
        old_key = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        current_id = DeviceSession.objects.get(session_key=old_key).pk
        response = self.post(
            self.client,
            "password",
            {
                "current_password": PASSWORD,
                "new_password": NEW_PASSWORD,
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.client.get("/api/v1/auth/me/").status_code, 200)
        self.assertEqual(other.get("/api/v1/auth/me/").status_code, 403)
        self.assertEqual(
            list(DeviceSession.objects.filter(user=user).values_list("pk", flat=True)), [current_id]
        )
        self.assertNotEqual(old_key, self.client.cookies[settings.SESSION_COOKIE_NAME].value)
        replay = APIClient()
        replay.cookies[settings.SESSION_COOKIE_NAME] = old_key
        self.assertEqual(replay.get("/api/v1/auth/me/").status_code, 403)
        user.refresh_from_db()
        self.assertTrue(user.check_password(NEW_PASSWORD))

    def test_invalid_password_change_preserves_password_and_expiry(self) -> None:
        user = self.create_user()
        self.login()
        device = DeviceSession.objects.get(user=user)
        expiry = device.expires_at
        for current, new in [("Incorrect!Example33", NEW_PASSWORD), (PASSWORD, "short")]:
            response = self.post(
                self.client,
                "password",
                {
                    "current_password": current,
                    "new_password": new,
                },
            )
            self.assertEqual(response.status_code, 400)
        user.refresh_from_db()
        device.refresh_from_db()
        self.assertTrue(user.check_password(PASSWORD))
        self.assertEqual(device.expires_at, expiry)

    def test_successful_reads_renew_but_rejections_do_not(self) -> None:
        user = self.create_user()
        self.login()
        now = timezone.now()
        DeviceSession.objects.filter(user=user).update(expires_at=now + timedelta(days=1))
        self.assertEqual(self.client.get("/api/v1/auth/me/").status_code, 200)
        device = DeviceSession.objects.get(user=user)
        self.assertGreater(device.expires_at, now + timedelta(days=89))
        expiry = device.expires_at
        session_expiry = Session.objects.get(session_key=device.session_key).expire_date
        self.assertEqual(self.post(self.client, "me", {}).status_code, 405)
        device.refresh_from_db()
        self.assertEqual(device.expires_at, expiry)
        self.assertEqual(
            Session.objects.get(session_key=device.session_key).expire_date, session_expiry
        )

    def test_inactivity_boundary_is_rejected_without_renewal(self) -> None:
        user = self.create_user()
        self.login()
        now = timezone.now()
        DeviceSession.objects.filter(user=user).update(expires_at=now)
        with patch("apps.accounts.authentication.timezone.now", return_value=now):
            self.assertEqual(self.client.get("/api/v1/auth/me/").status_code, 403)
        self.assertEqual(DeviceSession.objects.get(user=user).expires_at, now)

    def test_expired_django_session_cannot_be_revived(self) -> None:
        user = self.create_user()
        self.login()
        device = DeviceSession.objects.get(user=user)
        Session.objects.filter(session_key=device.session_key).update(
            expire_date=timezone.now() - timedelta(seconds=1)
        )
        self.assertEqual(self.client.get("/api/v1/auth/me/").status_code, 403)
        device.refresh_from_db()
        self.assertLess(device.last_active_at, timezone.now())

    def test_device_list_is_paginated_owned_and_hides_session_keys(self) -> None:
        user = self.create_user()
        self.create_user("Bob")
        other = APIClient(enforce_csrf_checks=True)
        self.login(other, "Bob")
        self.login()
        expiry = timezone.now() + timedelta(days=90)
        for number in range(51):
            key = f"{number:032x}"
            Session.objects.create(session_key=key, session_data="fixture", expire_date=expiry)
            DeviceSession.objects.create(
                user=user,
                session_key=key,
                device_name="Extra phone",
                last_active_at=timezone.now(),
                expires_at=expiry,
            )
        response = self.client.get("/api/v1/auth/devices/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["results"]), 50)
        self.assertIsNotNone(response.json()["next"])
        other_ids = set(
            DeviceSession.objects.filter(user__username="Bob").values_list("id", flat=True)
        )
        self.assertFalse(other_ids & {row["id"] for row in response.json()["results"]})
        self.assertNotIn("session_key", response.content.decode())
        self.assertEqual(self.client.get(response.json()["next"]).status_code, 200)

    @override_settings(AUTH_LOGIN_ACCOUNT_LIMIT=2)
    def test_login_account_limits_share_case_variants_and_expire(self) -> None:
        self.assertEqual(self.login(username="Nobody").status_code, 403)
        self.assertEqual(self.login(username="NOBODY").status_code, 403)
        self.assertEqual(self.login(username="nobody").status_code, 429)
        AuthAttemptBucket.objects.update(window_started_at=timezone.now() - timedelta(minutes=16))
        self.assertEqual(self.login(username="Nobody").status_code, 403)
        self.assertFalse(
            any(
                "nobody" in key.lower()
                for key in AuthAttemptBucket.objects.values_list("key", flat=True)
            )
        )

    @override_settings(AUTH_LOGIN_IP_LIMIT=2)
    def test_login_ip_limits_cover_different_usernames(self) -> None:
        self.assertEqual(self.login(username="First").status_code, 403)
        self.assertEqual(self.login(username="Second").status_code, 403)
        self.assertEqual(self.login(username="Third").status_code, 429)

    @override_settings(AUTH_REGISTRATION_IP_LIMIT=1)
    def test_registration_ip_limit(self) -> None:
        self.assertEqual(
            self.post(
                self.client,
                "register",
                {
                    "username": "First",
                    "password": PASSWORD,
                },
            ).status_code,
            201,
        )
        self.assertEqual(
            self.post(
                self.client,
                "register",
                {
                    "username": "Second",
                    "password": PASSWORD,
                },
            ).status_code,
            429,
        )
