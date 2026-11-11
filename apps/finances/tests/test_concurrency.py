from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from django.db import IntegrityError, close_old_connections, transaction
from django.test import TransactionTestCase

from apps.accounts.models import User
from apps.finances.models import Account, Balance, Category, CommandRecord, Operation
from apps.finances.services import core


class FinancialTransactionTests(TransactionTestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="ConcurrencyFinance", password=None)
        self.month = date(2026, 11, 1)
        core.execute(
            self.user,
            uuid4(),
            "setup",
            {},
            lambda portfolio: core.initialize(
                portfolio,
                {
                    "month": self.month,
                    "timezone": "UTC",
                    "budget": {"IQD": Decimal("100"), "USD": Decimal("0")},
                    "main_savings": {"IQD": Decimal("0"), "USD": Decimal("0")},
                },
            ),
        )
        self.budget = Account.objects.get(owner=self.user, kind="budget")
        self.category = Category.objects.filter(owner=self.user).first()

    def expense(self) -> dict:
        return {
            "local_date": date(2026, 11, 10),
            "timezone": "UTC",
            "amount": Decimal("80"),
            "currency": "IQD",
            "source_id": self.budget.pk,
            "category_id": self.category.pk,
        }

    def race(self, action) -> list:
        barrier = Barrier(2)

        def invoke(index: int):
            close_old_connections()
            try:
                owner = User.objects.get(pk=self.user.pk)
                barrier.wait(timeout=10)
                try:
                    return action(owner, index)
                except core.Conflict:
                    return "conflict"
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            return list(executor.map(invoke, [0, 1]))

    def test_concurrent_spending_has_one_winner_without_overdraft(self) -> None:
        data = self.expense()
        results = self.race(
            lambda owner, index: core.execute(
                owner,
                uuid4(),
                "expense",
                data,
                lambda portfolio: core.transact(portfolio, "expense", data),
            )
        )
        self.assertEqual(results.count("conflict"), 1)
        self.assertEqual(
            Balance.objects.get(account=self.budget, currency="IQD").amount, Decimal("20")
        )
        self.assertEqual(Operation.objects.filter(kind="expense").count(), 1)

    def test_concurrent_idempotent_replay_returns_one_operation(self) -> None:
        key = uuid4()
        data = self.expense()
        results = self.race(
            lambda owner, index: core.execute(
                owner,
                key,
                "expense",
                data,
                lambda portfolio: core.transact(portfolio, "expense", data),
            )
        )
        self.assertEqual(results[0], results[1])
        self.assertEqual(CommandRecord.objects.filter(key=key).count(), 1)
        self.assertEqual(Operation.objects.filter(kind="expense").count(), 1)

    def test_concurrent_rollover_happens_once_for_both_currencies(self) -> None:
        data = {
            "month": date(2026, 12, 1),
            "timezone": "UTC",
            "salary": {"IQD": Decimal("50"), "USD": Decimal("0")},
        }
        results = self.race(
            lambda owner, index: core.execute(
                owner, uuid4(), "rollover", data, lambda portfolio: core.rollover(portfolio, data)
            )
        )
        self.assertEqual(results.count("conflict"), 1)
        self.assertEqual(
            Balance.objects.get(account=self.budget, currency="IQD").amount, Decimal("50")
        )
        self.assertEqual(Operation.objects.filter(kind="rollover").count(), 1)

    def test_injected_ledger_failure_rolls_back_balance_operation_and_key(self) -> None:
        data = self.expense()
        key = uuid4()
        with (
            patch(
                "apps.finances.services.core.LedgerEntry.objects.create",
                side_effect=RuntimeError("synthetic failure"),
            ),
            self.assertRaises(RuntimeError),
        ):
            core.execute(
                self.user,
                key,
                "expense",
                data,
                lambda portfolio: core.transact(portfolio, "expense", data),
            )
        self.assertEqual(
            Balance.objects.get(account=self.budget, currency="IQD").amount, Decimal("100")
        )
        self.assertFalse(CommandRecord.objects.filter(key=key).exists())
        self.assertFalse(Operation.objects.filter(kind="expense").exists())

    def test_database_constraints_reject_negative_balance_and_duplicate_currency(self) -> None:
        with self.assertRaises(IntegrityError), transaction.atomic():
            Balance.objects.filter(account=self.budget, currency="IQD").update(amount=-1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Balance.objects.create(account=self.budget, currency="IQD")
