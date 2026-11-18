from datetime import date
from decimal import Decimal
from unittest import skipUnless
from uuid import uuid4

from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from rest_framework.test import APIClient

from identity.models import User

from . import budgets
from .ledger import delete_transaction, save_transaction
from .models import BudgetRule, BudgetSchedule, ExpenseCategory, MoneyTransaction, OpeningBalance
from .services import save_account


class BudgetTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user("budget@example.com", email_verified=True)
        self.other = User.objects.create_user("other-budget@example.com", email_verified=True)
        self.category = ExpenseCategory.objects.create(user=self.user, name_en="Food")
        self.client = APIClient(enforce_csrf_checks=True)
        self.client.force_login(self.user)

    def post(self, **changes: object):
        token = self.client.get("/api/v1/auth/csrf/").json()["csrf_token"]
        values = {
            "month": "2026-01",
            "scope": "future",
            "version": 0,
            "limit_iqd": "100.00",
            "limit_usd": None,
            **changes,
        }
        return self.client.post(
            f"/api/v1/budgets/{self.category.pk}/", values, format="json", HTTP_X_CSRFTOKEN=token
        )

    def detail(self, month: str = "2026-01") -> dict:
        response = self.client.get(f"/api/v1/budgets/{self.category.pk}/?month={month}")
        self.assertEqual(response.status_code, 200)
        return response.json()

    def listing(self, month: str = "2026-01"):
        return self.client.get(f"/api/v1/budgets/?month={month}")

    def test_reads_do_not_initialize_and_no_budget_before_start(self) -> None:
        self.assertEqual(self.detail()["version"], 0)
        self.assertEqual(self.listing().json()["count"], 0)
        self.assertFalse(BudgetSchedule.objects.exists())
        self.assertFalse(BudgetRule.objects.exists())
        self.assertEqual(self.post().status_code, 200)
        self.assertIsNone(self.detail("2025-12")["IQD"]["limit"])
        for month in ("2026-01", "2026-02", "2030-07", "9999-12"):
            self.assertEqual(self.detail(month)["IQD"]["limit"], "100.00")
        self.assertEqual(BudgetRule.objects.count(), 1)
        self.assertFalse(MoneyTransaction.objects.exists())

    def test_month_override_then_future_change_preserves_past(self) -> None:
        self.post(limit_usd="50")
        self.post(month="2026-02", scope="month", version=1, limit_iqd="150", limit_usd=None)
        self.assertEqual(self.detail("2026-02")["IQD"]["limit"], "150.00")
        self.assertIsNone(self.detail("2026-02")["USD"]["limit"])
        self.assertEqual(self.detail("2026-03")["USD"]["limit"], "50.00")
        self.post(month="2026-04", scope="month", version=2, limit_iqd="180")
        self.post(month="2026-03", scope="future", version=3, limit_iqd="200", limit_usd="80")
        self.assertEqual(self.detail("2026-01")["IQD"]["limit"], "100.00")
        self.assertEqual(self.detail("2026-02")["IQD"]["limit"], "150.00")
        self.assertEqual(self.detail("2026-04")["IQD"]["limit"], "200.00")
        self.assertEqual(self.detail("2027-01")["USD"]["limit"], "80.00")

    def test_month_removal_does_not_fall_back_and_future_removal_retains_version(self) -> None:
        self.post()
        self.post(month="2026-02", scope="month", version=1, limit_iqd=None)
        self.assertEqual(self.listing("2026-02").json()["count"], 0)
        self.assertIsNone(self.detail("2026-02")["IQD"]["limit"])
        self.assertEqual(self.detail("2026-03")["IQD"]["limit"], "100.00")
        self.post(month="2026-03", scope="future", version=2, limit_iqd=None)
        self.assertEqual(self.listing("2027-01").json()["count"], 0)
        self.assertEqual(self.detail("2027-01")["version"], 3)
        self.assertEqual(self.detail("2026-01")["IQD"]["limit"], "100.00")
        self.assertEqual(self.post(version=0).status_code, 409)

    def test_stale_version_and_duplicate_retry_cannot_change_rules(self) -> None:
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.post().status_code, 409)
        self.assertEqual(self.post(version=99).status_code, 409)
        self.assertEqual(BudgetRule.objects.count(), 1)
        self.assertEqual(self.detail()["version"], 1)

    def test_early_year_and_leap_month_boundaries(self) -> None:
        self.assertEqual(self.post(month="0001-01").status_code, 200)
        self.assertEqual(self.detail("0001-01")["month"], "0001-01")
        self.assertEqual(self.detail("2024-02")["IQD"]["limit"], "100.00")

    def test_validation_rejects_invalid_money_month_scope_and_unknown_fields(self) -> None:
        for field in ("limit_iqd", "limit_usd"):
            for amount in (
                "0",
                "-1",
                "0.001",
                "",
                "NaN",
                "Infinity",
                100,
                True,
                "10000000000000000",
            ):
                with self.subTest(field=field, amount=amount):
                    self.assertEqual(self.post(**{field: amount}).status_code, 400)
        for month in ("0000-01", "2026-13", "2026-00", "2026-1", "2026-01-01", "bad"):
            self.assertEqual(self.post(month=month).status_code, 400)
            self.assertEqual(self.listing(month).status_code, 400)
        self.assertEqual(self.post(scope="unknown").status_code, 400)
        self.assertEqual(self.post(user=self.other.pk).status_code, 400)
        self.assertEqual(self.post(version=-1).status_code, 400)
        self.assertFalse(BudgetSchedule.objects.exists())

    def test_privacy_csrf_and_verified_auth(self) -> None:
        self.post()
        self.client.force_login(self.other)
        self.assertEqual(self.listing().json()["count"], 0)
        self.assertEqual(self.post(version=1).status_code, 404)
        self.assertEqual(
            self.client.get(f"/api/v1/budgets/{self.category.pk}/?month=2026-01").status_code, 404
        )
        self.client.cookies.clear()
        self.client.force_login(self.user)
        self.assertEqual(
            self.client.post(f"/api/v1/budgets/{self.category.pk}/", {}, format="json").status_code,
            403,
        )
        self.user.email_verified = False
        self.user.save(update_fields=["email_verified"])
        self.assertEqual(self.listing().status_code, 403)
        self.assertEqual(self.post(version=1).status_code, 403)
        self.client.logout()
        self.assertEqual(self.listing().status_code, 403)

    def test_archived_categories_keep_reports_and_existing_edits(self) -> None:
        self.post()
        self.category.archived = True
        self.category.save(update_fields=["archived"])
        self.assertTrue(self.listing().json()["results"][0]["archived"])
        self.assertEqual(self.post(version=1, limit_iqd="200").status_code, 200)
        archived = ExpenseCategory.objects.create(user=self.user, name_en="Old", archived=True)
        self.category = archived
        self.assertEqual(self.post().status_code, 400)

    def test_live_expenses_only_currency_separation_no_rollover_and_corrections(self) -> None:
        self.post(limit_iqd="20", limit_usd="10")
        account = save_account(
            self.user,
            {
                "name": "Wallet",
                "group": None,
                "opening_iqd": Decimal("1000"),
                "opening_usd": Decimal("1000"),
            },
        )
        OpeningBalance.objects.filter(account=account).update(effective_date=date(2025, 1, 1))
        values = {
            "account": account.pk,
            "category": self.category.pk,
            "kind": "expense",
            "currency": "IQD",
            "amount": Decimal("30.25"),
            "date": date(2026, 1, 31),
            "note": "",
            "timezone": "UTC",
            "request_id": uuid4(),
        }
        entry, _ = save_transaction(self.user, values)
        save_transaction(
            self.user,
            {**values, "currency": "USD", "amount": Decimal("2.15"), "request_id": uuid4()},
        )
        save_transaction(
            self.user, {**values, "category": None, "kind": "income", "request_id": uuid4()}
        )
        save_transaction(self.user, {**values, "date": date(2026, 2, 1), "request_id": uuid4()})
        row = self.detail()
        self.assertEqual(
            row["IQD"], {"limit": "20.00", "spent": "30.25", "remaining": "-10.25", "over": True}
        )
        self.assertEqual(row["USD"]["spent"], "2.15")
        self.assertEqual(self.detail("2026-02")["IQD"]["limit"], "20.00")
        self.assertEqual(self.detail("2026-02")["IQD"]["spent"], "30.25")
        changed, _ = save_transaction(
            self.user, {**values, "amount": Decimal("10"), "version": 1}, entry.pk
        )
        self.assertEqual(self.detail()["IQD"]["remaining"], "10.00")
        delete_transaction(self.user, entry.pk, changed.version)
        self.assertEqual(self.detail()["IQD"]["spent"], "0.00")
        self.assertEqual(self.detail("2026-02")["IQD"]["spent"], "30.25")

    def test_pagination_and_fixed_query_count(self) -> None:
        for index in range(35):
            category = ExpenseCategory.objects.create(user=self.user, name_en=f"Category {index}")
            budgets.save_budget(
                self.user,
                category.pk,
                {
                    "month": date(2026, 1, 1),
                    "scope": "future",
                    "version": 0,
                    "limit_iqd": Decimal("1"),
                    "limit_usd": None,
                },
            )
        response = self.listing().json()
        self.assertEqual(response["count"], 35)
        self.assertEqual(len(response["results"]), 30)
        self.assertIn("month=2026-01", response["next"])
        self.assertEqual(len(self.client.get(response["next"]).json()["results"]), 5)
        categories = list(budgets.active_categories(self.user, date(2026, 1, 1))[:30])
        with self.assertNumQueries(2):
            budgets.budget_rows(categories, self.user, date(2026, 1, 1))

    @skipUnless(connection.vendor == "postgresql", "Exact maximum money requires PostgreSQL")
    def test_full_decimal_precision(self) -> None:
        self.assertEqual(self.post(limit_iqd="9999999999999999.99").status_code, 200)
        self.assertEqual(self.detail()["IQD"]["limit"], "9999999999999999.99")

    def test_database_constraints(self) -> None:
        self.post()
        schedule = BudgetSchedule.objects.get(category=self.category)
        for values in (
            {"month": date(2026, 2, 2)},
            {"limit_iqd": Decimal("0")},
            {"limit_usd": Decimal("-1")},
        ):
            with self.assertRaises(IntegrityError), transaction.atomic():
                BudgetRule.objects.create(
                    schedule=schedule,
                    month=values.get("month", date(2026, 2, 1)),
                    recurring=True,
                    limit_iqd=values.get("limit_iqd"),
                    limit_usd=values.get("limit_usd"),
                )
