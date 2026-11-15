from django.core import mail
from django.test import TestCase
from rest_framework.test import APIClient

from .models import User


class LoginTests(TestCase):
    def setUp(self) -> None:
        self.password = "A private test phrase 371!"
        self.user = User.objects.create_user(
            "signin@example.com",
            self.password,
            display_name="Sign-in test",
            language="en",
            email_verified=True,
        )
        self.client = APIClient(enforce_csrf_checks=True)
        self.refresh_csrf()

    def refresh_csrf(self) -> None:
        self.token = self.client.get("/api/v1/auth/csrf/").json()["csrf_token"]

    def post_login(self, email: str = "signin@example.com", password: str | None = None):
        return self.client.post(
            "/api/v1/auth/login/",
            {"email": email, "password": password or self.password},
            format="json",
            HTTP_X_CSRFTOKEN=self.token,
        )

    def test_verified_user_signs_in_with_normalized_email_and_safe_profile(self) -> None:
        response = self.post_login(email="SIGNIN@EXAMPLE.COM")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], self.user.pk)
        self.assertNotIn("password", response.json())
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(self.client.get("/api/v1/auth/me/").status_code, 200)
        self.assertFalse(hasattr(mail, "outbox") and mail.outbox)

    def test_wrong_and_unknown_credentials_have_identical_response(self) -> None:
        wrong = self.post_login(password="Wrong phrase")
        unknown = self.post_login(email="unknown@example.com")
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(wrong.json(), unknown.json())
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_unverified_and_inactive_users_cannot_login(self) -> None:
        for flags in [{"email_verified": False}, {"email_verified": True, "is_active": False}]:
            User.objects.filter(pk=self.user.pk).update(**flags)
            self.assertEqual(self.post_login().status_code, 401)

    def test_anonymous_login_requires_csrf(self) -> None:
        response = APIClient(enforce_csrf_checks=True).post(
            "/api/v1/auth/login/",
            {"email": self.user.email, "password": self.password},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_login_rotates_session_and_csrf_then_logout_ends_access(self) -> None:
        session = self.client.session
        session["before_login"] = True
        session.save()
        old_key = session.session_key
        self.assertEqual(self.post_login().status_code, 200)
        self.assertNotEqual(old_key, self.client.session.session_key)
        old_token = self.token
        self.refresh_csrf()
        self.assertNotEqual(old_token, self.token)
        response = self.client.post(
            "/api/v1/auth/logout/", {}, format="json", HTTP_X_CSRFTOKEN=self.token
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.client.get("/api/v1/auth/me/").status_code, 403)

    def test_login_rate_limit_is_separate_from_email_code_allowance(self) -> None:
        for _ in range(10):
            self.assertEqual(self.post_login(password="Wrong phrase").status_code, 401)
        self.assertEqual(self.post_login().status_code, 429)
        from .services import enforce_request_limit

        enforce_request_limit(f"email:{self.user.email}", 3)

    def test_registration_verification_logout_login_round_trip(self) -> None:
        import re

        registration = self.client.post(
            "/api/v1/auth/register/",
            {
                "email": "new@example.com",
                "display_name": "New user",
                "password": self.password,
                "language": "ar",
                "timezone": "Asia/Baghdad",
            },
            format="json",
            HTTP_X_CSRFTOKEN=self.token,
        )
        self.assertEqual(registration.status_code, 202)
        code = re.search(r"[0-9]{6}", mail.outbox[-1].body).group()
        verified = self.client.post(
            "/api/v1/auth/verify-email/", {"code": code}, format="json", HTTP_X_CSRFTOKEN=self.token
        )
        self.assertEqual(verified.status_code, 200)
        self.refresh_csrf()
        self.client.post("/api/v1/auth/logout/", {}, format="json", HTTP_X_CSRFTOKEN=self.token)
        self.refresh_csrf()
        self.assertEqual(self.post_login(email="new@example.com").status_code, 200)
