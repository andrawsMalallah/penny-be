from decimal import Decimal
from unittest.mock import patch

from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from app.finance.models import Account, AccountGroup, OpeningBalance
from app.finance.services.accounts import save_account
from app.identity.models import User, UserSettings


class AccountsTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(
            "accounts@example.com", language="en", email_verified=True
        )
        self.other = User.objects.create_user(
            "other@example.com", language="en", email_verified=True
        )
        self.client = APIClient(enforce_csrf_checks=True)
        self.client.force_login(self.user)
        self.values = {
            "name": "Wallet",
            "group": None,
            "opening_iqd": "250000.25",
            "opening_usd": "100.10",
        }
        self.preferences = {"language": "en", "default_currency": "IQD", "exchange_rate": None}

    def post(self, path: str, values: dict):
        token = self.client.get("/api/v1/auth/csrf/").json()["csrf_token"]
        return self.client.post(f"/api/v1/{path}", values, format="json", HTTP_X_CSRFTOKEN=token)

    def create(self, **overrides: object):
        return self.post("accounts/", {**self.values, **overrides})

    def test_creation_records_exact_opening_money_and_fresh_list(self) -> None:
        response = self.create()
        self.assertEqual(response.status_code, 201)
        account = response.json()
        self.assertEqual(account["balance_iqd"], "250000.25")
        self.assertEqual(account["balance_usd"], "100.10")
        self.assertEqual(account["opening"]["iqd"], "250000.25")
        self.assertTrue(account["opening"]["recorded_at"])
        self.assertNotIn("user", account)
        self.assertEqual(OpeningBalance.objects.get().usd, Decimal("100.10"))
        self.assertEqual(self.client.get("/api/v1/accounts/").json()["results"], [account])
        self.assertIn("no-store", response["Cache-Control"])
        self.assertFalse(UserSettings.objects.exists())

    def test_empty_reads_do_not_create_data(self) -> None:
        for path in ["accounts/", "account-groups/"]:
            response = self.client.get(f"/api/v1/{path}")
            self.assertEqual(response.json()["results"], [])
            self.assertIn("no-store", response["Cache-Control"])
        self.assertFalse(Account.objects.exists())
        self.assertFalse(AccountGroup.objects.exists())

    def test_invalid_money_is_rejected_without_any_account(self) -> None:
        for amount in [
            "-0.01",
            "NaN",
            "Infinity",
            "0.001",
            "10000000000000000",
            "",
            1.25,
            True,
            None,
        ]:
            with self.subTest(amount=amount):
                self.assertEqual(self.create(opening_iqd=amount).status_code, 400)
        self.assertFalse(Account.objects.exists())
        self.assertFalse(OpeningBalance.objects.exists())

    def test_maximum_money_and_zero_are_exact(self) -> None:
        if connection.vendor != "postgresql":
            self.skipTest("Full decimal precision requires PostgreSQL, not SQLite NUMERIC affinity")
        response = self.create(opening_iqd="9999999999999999.99", opening_usd="0.00")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["balance_iqd"], "9999999999999999.99")
        self.assertEqual(response.json()["balance_usd"], "0.00")

    def test_creation_rolls_back_when_opening_cannot_be_written(self) -> None:
        with patch(
            "app.finance.services.accounts.OpeningBalance.objects.create",
            side_effect=RuntimeError("test"),
        ):
            with self.assertRaises(RuntimeError):
                save_account(
                    self.user,
                    {**self.values, "opening_iqd": Decimal("1"), "opening_usd": Decimal("0")},
                )
        self.assertFalse(Account.objects.exists())

    def test_names_groups_and_opening_history_survive_edit(self) -> None:
        group = self.post("account-groups/", {"name": "Savings"}).json()
        account = self.create(group=group["id"]).json()
        response = self.post(f"accounts/{account['id']}/", {"name": "New wallet", "group": None})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["opening"], account["opening"])
        self.assertEqual(response.json()["balance_iqd"], account["balance_iqd"])
        self.assertEqual(response.json()["group"], None)
        self.assertEqual(self.post(f"accounts/{account['id']}/", {**self.values}).status_code, 400)
        self.assertEqual(
            self.post(f"account-groups/{group['id']}/", {"name": "Goals"}).status_code, 200
        )

    def test_owner_fields_unknown_fields_and_invalid_names_are_rejected(self) -> None:
        for values in [
            {"user": self.other.pk},
            {"archived": True},
            {"balance_iqd": "100"},
            {"name": " "},
            {"name": "x" * 101},
        ]:
            self.assertEqual(self.create(**values).status_code, 400)
        self.assertEqual(
            self.post("account-groups/", {"name": "Daily", "user": self.other.pk}).status_code, 400
        )

    def test_duplicate_names_include_archived_items_and_are_private(self) -> None:
        account = self.create().json()
        self.assertEqual(self.create(name=" wallet ").status_code, 400)
        self.post(f"accounts/{account['id']}/archive/", {"archived": True})
        self.assertEqual(self.create(name="WALLET").status_code, 400)
        self.client.force_login(self.other)
        self.assertEqual(self.create().status_code, 201)
        self.post("account-groups/", {"name": "Daily"})
        self.assertEqual(self.post("account-groups/", {"name": "daily"}).status_code, 400)

    def test_cross_user_reads_and_mutations_are_hidden(self) -> None:
        group = self.post("account-groups/", {"name": "Daily"}).json()
        account = self.create(group=group["id"]).json()
        self.client.force_login(self.other)
        for resource, record in [("accounts", account), ("account-groups", group)]:
            path = f"{resource}/{record['id']}/"
            self.assertEqual(self.client.get(f"/api/v1/{resource}/").json()["count"], 0)
            self.assertEqual(self.client.get(f"/api/v1/{path}").status_code, 404)
            values = {"name": "Hijack", **({"group": None} if resource == "accounts" else {})}
            self.assertEqual(self.post(path, values).status_code, 404)
            self.assertEqual(self.post(f"{path}archive/", {"archived": True}).status_code, 404)
        self.assertEqual(self.create(group=group["id"]).status_code, 400)
        self.assertEqual(
            self.post(
                "settings/", {**self.preferences, "default_account": account["id"]}
            ).status_code,
            400,
        )

    def test_archive_keeps_funds_clears_default_and_restores(self) -> None:
        account = self.create().json()
        self.assertEqual(
            self.post(
                "settings/", {**self.preferences, "default_account": account["id"]}
            ).status_code,
            200,
        )
        self.assertEqual(
            self.post(f"accounts/{account['id']}/archive/", {"archived": True}).status_code, 200
        )
        self.assertEqual(self.client.get("/api/v1/accounts/").json()["count"], 0)
        archived = self.client.get("/api/v1/accounts/?archived=true").json()["results"][0]
        self.assertEqual(archived["opening"], account["opening"])
        self.assertEqual(archived["balance_iqd"], "250000.25")
        self.assertIsNone(self.client.get("/api/v1/settings/").json()["default_account"])
        self.assertEqual(
            self.post(
                "settings/", {**self.preferences, "default_account": account["id"]}
            ).status_code,
            400,
        )
        self.assertEqual(
            self.post(f"accounts/{account['id']}/", {"name": "Other", "group": None}).status_code,
            400,
        )
        self.assertEqual(
            self.post(f"accounts/{account['id']}/archive/", {"archived": False}).status_code, 200
        )
        self.assertEqual(self.client.get("/api/v1/accounts/").json()["count"], 1)
        self.assertIsNone(UserSettings.objects.get(user=self.user).default_account_id)

    def test_group_archive_requires_no_active_accounts_and_restore_requires_active_group(
        self,
    ) -> None:
        group = self.post("account-groups/", {"name": "Daily"}).json()
        account = self.create(group=group["id"]).json()
        group_path = f"account-groups/{group['id']}/archive/"
        account_path = f"accounts/{account['id']}/archive/"
        self.assertEqual(self.post(group_path, {"archived": True}).status_code, 400)
        self.post(account_path, {"archived": True})
        self.assertEqual(self.post(group_path, {"archived": True}).status_code, 200)
        self.assertEqual(self.create(name="Another", group=group["id"]).status_code, 400)
        self.assertEqual(self.post(account_path, {"archived": False}).status_code, 400)
        self.assertEqual(self.post(group_path, {"archived": False}).status_code, 200)
        self.assertEqual(self.post(account_path, {"archived": False}).status_code, 200)

    def test_settings_invalid_default_is_atomic_and_omission_preserves_selection(self) -> None:
        account = self.create().json()
        self.post("settings/", {**self.preferences, "default_account": account["id"]})
        self.assertEqual(
            self.post(
                "settings/", {**self.preferences, "language": "ar", "default_account": 999999}
            ).status_code,
            400,
        )
        self.user.refresh_from_db()
        self.assertEqual(self.user.language, "en")
        self.assertEqual(
            self.post("settings/", self.preferences).json()["default_account"], account["id"]
        )
        self.assertIsNone(
            self.post("settings/", {**self.preferences, "default_account": None}).json()[
                "default_account"
            ]
        )

    def test_paginated_list_has_bounded_queries(self) -> None:
        for index in range(32):
            save_account(
                self.user,
                {
                    **self.values,
                    "name": f"Account {index:02}",
                    "opening_iqd": Decimal("1"),
                    "opening_usd": Decimal("2"),
                },
            )
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get("/api/v1/accounts/")
        data = response.json()
        self.assertEqual(data["count"], 32)
        self.assertEqual(len(data["results"]), 30)
        self.assertTrue(data["next"])
        self.assertLess(len(captured), 15)
        self.assertEqual(len(self.client.get("/api/v1/accounts/?page=2").json()["results"]), 2)
        self.assertEqual(self.client.get("/api/v1/accounts/?archived=invalid").status_code, 400)

    def test_database_rejects_negative_opening_money(self) -> None:
        account = Account.objects.create(user=self.user, name="Invalid")
        with self.assertRaises(IntegrityError), transaction.atomic():
            OpeningBalance.objects.create(
                account=account, iqd="-1", usd="0", effective_date="2026-01-01"
            )

    def test_every_financial_endpoint_requires_csrf_verified_auth_and_has_no_delete(self) -> None:
        group = self.post("account-groups/", {"name": "Daily"}).json()
        account = self.create().json()
        for resource, record in [("accounts", account), ("account-groups", group)]:
            paths = [
                f"{resource}/",
                f"{resource}/{record['id']}/",
                f"{resource}/{record['id']}/archive/",
            ]
            for path in paths:
                self.client.cookies.pop("csrftoken", None)
                self.assertEqual(
                    self.client.post(f"/api/v1/{path}", {}, format="json").status_code, 403
                )
            self.assertEqual(
                self.client.delete(f"/api/v1/{resource}/{record['id']}/").status_code, 403
            )
            token = self.client.get("/api/v1/auth/csrf/").json()["csrf_token"]
            self.assertEqual(
                self.client.delete(
                    f"/api/v1/{resource}/{record['id']}/", HTTP_X_CSRFTOKEN=token
                ).status_code,
                405,
            )
        self.client.logout()
        for verified in [None, False]:
            if verified is False:
                self.user.email_verified = False
                self.user.save()
                self.client.force_login(self.user)
            for resource, record in [("accounts", account), ("account-groups", group)]:
                self.assertEqual(self.client.get(f"/api/v1/{resource}/").status_code, 403)
                self.assertEqual(
                    self.client.get(f"/api/v1/{resource}/{record['id']}/").status_code, 403
                )
                self.assertEqual(self.post(f"{resource}/", self.values).status_code, 403)
                self.assertEqual(
                    self.post(
                        f"{resource}/{record['id']}/archive/", {"archived": True}
                    ).status_code,
                    403,
                )
