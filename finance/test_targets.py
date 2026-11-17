from datetime import date
from decimal import Decimal
from uuid import uuid4

from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from rest_framework.test import APIClient

from identity.models import User

from .ledger import save_transaction
from .models import Account, MoneyTransaction, OpeningBalance


class AccountTargetTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user("targets@example.com", email_verified=True)
        self.other = User.objects.create_user("other-targets@example.com", email_verified=True)
        self.client = APIClient(enforce_csrf_checks=True)
        self.client.force_login(self.user)
        self.values = {
            "name": "Savings",
            "group": None,
            "opening_iqd": "50.00",
            "opening_usd": "100.00",
        }

    def post(self, path: str, values: dict):
        token = self.client.get("/api/v1/auth/csrf/").json()["csrf_token"]
        return self.client.post(f"/api/v1/{path}", values, format="json", HTTP_X_CSRFTOKEN=token)

    def create(self, **values: object):
        return self.post("accounts/", {**self.values, **values})

    def test_optional_independent_targets_and_null_defaults(self) -> None:
        account = self.create().json()
        self.assertIsNone(account["target_iqd"])
        self.assertIsNone(account["target_usd"])
        self.assertEqual(account["target_version"], 1)
        self.assertEqual(
            self.create(name="Goal", target_iqd="0.01", target_usd="500.10").json()["target_usd"],
            "500.10",
        )
        self.assertEqual(
            self.create(name="IQD only", target_iqd="20").json()["target_iqd"], "20.00"
        )
        self.assertIsNone(self.create(name="USD only", target_usd="20").json()["target_iqd"])

    def test_edit_remove_and_legacy_metadata_preserve_money_and_other_target(self) -> None:
        account = self.create(target_iqd="80.00", target_usd="200.00").json()
        path = f"accounts/{account['id']}/"
        edit = self.post(
            path, {"name": "Savings", "group": None, "target_iqd": None, "target_version": 1}
        )
        self.assertEqual(edit.status_code, 200)
        self.assertIsNone(edit.json()["target_iqd"])
        self.assertEqual(edit.json()["target_usd"], "200.00")
        self.assertEqual(edit.json()["target_version"], 2)
        legacy = self.post(path, {"name": "Renamed", "group": None}).json()
        self.assertEqual(legacy["target_usd"], "200.00")
        self.assertEqual(legacy["target_version"], 2)
        for field in ("opening", "balance_iqd", "balance_usd"):
            self.assertEqual(legacy[field], account[field])
        self.assertEqual(OpeningBalance.objects.count(), 1)
        self.assertFalse(MoneyTransaction.objects.exists())

    def test_stale_or_missing_version_cannot_overwrite_targets_or_metadata(self) -> None:
        account = self.create(target_usd="200").json()
        path = f"accounts/{account['id']}/"
        values = {"name": "Savings", "group": None, "target_usd": "300", "target_version": 1}
        self.assertEqual(self.post(path, values).status_code, 200)
        self.assertEqual(
            self.post(path, {**values, "name": "Stale", "target_usd": None}).status_code, 409
        )
        values.pop("target_version")
        self.assertEqual(self.post(path, values).status_code, 400)
        saved = self.client.get(f"/api/v1/{path}").json()
        self.assertEqual(saved["name"], "Savings")
        self.assertEqual(saved["target_usd"], "300.00")

    def test_invalid_targets_are_rejected_atomically_for_both_currencies(self) -> None:
        for field in ("target_iqd", "target_usd"):
            for amount in (
                "0",
                "-0.01",
                "NaN",
                "Infinity",
                "0.001",
                "10000000000000000",
                "",
                100,
                True,
            ):
                with self.subTest(field=field, amount=amount):
                    self.assertEqual(self.create(**{field: amount}).status_code, 400)
        self.assertFalse(Account.objects.exists())

    def test_targets_are_private_and_require_verified_session_and_csrf(self) -> None:
        account = self.create(target_usd="200").json()
        path = f"accounts/{account['id']}/"
        values = {"name": "Changed", "group": None, "target_usd": None, "target_version": 1}
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(f"/api/v1/{path}").status_code, 404)
        self.assertEqual(self.post(path, values).status_code, 404)
        self.assertEqual(self.client.get("/api/v1/accounts/").json()["count"], 0)
        self.client.force_login(self.user)
        self.client.cookies.clear()
        self.client.force_login(self.user)
        self.assertEqual(
            self.client.post(f"/api/v1/{path}", values, format="json").status_code, 403
        )
        self.user.email_verified = False
        self.user.save(update_fields=["email_verified"])
        self.assertEqual(self.post(path, values).status_code, 403)
        self.client.logout()
        self.assertEqual(self.post(path, values).status_code, 403)

    def test_archive_restore_preserve_targets_and_archived_edit_is_rejected(self) -> None:
        account = self.create(target_iqd="80", target_usd="200").json()
        path = f"accounts/{account['id']}/"
        self.assertEqual(
            self.post(path + "archive/", {"archived": True}).json()["target_usd"], "200.00"
        )
        self.assertEqual(
            self.post(
                path, {"name": "Savings", "group": None, "target_usd": None, "target_version": 1}
            ).status_code,
            400,
        )
        self.assertEqual(
            self.post(path + "archive/", {"archived": False}).json()["target_iqd"], "80.00"
        )

    def test_targets_do_not_cap_balances_or_change_after_money_moves(self) -> None:
        account = self.create(target_usd="0.01").json()
        self.assertEqual(account["balance_usd"], "100.00")
        values = {
            "account": account["id"],
            "destination": None,
            "category": None,
            "kind": "income",
            "currency": "USD",
            "amount": Decimal("25"),
            "date": date.today(),
            "note": "",
            "timezone": "UTC",
            "request_id": uuid4(),
            "exchange_rate": None,
            "received_amount": None,
        }
        save_transaction(self.user, values)
        current = self.client.get(f"/api/v1/accounts/{account['id']}/").json()
        self.assertEqual(current["balance_usd"], "125.00")
        self.assertEqual(current["target_usd"], "0.01")
        self.assertEqual(current["target_version"], 1)

    def test_database_rejects_nonpositive_targets(self) -> None:
        account = self.create().json()
        for field in ("target_iqd", "target_usd"):
            with self.assertRaises(IntegrityError), transaction.atomic():
                Account.objects.filter(pk=account["id"]).update(**{field: Decimal("0")})

    def test_maximum_target_precision(self) -> None:
        if connection.vendor != "postgresql":
            self.skipTest("Full decimal precision requires PostgreSQL")
        account = self.create(target_iqd="9999999999999999.99", target_usd="0.01").json()
        saved = self.client.get(f"/api/v1/accounts/{account['id']}/").json()
        self.assertEqual(saved["target_iqd"], "9999999999999999.99")
        self.assertEqual(saved["target_usd"], "0.01")
