from datetime import UTC, date, datetime
from decimal import Decimal
from unittest import skipUnless
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext

import app.finance.tests.test_analytics as report_tests
import app.finance.tests.test_ledger_concurrency as race_tests
from app.finance.models import BudgetRule, BudgetSchedule, ExpenseCategory, MoneyTransaction
from app.finance.services.accounts import save_account
from app.finance.services.budgets import save_budget
from app.finance.services.ledger import save_transaction
from app.finance.services.monthly_review import monthly_period, monthly_review
from app.identity.models import UserSettings


class MonthlyReviewTests(TestCase):
    entry = report_tests.AnalyticsTests.entry
    debt = report_tests.AnalyticsTests.debt

    def setUp(self) -> None:
        report_tests.AnalyticsTests.setUp(self)
        self.url = "/api/v1/analytics/monthly/"

    def get(self, **values: object):
        return self.client.get(self.url, {"month": "2026-11", "timezone": "Asia/Baghdad", **values})

    def budget(self, category=None, month: str = "2026-11") -> dict:
        return save_budget(
            self.user,
            (category or self.category).pk,
            {
                "month": date.fromisoformat(month + "-01"),
                "version": 0,
                "scope": "future",
                "limit_iqd": Decimal("100"),
                "limit_usd": Decimal("10"),
            },
        )

    def test_current_month_uses_elapsed_dates_and_full_budget_limit(self) -> None:
        self.budget()
        self.entry("income", "200.25")
        self.entry("expense", "50.10")
        before = self.entry("expense", "20")
        before.date = date(2026, 10, 18)
        before.save()
        excluded = self.entry("income", "999")
        excluded.date = date(2026, 10, 19)
        excluded.save()
        # Another device timezone can already record tomorrow; this report ends at its today.
        tomorrow = self.entry("expense", "40")
        tomorrow.date = date(2026, 11, 19)
        tomorrow.save()
        data = self.get().json()
        self.assertEqual(data["period"]["end"], "2026-11-18")
        self.assertEqual(data["period"]["previous_end"], "2026-10-18")
        self.assertTrue(data["period"]["incomplete"])
        self.assertEqual(
            data["activity"]["IQD"],
            {
                "income": "200.25",
                "expense": "50.10",
                "difference": "150.15",
                "previous_income": "0.00",
                "previous_expense": "20.00",
                "previous_difference": "-20.00",
                "income_change": "200.25",
                "expense_change": "30.10",
                "difference_change": "170.15",
            },
        )
        self.assertEqual(data["budgets"]["results"][0]["IQD"]["spent"], "50.10")
        self.assertEqual(data["budgets"]["results"][0]["IQD"]["limit"], "100.00")

    def test_past_months_use_full_months_and_calendar_edges(self) -> None:
        cases = [
            ("2026-10", "2026-10-31", "2026-09-01", "2026-09-30"),
            ("2024-02", "2024-02-29", "2024-01-01", "2024-01-31"),
            ("2026-01", "2026-01-31", "2025-12-01", "2025-12-31"),
            ("0001-02", "0001-02-28", "0001-01-01", "0001-01-31"),
        ]
        for month, end, previous_start, previous_end in cases:
            with self.subTest(month=month):
                data = self.get(month=month).json()["period"]
                self.assertEqual(
                    (data["end"], data["previous_start"], data["previous_end"]),
                    (end, previous_start, previous_end),
                )
                self.assertFalse(data["incomplete"])

    def test_current_month_end_clamping_and_timezone_boundary(self) -> None:
        with patch(
            "app.finance.services.monthly_review.timezone.now",
            return_value=datetime(2024, 3, 31, 12, tzinfo=UTC),
        ):
            period = monthly_period(date(2024, 3, 1), "UTC")
            self.assertEqual(period["previous_end"], date(2024, 2, 29))
        with patch(
            "app.finance.services.monthly_review.timezone.now",
            return_value=datetime(2026, 12, 1, 0, 30, tzinfo=UTC),
        ):
            baghdad = monthly_period(date(2026, 11, 1), "Asia/Baghdad")
            west = monthly_period(date(2026, 11, 1), "America/New_York")
            self.assertFalse(baghdad["incomplete"])
            self.assertTrue(west["incomplete"])
            self.assertEqual(west["previous_end"], date(2026, 10, 30))

    def test_top_five_are_sorted_separately_per_currency_with_stable_ties(self) -> None:
        for index in range(7):
            category = ExpenseCategory.objects.create(
                user=self.user, name_en=f"Category {index}", archived=True
            )
            for currency, amount in [("IQD", str(index + 1)), ("USD", str(7 - index))]:
                MoneyTransaction.objects.create(
                    user=self.user,
                    account=self.account,
                    kind="expense",
                    category=category,
                    amount=amount,
                    currency=currency,
                    date=date(2026, 11, 10),
                    request_id=uuid4(),
                    creation_payload={},
                )
        data = self.get().json()
        for currency in ("IQD", "USD"):
            self.assertEqual(len(data["top_categories"][currency]), 5)
            self.assertEqual(
                [row[currency] for row in data["top_categories"][currency]],
                ["7.00", "6.00", "5.00", "4.00", "3.00"],
            )
        self.assertNotEqual(
            data["top_categories"]["IQD"][0]["category"],
            data["top_categories"]["USD"][0]["category"],
        )
        self.assertEqual(data["activity"]["IQD"]["expense"], "28.00")
        first_category = ExpenseCategory.objects.get(user=self.user, name_en="Category 0")
        MoneyTransaction.objects.filter(category=first_category, currency="IQD").update(amount="7")
        tied = self.get().json()["top_categories"]["IQD"]
        self.assertEqual(tied[0]["category"], first_category.pk)

    def test_exclusions_private_scope_deleted_and_archived_history(self) -> None:
        self.account.archived = True
        self.account.save(update_fields=["archived"])
        self.entry("income", "3.25")
        self.entry("expense", "4.50", "USD")
        self.entry("income", "999", deleted_at=datetime(2026, 11, 11, tzinfo=UTC))
        # Exchange and transfer fixture fields satisfy the existing ledger constraints.
        self.entry(
            "exchange", "1", "USD", exchange_rate=Decimal("1500"), received_amount=Decimal("1500")
        )
        destination = save_account(
            self.user,
            {
                "name": "Other wallet",
                "group": None,
                "opening_iqd": Decimal("0"),
                "opening_usd": Decimal("0"),
            },
        )
        self.entry("transfer", "100", destination=destination)
        debt = self.debt("i_owe", "USD", "20")
        debt.origin, debt.account, debt.date = "movement", self.account, date(2026, 11, 1)
        debt.save(update_fields=["origin", "account", "date"])
        self.entry("debt_in", "20", "USD", debt=debt, debt_role="principal")
        self.entry("debt_out", "5", "USD", debt=debt, debt_role="repayment")
        report = self.get().json()
        self.assertEqual(report["activity"]["IQD"]["income"], "3.25")
        self.assertEqual(report["activity"]["USD"]["difference"], "-4.50")
        self.assertEqual(report["activity"]["IQD"]["expense"], "0.00")
        self.client.force_login(self.other)
        empty = self.get().json()
        self.assertEqual(empty["activity"]["IQD"]["income"], "0.00")
        self.assertEqual(empty["budgets"]["count"], 0)
        self.assertEqual(empty["top_categories"]["USD"], [])
        self.assertIn("no-store", self.get()["Cache-Control"])
        self.assertFalse(UserSettings.objects.exists())
        self.assertFalse(BudgetSchedule.objects.filter(category__user=self.other).exists())

    def test_budget_paging_and_rules_preserve_prior_month_and_currency_limits(self) -> None:
        for index in range(31):
            category = ExpenseCategory.objects.create(user=self.user, name_en=f"Budget {index}")
            self.budget(category, "2026-10")
        first = self.get().json()
        self.assertEqual(first["budgets"]["count"], 31)
        self.assertEqual(len(first["budgets"]["results"]), 10)
        parsed = urlsplit(first["budgets"]["next"])
        self.assertEqual(parse_qs(parsed.query)["timezone"], ["Asia/Baghdad"])
        second = self.client.get(first["budgets"]["next"]).json()
        self.assertEqual(len(second["budgets"]["results"]), 10)
        self.assertEqual(second["activity"], first["activity"])
        with CaptureQueriesContext(connection) as captured:
            monthly_review(self.user, monthly_period(date(2026, 11, 1), "Asia/Baghdad"))
        self.assertLessEqual(len(captured), 12)
        self.assertEqual(self.get(page=5).status_code, 404)
        category = ExpenseCategory.objects.get(pk=first["budgets"]["results"][0]["category"])
        save_budget(
            self.user,
            category.pk,
            {
                "month": date(2026, 11, 1),
                "version": 1,
                "scope": "month",
                "limit_iqd": None,
                "limit_usd": Decimal("20"),
            },
        )
        row = self.get().json()["budgets"]["results"][0]
        self.assertIsNone(row["IQD"]["limit"])
        self.assertEqual(row["USD"]["limit"], "20.00")
        self.assertEqual(
            self.get(month="2026-10").json()["budgets"]["results"][0]["IQD"]["limit"], "100.00"
        )

    def test_validation_auth_and_get_purity(self) -> None:
        for extra in [
            {"month": "bad"},
            {"month": "2026-12"},
            {"month": "0001-01"},
            {"month": "0000-01"},
            {"timezone": "Invalid/Zone"},
            {"page": 0},
            {"user": self.other.pk},
            {"start": "2026-01-01"},
        ]:
            self.assertEqual(self.get(**extra).status_code, 400)
        counts = (
            BudgetSchedule.objects.count(),
            BudgetRule.objects.count(),
            MoneyTransaction.objects.count(),
        )
        self.assertEqual(self.get().status_code, 200)
        self.assertEqual(
            counts,
            (
                BudgetSchedule.objects.count(),
                BudgetRule.objects.count(),
                MoneyTransaction.objects.count(),
            ),
        )
        self.client.logout()
        self.assertEqual(self.get().status_code, 403)
        self.user.email_verified = False
        self.user.save(update_fields=["email_verified"])
        self.client.force_login(self.user)
        self.assertEqual(self.get().status_code, 403)

    def test_refresh_reflects_correction_and_deletion(self) -> None:
        entry = self.entry("expense", "10.10")
        self.budget()
        self.assertEqual(self.get().json()["budgets"]["results"][0]["IQD"]["spent"], "10.10")
        entry.amount = Decimal("20.25")
        entry.save(update_fields=["amount"])
        data = self.get().json()
        self.assertEqual(data["activity"]["IQD"]["expense"], "20.25")
        self.assertEqual(data["top_categories"]["IQD"][0]["IQD"], "20.25")
        entry.deleted_at = datetime(2026, 11, 11, tzinfo=UTC)
        entry.save(update_fields=["deleted_at"])
        self.assertEqual(self.get().json()["budgets"]["results"][0]["IQD"]["spent"], "0.00")

    @skipUnless(connection.vendor == "postgresql", "Exact maximum money requires PostgreSQL")
    def test_large_exact_totals_above_single_entry_capacity(self) -> None:
        for _ in range(2):
            self.entry("income", "9999999999999999.99")
        self.entry("expense", "0.01")
        data = self.get().json()["activity"]["IQD"]
        self.assertEqual(data["income"], "19999999999999999.98")
        self.assertEqual(data["difference"], "19999999999999999.97")
        self.assertEqual(data["difference_change"], "19999999999999999.97")


