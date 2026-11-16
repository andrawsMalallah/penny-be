from datetime import timedelta

from django.conf import settings
from django.contrib.auth import HASH_SESSION_KEY
from django.contrib.sessions.models import Session
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from .models import SignedInSession, User
from .sessions import GENERATION_KEY, revoke_user_sessions, start_user_session


class SessionTests(TestCase):
    def setUp(self) -> None:
        self.password = "Private session test phrase 731!"
        self.user = User.objects.create_user(
            "sessions@example.com",
            self.password,
            display_name="Session test",
            language="en",
            email_verified=True,
        )
        self.client = self.sign_in()

    def post(self, client: APIClient, path: str, body: dict):
        token = client.get("/api/v1/auth/csrf/").json()["csrf_token"]
        return client.post(
            f"/api/v1/auth/{path}/",
            body,
            format="json",
            HTTP_X_CSRFTOKEN=token,
            HTTP_USER_AGENT="Mozilla/5.0 (Windows NT 10.0) Chrome/130.0 Safari/537.36",
        )

    def sign_in(self, user: User | None = None) -> APIClient:
        client = APIClient(enforce_csrf_checks=True)
        response = self.post(
            client, "login", {"email": (user or self.user).email, "password": self.password}
        )
        self.assertEqual(response.status_code, 200)
        return client

    def test_list_is_private_current_paginated_and_never_exposes_session_keys(self) -> None:
        second = self.sign_in()
        other = User.objects.create_user(
            "other@example.com",
            self.password,
            language="en",
            email_verified=True,
        )
        self.sign_in(other)
        response = self.client.get("/api/v1/auth/sessions/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(response.json()["count"], 2)
        rows = response.json()["results"]
        self.assertEqual(sum(row["is_current"] for row in rows), 1)
        for row in rows:
            self.assertEqual(
                set(row), {"browser", "device", "signed_in_at", "last_active_at", "is_current"}
            )
            self.assertEqual((row["browser"], row["device"]), ("Chrome", "Windows"))
            self.assertIsNotNone(row["signed_in_at"])
        for client in [self.client, second]:
            self.assertNotIn(
                client.cookies[settings.SESSION_COOKIE_NAME].value, response.content.decode()
            )

    def test_activity_updates_but_does_not_extend_expiry_or_change_sign_in(self) -> None:
        record = SignedInSession.objects.get(user=self.user)
        signed_in_at = record.signed_in_at
        expiry = record.session.expire_date
        earlier = timezone.now() - timedelta(minutes=10)
        SignedInSession.objects.filter(pk=record.pk).update(last_active_at=earlier)
        self.client.get("/api/v1/auth/me/")
        record.refresh_from_db()
        self.assertGreater(record.last_active_at, earlier)
        self.assertEqual(record.signed_in_at, signed_in_at)
        self.assertEqual(record.session.expire_date, expiry)

    def test_logout_all_revokes_current_other_and_untracked_legacy_sessions(self) -> None:
        second = self.sign_in()
        legacy = APIClient()
        legacy.force_login(self.user)
        other = User.objects.create_user(
            "unaffected@example.com",
            self.password,
            language="en",
            email_verified=True,
        )
        other_client = self.sign_in(other)
        response = self.post(self.client, "logout-all", {})
        self.assertEqual(response.status_code, 204)
        self.assertEqual(SignedInSession.objects.filter(user=self.user).count(), 0)
        for client in [self.client, second, legacy]:
            self.assertEqual(client.get("/api/v1/auth/me/").status_code, 403)
        self.assertEqual(other_client.get("/api/v1/auth/me/").status_code, 200)
        self.assertEqual(self.sign_in().get("/api/v1/auth/me/").status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.password))

    def test_revoked_session_data_saved_by_an_inflight_request_cannot_restore_access(self) -> None:
        stale_data = dict(self.client.session)
        revoke_user_sessions(self.user)
        stale = self.client.session
        stale.update(stale_data)
        stale.save()
        self.client.cookies[settings.SESSION_COOKIE_NAME] = stale.session_key
        self.assertEqual(self.client.get("/api/v1/auth/me/").status_code, 403)

    def test_anonymous_and_unverified_users_cannot_list_or_revoke_and_csrf_is_required(
        self,
    ) -> None:
        anonymous = APIClient(enforce_csrf_checks=True)
        self.assertEqual(anonymous.get("/api/v1/auth/sessions/").status_code, 403)
        self.assertEqual(self.post(anonymous, "logout-all", {}).status_code, 403)
        self.assertEqual(
            self.client.post("/api/v1/auth/logout-all/", {}, format="json").status_code, 403
        )
        self.assertEqual(self.client.get("/api/v1/auth/logout-all/").status_code, 405)
        self.assertEqual(self.client.get("/api/v1/auth/me/").status_code, 200)
        User.objects.filter(pk=self.user.pk).update(email_verified=False)
        self.assertEqual(self.client.get("/api/v1/auth/sessions/").status_code, 403)
        self.assertEqual(self.post(self.client, "logout-all", {}).status_code, 403)

    def test_expired_sessions_are_excluded_and_normal_logout_removes_metadata(self) -> None:
        second = self.sign_in()
        Session.objects.filter(session_key=second.session.session_key).update(
            expire_date=timezone.now() - timedelta(seconds=1)
        )
        self.assertEqual(self.client.get("/api/v1/auth/sessions/").json()["count"], 1)
        key = self.client.session.session_key
        self.assertEqual(self.post(self.client, "logout", {}).status_code, 204)
        self.assertFalse(SignedInSession.objects.filter(pk=key).exists())

    def test_legacy_session_is_enrolled_with_unknown_sign_in_time(self) -> None:
        legacy = APIClient()
        legacy.force_login(self.user)
        response = legacy.get("/api/v1/auth/sessions/", HTTP_USER_AGENT="<script>private</script>")
        row = next(row for row in response.json()["results"] if row["is_current"])
        self.assertIsNone(row["signed_in_at"])
        self.assertEqual(row["browser"], "Unknown browser")
        self.assertNotIn("<script>", response.content.decode())

    def test_pagination_is_bounded_and_owned(self) -> None:
        now = timezone.now()
        for index in range(32):
            session = Session.objects.create(
                session_key=f"fixture-{index}",
                session_data="",
                expire_date=now + timedelta(hours=1),
            )
            SignedInSession.objects.create(
                session=session,
                user=self.user,
                generation=0,
                browser="Firefox",
                device="Linux",
                last_active_at=now,
            )
        first = self.client.get("/api/v1/auth/sessions/").json()
        second = self.client.get("/api/v1/auth/sessions/?page=2").json()
        self.assertEqual(
            (first["count"], len(first["results"]), len(second["results"])), (33, 30, 3)
        )
        self.assertIsNotNone(first["next"])
        self.assertIsNotNone(second["previous"])

    def test_login_rechecks_a_password_reset_that_completed_during_authentication(self) -> None:
        from django.contrib.sessions.backends.db import SessionStore
        from django.test import RequestFactory

        request = RequestFactory().post("/")
        request.session = SessionStore()
        User.objects.filter(pk=self.user.pk).update(password="changed-password-hash")
        from .errors import InvalidCredentials

        with self.assertRaises(InvalidCredentials):
            start_user_session(request, self.user)

    def test_revocation_generation_is_not_returned_in_profile(self) -> None:
        self.assertNotIn("session_generation", self.client.get("/api/v1/auth/me/").json())
        self.assertIn(GENERATION_KEY, self.client.session)
        self.assertIn(HASH_SESSION_KEY, self.client.session)
