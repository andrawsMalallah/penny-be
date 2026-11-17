from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from threading import Barrier
from unittest import skipUnless
from uuid import uuid4

from django.db import close_old_connections, connection
from django.test import TransactionTestCase
from rest_framework.exceptions import APIException

from identity.models import User

from .categories import initialize_categories
from .ledger import delete_transaction, save_transaction
from .models import ExpenseCategory, MoneyTransaction, OpeningBalance
from .services import accounts_for, archive_account, save_account


@skipUnless(connection.vendor == "postgresql", "Requires disposable PostgreSQL test database")
class LedgerConcurrencyTests(TransactionTestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(
            "ledger-race@example.com", email_verified=True, language="en"
        )
        self.account = save_account(
            self.user,
            {
                "name": "Wallet",
                "group": None,
                "opening_iqd": Decimal("100"),
                "opening_usd": Decimal("0"),
            },
        )
        OpeningBalance.objects.filter(account=self.account).update(effective_date=date(2026, 1, 1))
        initialize_categories(self.user)
        self.category = ExpenseCategory.objects.filter(user=self.user).first()
        self.values = {
            "account": self.account.pk,
            "category": self.category.pk,
            "kind": "expense",
            "currency": "IQD",
            "amount": Decimal("60.00"),
            "date": date(2026, 1, 2),
            "note": "",
            "timezone": "UTC",
        }

    def simultaneously(self, first: Callable, second: Callable) -> list[bool]:
        barrier = Barrier(2, timeout=15)

        def attempt(action: Callable) -> bool:
            close_old_connections()
            try:
                barrier.wait()
                try:
                    action()
                    return True
                except APIException:
                    return False
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            return list(executor.map(attempt, [first, second]))

    def test_simultaneous_spending_never_overdraws(self) -> None:
        results = self.simultaneously(
            lambda: save_transaction(self.user, {**self.values, "request_id": uuid4()}),
            lambda: save_transaction(self.user, {**self.values, "request_id": uuid4()}),
        )
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(MoneyTransaction.objects.count(), 1)
        self.assertEqual(accounts_for(self.user).get(pk=self.account.pk).balance_iqd, Decimal("40"))

    def test_same_request_only_creates_once(self) -> None:
        values = {**self.values, "request_id": uuid4()}
        self.assertEqual(
            self.simultaneously(
                lambda: save_transaction(self.user, values),
                lambda: save_transaction(self.user, values),
            ),
            [True, True],
        )
        self.assertEqual(MoneyTransaction.objects.count(), 1)

    def test_simultaneous_edits_reject_stale_version(self) -> None:
        entry, _ = save_transaction(self.user, {**self.values, "request_id": uuid4()})
        results = self.simultaneously(
            lambda: save_transaction(
                self.user, {**self.values, "version": 1, "amount": Decimal("20")}, entry.pk
            ),
            lambda: save_transaction(
                self.user, {**self.values, "version": 1, "amount": Decimal("30")}, entry.pk
            ),
        )
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(MoneyTransaction.objects.get().version, 2)

    def test_income_deletion_and_spending_preserve_history(self) -> None:
        entry, _ = save_transaction(
            self.user,
            {
                **self.values,
                "kind": "income",
                "category": None,
                "request_id": uuid4(),
                "amount": Decimal("100"),
            },
        )
        results = self.simultaneously(
            lambda: delete_transaction(self.user, entry.pk, 1),
            lambda: save_transaction(
                self.user, {**self.values, "amount": Decimal("150"), "request_id": uuid4()}
            ),
        )
        self.assertEqual(sorted(results), [False, True])
        self.assertGreaterEqual(
            accounts_for(self.user).get(pk=self.account.pk).balance_iqd, Decimal("0")
        )

    def test_account_archive_and_new_spend_serialize(self) -> None:
        results = self.simultaneously(
            lambda: archive_account(self.user, self.account.pk, True),
            lambda: save_transaction(self.user, {**self.values, "request_id": uuid4()}),
        )
        self.assertTrue(results[0])
        self.assertTrue(accounts_for(self.user).get(pk=self.account.pk).archived)
        self.assertIn(
            accounts_for(self.user).get(pk=self.account.pk).balance_iqd,
            [Decimal("40"), Decimal("100")],
        )
