from decimal import Decimal
from unittest import skipUnless
from uuid import uuid4

from django.db import connection
from django.test import TransactionTestCase

import app.finance.tests.test_ledger_concurrency as ledger_tests
from app.finance.services.accounts import accounts_for, save_account
from app.finance.services.ledger import save_transaction


@skipUnless(connection.vendor == "postgresql", "Requires disposable PostgreSQL test database")
class TargetConcurrencyTests(TransactionTestCase):
    simultaneously = ledger_tests.LedgerConcurrencyTests.simultaneously

    def setUp(self) -> None:
        ledger_tests.LedgerConcurrencyTests.setUp(self)
        self.target_values = {
            "name": "Wallet",
            "group": None,
            "target_iqd": Decimal("200"),
            "target_version": 1,
        }

    def test_competing_target_edits_reject_stale_version(self) -> None:
        results = self.simultaneously(
            lambda: save_account(self.user, self.target_values, self.account.pk),
            lambda: save_account(
                self.user, {**self.target_values, "target_iqd": Decimal("300")}, self.account.pk
            ),
        )
        self.assertEqual(sorted(results), [False, True])
        account = accounts_for(self.user).get(pk=self.account.pk)
        self.assertEqual(account.target_version, 2)
        self.assertIn(account.target_iqd, [Decimal("200"), Decimal("300")])
        self.assertEqual(account.balance_iqd, Decimal("100"))

    def test_target_edit_and_spending_preserve_both_changes(self) -> None:
        self.assertEqual(
            self.simultaneously(
                lambda: save_account(self.user, self.target_values, self.account.pk),
                lambda: save_transaction(self.user, {**self.values, "request_id": uuid4()}),
            ),
            [True, True],
        )
        account = accounts_for(self.user).get(pk=self.account.pk)
        self.assertEqual(account.target_iqd, Decimal("200"))
        self.assertEqual(account.balance_iqd, Decimal("40"))
