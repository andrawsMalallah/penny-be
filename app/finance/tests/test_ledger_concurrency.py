from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from threading import Barrier
from unittest import skipUnless
from uuid import uuid4

from django.db import close_old_connections, connection
from django.http import Http404
from django.test import TransactionTestCase
from rest_framework.exceptions import APIException

from app.finance.models import ExpenseCategory, MoneyTransaction, OpeningBalance
from app.finance.services.accounts import accounts_for, archive_account, save_account
from app.finance.services.categories import initialize_categories
from app.finance.services.ledger import delete_transaction, save_transaction
from app.identity.models import User, UserSettings


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
                except Http404:
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

    def transfer_values(self) -> dict:
        target = save_account(
            self.user,
            {
                "name": "Savings",
                "group": None,
                "opening_iqd": Decimal("0"),
                "opening_usd": Decimal("0"),
            },
        )
        OpeningBalance.objects.filter(account=target).update(effective_date=date(2026, 1, 1))
        return {
            **self.values,
            "kind": "transfer",
            "category": None,
            "destination": target.pk,
            "request_id": uuid4(),
        }

    def test_transfer_and_spending_compete_for_same_source(self) -> None:
        values = self.transfer_values()
        results = self.simultaneously(
            lambda: save_transaction(self.user, values),
            lambda: save_transaction(self.user, {**self.values, "request_id": uuid4()}),
        )
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(accounts_for(self.user).get(pk=self.account.pk).balance_iqd, Decimal("40"))
        target_balance = accounts_for(self.user).get(pk=values["destination"]).balance_iqd
        self.assertEqual(target_balance, Decimal("60") if results[0] else Decimal("0"))

    def test_duplicate_transfer_request_applies_both_sides_once(self) -> None:
        values = self.transfer_values()
        self.assertEqual(
            self.simultaneously(
                lambda: save_transaction(self.user, values),
                lambda: save_transaction(self.user, values),
            ),
            [True, True],
        )
        self.assertEqual(MoneyTransaction.objects.count(), 1)
        self.assertEqual(
            accounts_for(self.user).get(pk=values["destination"]).balance_iqd, Decimal("60")
        )

    def test_two_transfer_edits_only_one_version_wins(self) -> None:
        values = self.transfer_values()
        entry, _ = save_transaction(self.user, values)
        results = self.simultaneously(
            lambda: save_transaction(
                self.user, {**values, "amount": Decimal("20"), "version": 1}, entry.pk
            ),
            lambda: save_transaction(
                self.user, {**values, "amount": Decimal("30"), "version": 1}, entry.pk
            ),
        )
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(MoneyTransaction.objects.get().version, 2)
        balances = list(accounts_for(self.user))
        self.assertEqual(sum(account.balance_iqd for account in balances), Decimal("100"))

    def test_transfer_delete_and_destination_spend_preserve_both_histories(self) -> None:
        values = self.transfer_values()
        entry, _ = save_transaction(self.user, values)
        results = self.simultaneously(
            lambda: delete_transaction(self.user, entry.pk, 1),
            lambda: save_transaction(
                self.user, {**self.values, "account": values["destination"], "request_id": uuid4()}
            ),
        )
        self.assertEqual(sorted(results), [False, True])
        for account in accounts_for(self.user):
            self.assertGreaterEqual(account.balance_iqd, Decimal("0"))

    def test_destination_archive_and_new_transfer_serialize(self) -> None:
        values = self.transfer_values()
        results = self.simultaneously(
            lambda: archive_account(self.user, values["destination"], True),
            lambda: save_transaction(self.user, values),
        )
        self.assertTrue(results[0])
        account = accounts_for(self.user).get(pk=values["destination"])
        self.assertTrue(account.archived)
        self.assertEqual(account.balance_iqd, Decimal("60") if results[1] else Decimal("0"))

    def exchange_values(self) -> dict:
        OpeningBalance.objects.filter(account=self.account).update(usd=Decimal("20"))
        UserSettings.objects.create(user=self.user, exchange_rate=Decimal("1000"))
        return {
            **self.values,
            "kind": "exchange",
            "category": None,
            "currency": "USD",
            "amount": Decimal("15.00"),
            "exchange_rate": Decimal("1000.000000"),
            "received_amount": Decimal("15000.00"),
            "request_id": uuid4(),
        }

    def test_exchange_and_spending_compete_for_source_currency(self) -> None:
        values = self.exchange_values()
        results = self.simultaneously(
            lambda: save_transaction(self.user, values),
            lambda: save_transaction(
                self.user,
                {**self.values, "currency": "USD", "amount": Decimal("15"), "request_id": uuid4()},
            ),
        )
        self.assertEqual(sorted(results), [False, True])
        account = accounts_for(self.user).get(pk=self.account.pk)
        self.assertEqual(account.balance_usd, Decimal("5"))
        self.assertEqual(account.balance_iqd, Decimal("15100") if results[0] else Decimal("100"))

    def test_duplicate_exchange_request_applies_once(self) -> None:
        values = self.exchange_values()
        self.assertEqual(
            self.simultaneously(
                lambda: save_transaction(self.user, values),
                lambda: save_transaction(self.user, values),
            ),
            [True, True],
        )
        self.assertEqual(MoneyTransaction.objects.count(), 1)
        self.assertEqual(
            accounts_for(self.user).get(pk=self.account.pk).balance_iqd, Decimal("15100")
        )

    def test_exchange_delete_and_received_spend_preserve_both_currencies(self) -> None:
        values = self.exchange_values()
        entry, _ = save_transaction(self.user, values)
        results = self.simultaneously(
            lambda: delete_transaction(self.user, entry.pk, 1),
            lambda: save_transaction(
                self.user, {**self.values, "amount": Decimal("15050"), "request_id": uuid4()}
            ),
        )
        self.assertEqual(sorted(results), [False, True])
        account = accounts_for(self.user).get(pk=self.account.pk)
        self.assertGreaterEqual(account.balance_iqd, 0)
        self.assertGreaterEqual(account.balance_usd, 0)

    def test_simultaneous_exchange_edits_reject_stale_version(self) -> None:
        values = self.exchange_values()
        entry, _ = save_transaction(self.user, values)
        results = self.simultaneously(
            lambda: save_transaction(
                self.user,
                {
                    **values,
                    "version": 1,
                    "exchange_rate": Decimal("1001"),
                    "received_amount": Decimal("15015"),
                },
                entry.pk,
            ),
            lambda: save_transaction(
                self.user,
                {
                    **values,
                    "version": 1,
                    "exchange_rate": Decimal("1002"),
                    "received_amount": Decimal("15030"),
                },
                entry.pk,
            ),
        )
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(MoneyTransaction.objects.get().version, 2)