@skipUnless(connection.vendor == "postgresql", "Requires disposable PostgreSQL test database")
class MonthlyReviewConcurrencyTests(TransactionTestCase):
    simultaneously = race_tests.LedgerConcurrencyTests.simultaneously

    def setUp(self) -> None:
        race_tests.LedgerConcurrencyTests.setUp(self)

    def test_report_never_mixes_totals_categories_and_budgets_during_spending(self) -> None:
        save_budget(
            self.user,
            self.category.pk,
            {
                "month": date(2026, 1, 1),
                "version": 0,
                "scope": "future",
                "limit_iqd": Decimal("100"),
                "limit_usd": None,
            },
        )
        reports = []
        period = monthly_period(date(2026, 1, 1), "UTC")
        self.assertEqual(
            self.simultaneously(
                lambda: reports.append(monthly_review(self.user, period)),
                lambda: save_transaction(self.user, {**self.values, "request_id": uuid4()}),
            ),
            [True, True],
        )
        data = reports[0]
        spent = data["activity"]["IQD"]["expense"]
        self.assertIn(spent, ["0.00", "60.00"])
        self.assertEqual(data["budgets"]["results"][0]["IQD"]["spent"], spent)
        if data["top_categories"]["IQD"]:
            self.assertEqual(data["top_categories"]["IQD"][0]["IQD"], spent)
        else:
            self.assertEqual(spent, "0.00")
