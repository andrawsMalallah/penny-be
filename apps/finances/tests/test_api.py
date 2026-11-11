from decimal import Decimal
from uuid import uuid4

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.finances.models import (
    Account,
    Category,
    CommandRecord,
    Operation,
)
from tests.financial_helpers import FinancialApiTestMixin


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class FinancialApiTests(FinancialApiTestMixin, TestCase):
    def test_setup_and_owned_reads_have_exact_balances_and_pagination(self) -> None:
        response = self.client.get("/api/v1/finances/accounts/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("next", response.json())
        self.assertEqual(len(response.json()["results"]), 2)
        self.assertEqual(Category.objects.filter(owner=self.user).count(), 10)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.reconcile()

    def test_writes_require_csrf_uuid_decimal_strings_and_strict_fields(self) -> None:
        invalid_client = APIClient(enforce_csrf_checks=True)
        invalid_client.cookies = self.client.cookies
        self.assertEqual(
            invalid_client.post(
                "/api/v1/finances/income/", self.dated(currency="USD", amount="1.00"), format="json"
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/api/v1/finances/income/", self.dated(currency="USD", amount="1.00"), format="json"
            ).status_code,
            400,
        )
        for change in (
            {"amount": 1.1},
            {"amount": "0.00"},
            {"amount": "1.001"},
            {"owner_id": 99},
            {"timezone": "Invalid/Timezone"},
            {"local_date": "2026-10-10"},
        ):
            response = self.write("expenses", {**self.expense("1.00"), **change})
            self.assertIn(response.status_code, (400, 409))
        self.assertEqual(self.amount(self.budget), Decimal("100"))

    def test_split_is_exact_atomic_and_stale_confirmation_is_rejected(self) -> None:
        data = self.expense("150.00")
        rejected = self.write("expenses", data)
        self.assertEqual(rejected.status_code, 409)
        preview = self.client.post(
            "/api/v1/finances/spending-preview/",
            self.dated(source_id=self.budget.pk, currency="IQD", amount="150.00"),
            format="json",
        ).json()
        confirmed = [
            {"account_id": item["account_id"], "amount": item["amount"]}
            for item in preview["allocations"]
        ]
        self.assertEqual(self.write("expenses", self.expense("10.00")).status_code, 200)
        self.assertEqual(
            self.write("expenses", {**data, "allocations": confirmed}).status_code, 409
        )
        fresh = self.write("expenses", data).json()["allocations"]
        response = self.write(
            "expenses",
            {
                **data,
                "allocations": [
                    {"account_id": item["account_id"], "amount": item["amount"]} for item in fresh
                ],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.amount(self.budget), Decimal("0"))
        self.assertEqual(self.amount(self.savings), Decimal("40"))
        self.assertEqual(self.amount(self.budget, "USD"), Decimal("10"))
        self.reconcile()

    def test_idempotency_returns_original_result_and_conflicting_reuse_fails(self) -> None:
        key = str(uuid4())
        first = self.write("expenses", self.expense("5.00"), key)
        second = self.write("expenses", self.expense("5.00"), key)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(self.amount(self.budget), Decimal("95"))
        self.assertEqual(self.write("expenses", self.expense("6.00"), key).status_code, 409)

    def test_insufficient_funds_never_uses_other_currencies_or_named_accounts(self) -> None:
        response = self.write(
            "accounts/create", self.dated(name="Car", opening={"IQD": "500.00", "USD": "0.00"})
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.write("expenses", self.expense("201.00")).status_code, 409)
        self.assertEqual(self.amount(self.budget), Decimal("100"))
        self.reconcile()

    def test_transfers_exchanges_rounding_and_original_rate_reversal(self) -> None:
        created = self.write(
            "accounts/create", self.dated(name="Car", opening={"IQD": "0.00", "USD": "0.00"})
        ).json()
        target = Account.objects.get(pk=created["account_id"])
        self.assertEqual(
            self.write(
                "transfers",
                self.dated(
                    source_id=self.budget.pk, target_id=target.pk, currency="IQD", amount="10.00"
                ),
            ).status_code,
            200,
        )
        exchange = self.write(
            "exchanges",
            self.dated(source_id=self.budget.pk, currency="USD", amount="1.00", rate="1.005000"),
        )
        self.assertEqual(exchange.status_code, 200)
        self.assertEqual(exchange.json()["details"]["received"], "1.01")
        self.write("preferences", {"exchange_rate": "999.000000"})
        self.assertEqual(
            self.write(
                f"activity/{exchange.json()['operation_id']}/reverse", self.dated()
            ).status_code,
            200,
        )
        self.assertEqual(self.amount(self.budget), Decimal("90.00"))
        self.assertEqual(self.amount(self.budget, "USD"), Decimal("10.00"))
        self.assertEqual(
            self.write(
                "exchanges",
                self.dated(
                    source_id=self.budget.pk, currency="IQD", amount="0.01", rate="999999.000000"
                ),
            ).status_code,
            400,
        )
        self.reconcile()

    def test_expense_reverse_replace_is_atomic_and_preserves_history(self) -> None:
        original = self.write("expenses", self.expense("20.00")).json()["operation_id"]
        payload = self.dated(replacement={"kind": "expense", "data": self.expense("201.00")})
        self.assertEqual(self.write(f"activity/{original}/reverse", payload).status_code, 409)
        self.assertFalse(Operation.objects.filter(reverses_id=original).exists())
        self.assertEqual(self.amount(self.budget), Decimal("80"))
        payload["replacement"]["data"] = self.expense("5.00")
        self.assertEqual(self.write(f"activity/{original}/reverse", payload).status_code, 200)
        self.assertEqual(self.amount(self.budget), Decimal("95"))
        reports = self.client.get("/api/v1/finances/reports/").json()
        self.assertEqual(reports["expense"]["IQD"], "5.00")
        self.reconcile()

    def test_rollover_together_skips_months_blocks_duplicates_and_closed_edits(self) -> None:
        original = self.write("expenses", self.expense("20.00")).json()["operation_id"]
        data = {
            "month": "2027-02-01",
            "timezone": "Asia/Baghdad",
            "salary": {"IQD": "250.00", "USD": "0.00"},
        }
        key = str(uuid4())
        first = self.write("rollover", data, key)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(self.write("rollover", data, key).json(), first.json())
        self.assertEqual(self.write("rollover", data).status_code, 409)
        self.assertEqual(self.amount(self.budget), Decimal("250"))
        self.assertEqual(self.amount(self.savings), Decimal("180"))
        self.assertEqual(self.amount(self.savings, "USD"), Decimal("10"))
        self.assertEqual(
            self.write(
                f"activity/{original}/reverse", {"local_date": "2027-02-02", "timezone": "UTC"}
            ).status_code,
            409,
        )
        self.reconcile()

    def test_archive_metadata_and_cross_user_reads_and_writes(self) -> None:
        created = self.write(
            "accounts/create", self.dated(name="Car", opening={"IQD": "1.00", "USD": "0.00"})
        ).json()
        self.assertEqual(
            self.write(f"accounts/{created['account_id']}/change", {"archived": True}).status_code,
            409,
        )
        self.assertEqual(
            self.write(f"accounts/{self.budget.pk}/change", {"name": "Changed"}).status_code, 400
        )
        other = User.objects.create_user(username="OtherFinance", password="Synthetic!Password23")
        self.client.force_authenticate(user=other)
        self.assertEqual(self.client.get("/api/v1/finances/accounts/").json()["results"], [])
        operation_id = Operation.objects.filter(owner=self.user).first().pk
        self.assertEqual(
            self.client.get(f"/api/v1/finances/activity/{operation_id}/").status_code, 404
        )
        self.assertEqual(
            self.write(f"accounts/{created['account_id']}/change", {"name": "Stolen"}).status_code,
            404,
        )
        self.assertFalse(CommandRecord.objects.filter(owner=other).exists())

    def test_reversal_insufficient_funds_rolls_back(self) -> None:
        income = self.write("income", self.dated(currency="IQD", amount="10.00")).json()[
            "operation_id"
        ]
        split = [
            {"account_id": self.budget.pk, "amount": "110.00"},
            {"account_id": self.savings.pk, "amount": "100.00"},
        ]
        self.assertEqual(
            self.write("expenses", self.expense("210.00", allocations=split)).status_code, 200
        )
        self.assertEqual(self.write(f"activity/{income}/reverse", self.dated()).status_code, 409)
        self.assertFalse(Operation.objects.filter(reverses_id=income).exists())
        self.reconcile()

    def test_previews_are_read_only_stale_revision_fails_and_salary_is_income(self) -> None:
        exchange = self.dated(
            source_id=self.budget.pk, currency="IQD", amount="1.00", rate="8.000000"
        )
        before = Operation.objects.count()
        quote = self.client.post("/api/v1/finances/exchange-preview/", exchange, format="json")
        self.assertEqual(quote.status_code, 200)
        self.assertEqual(quote.json()["quote"]["received"], "0.13")
        self.assertEqual(Operation.objects.count(), before)
        self.write("income", self.dated(amount="1.00", currency="IQD"))
        self.assertEqual(
            self.write(
                "exchanges", {**exchange, "expected_revision": quote.json()["revision"]}
            ).status_code,
            409,
        )
        rollover = {
            "month": "2026-12-01",
            "timezone": "UTC",
            "salary": {"IQD": "20.00", "USD": "0.00"},
        }
        preview = self.client.post("/api/v1/finances/rollover-preview/", rollover, format="json")
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(
            self.write(
                "rollover", {**rollover, "expected_revision": preview.json()["revision"]}
            ).status_code,
            200,
        )
        report = self.client.get("/api/v1/finances/reports/?start=2026-12-01&end=2026-12-31").json()
        self.assertEqual(report["income"]["IQD"], "20.00")
        self.reconcile()

    def test_category_report_includes_reversal_and_session_is_rechecked_under_lock(self) -> None:
        from unittest.mock import patch

        from apps.accounts.models import DeviceSession
        from apps.finances.services import core

        original = self.write("expenses", self.expense("1.00")).json()["operation_id"]
        self.write(f"activity/{original}/reverse", self.dated())
        report = self.client.get(f"/api/v1/finances/reports/?category_id={self.category.pk}").json()
        self.assertEqual(report["expense"]["IQD"], "0.00")
        execute = core.execute

        def revoke_before_lock(*args, **kwargs):
            DeviceSession.objects.filter(user=self.user).delete()
            return execute(*args, **kwargs)

        with patch("apps.finances.api.views.core.execute", side_effect=revoke_before_lock):
            self.assertEqual(
                self.write("income", self.dated(amount="5.00", currency="IQD")).status_code, 403
            )
        self.assertEqual(self.amount(self.budget), Decimal("100.00"))
