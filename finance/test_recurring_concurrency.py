from datetime import date
from decimal import Decimal
from unittest import skipUnless
from uuid import uuid4

from django.db import connection
from django.test import TransactionTestCase

from . import recurring
from . import test_ledger_concurrency as ledger_tests
from .ledger import save_transaction
from .models import MoneyTransaction, RecurringOutcome, RecurringSchedule


@skipUnless(connection.vendor == "postgresql", "Requires disposable PostgreSQL")
class RecurringConcurrencyTests(TransactionTestCase):
    simultaneously = ledger_tests.LedgerConcurrencyTests.simultaneously

    def setUp(self) -> None:
        ledger_tests.LedgerConcurrencyTests.setUp(self)
        self.schedule_values = {
            "name": "Rent",
            "kind": "expense",
            "account": self.account.pk,
            "category": self.category.pk,
            "currency": "IQD",
            "amount": Decimal("60"),
            "note": "",
            "frequency": "monthly",
            "start_date": date(2026, 1, 1),
            "end_date": None,
            "timezone": "UTC",
            "request_id": uuid4(),
        }
        self.schedule, _ = recurring.save_schedule(self.user, self.schedule_values)
        self.resolve_values = {
            "version": 1,
            "scheduled_date": date(2026, 1, 1),
            "timezone": "UTC",
            "transaction": self.values,
        }

    def test_double_confirm_creates_one_transaction(self) -> None:
        results = self.simultaneously(
            lambda: recurring.resolve(self.user, self.schedule.pk, self.resolve_values),
            lambda: recurring.resolve(self.user, self.schedule.pk, self.resolve_values),
        )
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(MoneyTransaction.objects.count(), 1)
        self.assertEqual(RecurringOutcome.objects.count(), 1)

    def test_confirm_skip_race_has_one_outcome(self) -> None:
        results = self.simultaneously(
            lambda: recurring.resolve(self.user, self.schedule.pk, self.resolve_values),
            lambda: recurring.resolve(self.user, self.schedule.pk, self.resolve_values, skip=True),
        )
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(RecurringOutcome.objects.count(), 1)

    def test_confirm_and_spend_cannot_overdraw(self) -> None:
        results = self.simultaneously(
            lambda: recurring.resolve(self.user, self.schedule.pk, self.resolve_values),
            lambda: save_transaction(self.user, {**self.values, "request_id": uuid4()}),
        )
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(MoneyTransaction.objects.count(), 1)

    def test_edit_and_confirm_reject_stale_schedule(self) -> None:
        values = {**self.schedule_values, "version": 1, "amount": Decimal("20")}
        results = self.simultaneously(
            lambda: recurring.resolve(self.user, self.schedule.pk, self.resolve_values),
            lambda: recurring.save_schedule(self.user, values, self.schedule.pk),
        )
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(RecurringSchedule.objects.get().version, 2)
