from datetime import date
from decimal import Decimal
from unittest import skipUnless
from uuid import uuid4

from django.db import connection
from django.test import TransactionTestCase

import app.finance.tests.test_ledger_concurrency as ledger_tests
from app.finance.models import Debt, MoneyTransaction
from app.finance.services.accounts import accounts_for, archive_account
from app.finance.services.debts import (
    debts_for,
    delete_debt,
    delete_repayment,
    save_debt,
    save_repayment,
)
from app.finance.services.ledger import save_transaction


@skipUnless(connection.vendor == "postgresql", "Requires disposable PostgreSQL test database")
class DebtConcurrencyTests(TransactionTestCase):
    simultaneously = ledger_tests.LedgerConcurrencyTests.simultaneously

    def setUp(self) -> None:
        ledger_tests.LedgerConcurrencyTests.setUp(self)
        self.debt_values = {
            "person": None,
            "person_name": "Rima",
            "direction": "owed_to_me",
            "origin": "existing",
            "account": None,
            "currency": "IQD",
            "amount": Decimal("100.00"),
            "date": date(2020, 1, 1),
            "note": "",
            "timezone": "UTC",
            "request_id": uuid4(),
        }
        self.payment_values = {
            "account": self.account.pk,
            "amount": Decimal("60.00"),
            "date": date(2026, 1, 2),
            "note": "",
            "timezone": "UTC",
            "request_id": uuid4(),
        }

    def test_simultaneous_repayments_never_exceed_debt(self) -> None:
        debt, _ = save_debt(self.user, self.debt_values)
        results = self.simultaneously(
            lambda: save_repayment(self.user, debt.pk, self.payment_values),
            lambda: save_repayment(
                self.user, debt.pk, {**self.payment_values, "request_id": uuid4()}
            ),
        )
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(debts_for(self.user).get().remaining, Decimal("40"))
        self.assertEqual(accounts_for(self.user).get().balance_iqd, Decimal("160"))

    def test_debt_reduction_and_repayment_serialize(self) -> None:
        debt, _ = save_debt(self.user, self.debt_values)
        results = self.simultaneously(
            lambda: save_debt(
                self.user, {**self.debt_values, "amount": Decimal("50"), "version": 1}, debt.pk
            ),
            lambda: save_repayment(self.user, debt.pk, self.payment_values),
        )
        self.assertEqual(sorted(results), [False, True])
        self.assertGreaterEqual(debts_for(self.user).get().remaining, 0)

    def test_debt_delete_and_repayment_serialize(self) -> None:
        debt, _ = save_debt(self.user, self.debt_values)
        results = self.simultaneously(
            lambda: delete_debt(self.user, debt.pk, 1),
            lambda: save_repayment(self.user, debt.pk, self.payment_values),
        )
        self.assertEqual(sorted(results), [False, True])

    def test_new_lending_competes_with_expense(self) -> None:
        results = self.simultaneously(
            lambda: save_debt(
                self.user,
                {
                    **self.debt_values,
                    "origin": "movement",
                    "account": self.account.pk,
                    "amount": Decimal("60"),
                    "date": date(2026, 1, 2),
                },
            ),
            lambda: save_transaction(self.user, {**self.values, "request_id": uuid4()}),
        )
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(accounts_for(self.user).get().balance_iqd, Decimal("40"))
        self.assertEqual(Debt.objects.count(), 1 if results[0] else 0)

    def test_duplicate_debt_request_only_creates_one_principal(self) -> None:
        values = {
            **self.debt_values,
            "origin": "movement",
            "account": self.account.pk,
            "amount": Decimal("60"),
            "date": date(2026, 1, 2),
        }
        self.assertEqual(
            self.simultaneously(
                lambda: save_debt(self.user, values), lambda: save_debt(self.user, values)
            ),
            [True, True],
        )
        self.assertEqual(Debt.objects.count(), 1)
        self.assertEqual(MoneyTransaction.objects.count(), 1)
        self.assertEqual(accounts_for(self.user).get().balance_iqd, Decimal("40"))

    def test_duplicate_repayment_only_applies_once(self) -> None:
        debt, _ = save_debt(self.user, self.debt_values)
        self.assertEqual(
            self.simultaneously(
                lambda: save_repayment(self.user, debt.pk, self.payment_values),
                lambda: save_repayment(self.user, debt.pk, self.payment_values),
            ),
            [True, True],
        )
        self.assertEqual(debts_for(self.user).get().version, 2)
        self.assertEqual(MoneyTransaction.objects.count(), 1)

    def test_delete_received_repayment_and_expense_serialize(self) -> None:
        debt, _ = save_debt(self.user, self.debt_values)
        payment, _ = save_repayment(self.user, debt.pk, self.payment_values)
        results = self.simultaneously(
            lambda: delete_repayment(self.user, debt.pk, payment.pk, 1),
            lambda: save_transaction(
                self.user, {**self.values, "amount": Decimal("160"), "request_id": uuid4()}
            ),
        )
        self.assertEqual(sorted(results), [False, True])
        self.assertGreaterEqual(accounts_for(self.user).get().balance_iqd, 0)

    def test_two_repayment_edits_only_one_version_wins(self) -> None:
        debt, _ = save_debt(self.user, self.debt_values)
        payment, _ = save_repayment(self.user, debt.pk, self.payment_values)
        results = self.simultaneously(
            lambda: save_repayment(
                self.user,
                debt.pk,
                {**self.payment_values, "amount": Decimal("40"), "version": 1},
                payment.pk,
            ),
            lambda: save_repayment(
                self.user,
                debt.pk,
                {**self.payment_values, "amount": Decimal("50"), "version": 1},
                payment.pk,
            ),
        )
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(MoneyTransaction.objects.get().version, 2)

    def test_account_archive_and_repayment_serialize(self) -> None:
        debt, _ = save_debt(self.user, self.debt_values)
        results = self.simultaneously(
            lambda: archive_account(self.user, self.account.pk, True),
            lambda: save_repayment(self.user, debt.pk, self.payment_values),
        )
        self.assertTrue(results[0])
        account = accounts_for(self.user).get()
        self.assertTrue(account.archived)
        self.assertEqual(account.balance_iqd, Decimal("160") if results[1] else Decimal("100"))
