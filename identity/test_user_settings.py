from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase
from rest_framework.test import APIClient

from .models import User, UserSettings


class UserSettingsTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(
            "settings@example.com", "Test settings phrase 918!", language="en", email_verified=True
        )
        self.client = APIClient(enforce_csrf_checks=True)
        self.client.force_login(self.user)
        self.values = {"language": "ar", "default_currency": "USD", "exchange_rate": "1310.123456"}

    def save(self, values: dict):
        token = self.client.get("/api/v1/auth/csrf/").json()["csrf_token"]
        return self.client.post("/api/v1/settings/", values, format="json", HTTP_X_CSRFTOKEN=token)

    def test_fresh_settings_have_no_currency_or_rate_and_get_does_not_write(self) -> None:
        response = self.client.get("/api/v1/settings/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "language": "en",
                "default_currency": "",
                "exchange_rate": None,
                "default_account": None,
            },
        )
        self.assertIn("no-store", response["Cache-Control"])
        self.assertFalse(UserSettings.objects.exists())

    def test_save_is_private_exact_and_updates_login_language(self) -> None:
        other = User.objects.create_user("other@example.com", language="en", email_verified=True)
        UserSettings.objects.create(user=other, default_currency="IQD", exchange_rate="1500")
        self.assertEqual(self.save(self.values).json(), {**self.values, "default_account": None})
        self.assertEqual(
            self.client.get("/api/v1/settings/").json(), {**self.values, "default_account": None}
        )
        self.user.refresh_from_db()
        self.assertEqual(self.user.language, "ar")
        self.assertEqual(
            UserSettings.objects.get(user=self.user).exchange_rate, Decimal("1310.123456")
        )
        self.assertEqual(UserSettings.objects.get(user=other).exchange_rate, Decimal("1500"))
        self.assertEqual(self.client.get("/api/v1/auth/me/").json()["language"], "ar")

    def test_rate_can_be_cleared_and_currency_left_unselected(self) -> None:
        self.save(self.values)
        values = {"language": "en", "default_currency": "", "exchange_rate": None}
        self.assertEqual(self.save(values).json(), {**values, "default_account": None})

    def test_rejects_invalid_rates_without_partial_language_update(self) -> None:
        for rate in [
            "0",
            "-1",
            "NaN",
            "Infinity",
            "1.0000001",
            "1000000000000",
            "abc",
            1310.5,
            True,
        ]:
            with self.subTest(rate=rate):
                self.assertEqual(self.save({**self.values, "exchange_rate": rate}).status_code, 400)
        self.user.refresh_from_db()
        self.assertEqual(self.user.language, "en")
        self.assertFalse(UserSettings.objects.filter(user=self.user).exists())

    def test_rejects_unknown_fields_and_unsupported_choices(self) -> None:
        for extra in [{"user_id": 99}, {"language": "fr"}, {"default_currency": "EUR"}]:
            with self.subTest(extra=extra):
                self.assertEqual(self.save({**self.values, **extra}).status_code, 400)
        self.assertEqual(self.save({}).status_code, 400)

    def test_requires_csrf(self) -> None:
        self.assertEqual(
            self.client.post("/api/v1/settings/", self.values, format="json").status_code, 403
        )

    def test_anonymous_and_unverified_users_cannot_access_settings(self) -> None:
        self.client.logout()
        self.assertEqual(self.client.get("/api/v1/settings/").status_code, 403)
        self.assertEqual(self.save(self.values).status_code, 403)
        self.user.email_verified = False
        self.user.save(update_fields=["email_verified"])
        self.client.force_login(self.user)
        self.assertEqual(self.client.get("/api/v1/settings/").status_code, 403)
        self.assertEqual(self.save(self.values).status_code, 403)

    def test_database_rejects_nonpositive_rate(self) -> None:
        with self.assertRaises(IntegrityError), transaction.atomic():
            UserSettings.objects.create(user=self.user, exchange_rate=Decimal("0"))
