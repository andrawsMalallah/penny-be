from datetime import date
from unittest import skipUnless
from uuid import uuid4

from django.db import connection
from django.test import TransactionTestCase

import app.finance.tests.test_ledger_concurrency as ledger_tests
from app.finance.services.analytics import summary
from app.finance.services.ledger import save_transaction


@skipUnless(connection.vendor == "postgresql", "Requires disposable PostgreSQL test database")
class AnalyticsConcurrencyTests(TransactionTestCase):
    simultaneously = ledger_tests.LedgerConcurrencyTests.simultaneously

    def setUp(self) -> None:
        ledger_tests.LedgerConcurrencyTests.setUp(self)

    def test_summary_never_mixes_before_and_after_a_competing_expense(self) -> None:
        results = []
        period = {
            "key": "custom",
            "start": date(2026, 1, 1),
            "end": date(2026, 1, 2),
            "previous_start": date(2025, 12, 30),
            "previous_end": date(2025, 12, 31),
            "today": date(2026, 1, 2),
        }
        self.assertEqual(
            self.simultaneously(
                lambda: results.append(summary(self.user, period)),
                lambda: save_transaction(self.user, {**self.values, "request_id": uuid4()}),
            ),
            [True, True],
        )
        result = results[0]
        pair = (result["current"]["IQD"]["held"], result["activity"]["IQD"]["expense"])
        self.assertIn(pair, [("100.00", "0.00"), ("40.00", "60.00")])
