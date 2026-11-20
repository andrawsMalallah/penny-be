from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from app.identity.models import TwoFactorLogin, TwoFactorSettings, User
from app.identity.services.sessions import revoke_user_sessions
from app.identity.services.two_factor import LOGIN_KEY


class SessionStateTests(TestCase):
    def setUp(self) -> None:
        self.client = APIClient(enforce_csrf_checks=True)
        self.user = User.objects.create_user(
            "session-state@example.com",
            "Public test phrase 918!",
            language="en",
            email_verified=True,
        )

    def state(self):
        return self.client.get("/api/v1/auth/session/")

    def assert_guest(self) -> None:
        response = self.state()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"user": None, "two_factor_pending": False})
        self.assertIn("no-store", response["Cache-Control"])

    def challenge(self) -> TwoFactorLogin:
        TwoFactorSettings.objects.get_or_create(user=self.user, defaults={"enabled": True})
        challenge = TwoFactorLogin.objects.create(
            user=self.user,
            password_auth_hash=self.user.get_session_auth_hash(),
            session_generation=self.user.session_generation,
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        session = self.client.session
        session[LOGIN_KEY] = str(challenge.pk)
        session.save()
        return challenge

    def test_guest_is_normal_and_does_not_create_a_session(self) -> None:
        response = self.state()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"user": None, "two_factor_pending": False})
        self.assertNotIn("sessionid", response.cookies)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(self.client.get("/api/v1/auth/me/").status_code, 403)

    def test_authenticated_response_is_private_and_uses_existing_user_serializer(self) -> None:
        User.objects.create_user("other-state@example.com", language="ar", email_verified=True)
        self.client.force_login(self.user)
        response = self.state()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"], self.client.get("/api/v1/auth/me/").json())
        self.assertEqual(response.json()["user"]["id"], self.user.pk)
        self.assertFalse(response.json()["two_factor_pending"])
        self.assertIn("no-store", response["Cache-Control"])

    def test_logout_and_revocation_return_guest(self) -> None:
        self.client.force_login(self.user)
        self.client.logout()
        self.assert_guest()
        self.client.force_login(self.user)
        revoke_user_sessions(self.user)
        self.assert_guest()

    def test_unverified_user_gets_no_identity(self) -> None:
        self.user.email_verified = False
        self.user.save(update_fields=["email_verified"])
        self.client.force_login(self.user)
        self.assert_guest()

    def test_pending_factor_is_browser_bound_and_never_returns_private_identity(self) -> None:
        self.challenge()
        response = self.state()
        self.assertEqual(response.json(), {"user": None, "two_factor_pending": True})
        self.assertEqual(
            APIClient().get("/api/v1/auth/session/").json()["two_factor_pending"], False
        )
        self.assertEqual(self.client.get("/api/v1/settings/").status_code, 403)

    def test_expired_consumed_and_exhausted_challenges_are_not_resumed(self) -> None:
        for changes in [
            {"expires_at": timezone.now() - timedelta(seconds=1)},
            {"consumed": True},
            {"failed_attempts": 5},
        ]:
            with self.subTest(changes=changes):
                challenge = self.challenge()
                TwoFactorLogin.objects.filter(pk=challenge.pk).update(**changes)
                self.assert_guest()

    def test_revoked_or_password_changed_factor_challenge_is_not_resumed(self) -> None:
        self.challenge()
        User.objects.filter(pk=self.user.pk).update(session_generation=1)
        self.assert_guest()
        self.user.refresh_from_db()
        self.challenge()
        self.user.set_password("Another public test phrase 527!")
        self.user.save(update_fields=["password"])
        self.assert_guest()
