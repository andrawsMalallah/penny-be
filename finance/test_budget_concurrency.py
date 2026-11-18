from datetime import date
from decimal import Decimal
from unittest import skipUnless
from uuid import uuid4

from django.db import connection
from django.test import TransactionTestCase

from . import test_ledger_concurrency as ledger_tests
from .budgets import budget_rows, categories_for_month, save_budget
from .ledger import save_transaction
from .models import BudgetRule, BudgetSchedule


@skipUnless(connection.vendor == "postgresql", "Requires disposable PostgreSQL test database")
class BudgetConcurrencyTests(TransactionTestCase):
    simultaneously = ledger_tests.LedgerConcurrencyTests.simultaneously

    def setUp(self) -> None:
        ledger_tests.LedgerConcurrencyTests.setUp(self)
        self.budget_values = {
            "month": date(2026, 1, 1),
            "scope": "future",
            "version": 0,
            "limit_iqd": Decimal("20"),
            "limit_usd": None,
        }

    def test_competing_first_creates_cannot_overwrite_each_other(self) -> None:
        results = self.simultaneously(
            lambda: save_budget(self.user, self.category.pk, self.budget_values),
            lambda: save_budget(
                self.user, self.category.pk, {**self.budget_values, "limit_iqd": Decimal("30")}
            ),
        )
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(BudgetSchedule.objects.get().version, 1)
        self.assertEqual(BudgetRule.objects.count(), 1)

    def test_budget_changes_do_not_block_overspending(self) -> None:
        self.assertEqual(
            self.simultaneously(
                lambda: save_budget(self.user, self.category.pk, self.budget_values),
                lambda: save_transaction(self.user, {**self.values, "request_id": uuid4()}),
            ),
            [True, True],
        )
        category = categories_for_month(self.user, date(2026, 1, 1)).get(pk=self.category.pk)
        row = budget_rows([category], self.user, date(2026, 1, 1))[0]
        self.assertTrue(row["IQD"]["over"])
        self.assertEqual(row["IQD"]["spent"], "60.00")

    def test_competing_edit_and_removal_require_current_version(self) -> None:
        save_budget(self.user, self.category.pk, self.budget_values)
        values = {**self.budget_values, "version": 1}
        results = self.simultaneously(
            lambda: save_budget(self.user, self.category.pk, {**values, "limit_iqd": None}),
            lambda: save_budget(
                self.user, self.category.pk, {**values, "limit_iqd": Decimal("30")}
            ),
        )
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(BudgetSchedule.objects.get().version, 2)
