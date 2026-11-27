import csv
from datetime import date
from decimal import Decimal
from io import StringIO
from tempfile import SpooledTemporaryFile
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone

import app.finance.tests.test_ledger_concurrency as race_tests
import app.finance.tests.test_transactions as ledger_tests
from app.finance.models import Debt, DebtPerson, MoneyTransaction
from app.finance.services.accounts import save_account
from app.finance.services.ledger import save_transaction
from app.finance.services.transaction_export import (
    ExportCountChanged,
    export_transactions,
    safe_text,
)


class TransactionExportTests(TransactionTestCase):
    create = ledger_tests.TransactionTests.create
    post = ledger_tests.TransactionTests.post
    expense = ledger_tests.TransactionTests.expense

    def setUp(self) -> None:
        ledger_tests.TransactionTests.setUp(self)
        self.export_url = self.root + "export/"

    def document(self, **filters: object) -> list[list[str]]:
        count = self.client.get(self.export_url + "count/", filters).json()["count"]
        response = self.client.get(
            self.export_url,
            {"language": "en", "expected_count": count, **filters},
            HTTP_ACCEPT="text/csv, application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn("no-store", response["Cache-Control"])
        try:
            content = b"".join(response.streaming_content)
        finally:
            response.close()
        self.assertTrue(content.startswith(b"\xef\xbb\xbf"))
        return list(csv.reader(StringIO(content.decode("utf-8-sig"), newline="")))

    def test_all_pages_order_exact_dates_text_and_no_mutations(self) -> None:
        for index in range(33):
            self.assertEqual(self.create(note=f"Entry {index}").status_code, 201)
        self.assertEqual(len(self.client.get(self.root).json()["results"]), 10)
        snapshot = list(MoneyTransaction.objects.values())
        rows = self.document()
        self.assertEqual(len(rows), 34)
        self.assertEqual(rows[1][0], "2026-01-02")
        self.assertEqual(rows[1][-1], "Entry 32")
        self.assertEqual(rows[-1][-1], "Entry 0")
        self.assertEqual(list(MoneyTransaction.objects.values()), snapshot)

    def test_name_search_matches_export_count_and_rows(self) -> None:
        self.expense(amount="5.00")
        self.create(note="Unrelated income")
        filters = {"account_search": "wall", "category_search": self.category.name_en[:3]}
        self.assertEqual(self.client.get(self.root, filters).json()["count"], 1)
        rows = self.document(**filters)
        self.assertEqual(len(rows), 2)

    def test_shared_filters_include_transfer_destination_and_both_exchange_currencies(self) -> None:
        destination = save_account(
            self.user,
            {
                "name": "Savings",
                "group": None,
                "opening_iqd": Decimal("0"),
                "opening_usd": Decimal("0"),
            },
        )
        # Fixtures exercise export representation independently from ledger validation.
        MoneyTransaction.objects.create(
            user=self.user,
            account=self.account,
            destination=destination,
            kind="transfer",
            currency="IQD",
            amount="10.25",
            date=date(2026, 1, 3),
            request_id=uuid4(),
            creation_payload={},
        )
        MoneyTransaction.objects.create(
            user=self.user,
            account=self.account,
            kind="exchange",
            currency="USD",
            amount="1.25",
            exchange_rate="1500.123456",
            received_amount="1875.15",
            date=date(2026, 1, 4),
            request_id=uuid4(),
            creation_payload={},
        )
        self.create()
        cases = [
            {"account": destination.pk},
            {"currency": "IQD"},
            {"currency": "USD"},
            {"kind": "transfer"},
            {"start": "2026-01-03", "end": "2026-01-04"},
            {"category": self.category.pk},
            {"account": 999999},
        ]
        for filters in cases:
            with self.subTest(filters=filters):
                expected = self.client.get(self.root, filters).json()
                rows = self.document(**filters)
                self.assertEqual(len(rows) - 1, expected["count"])
                self.assertEqual(
                    [row[0] for row in rows[1:]], [r["date"] for r in expected["results"]]
                )
        exchange = self.document(kind="exchange")[1]
        self.assertEqual(exchange[5:10], ["USD", "1.25", "1500.123456", "IQD", "1875.15"])
        transfer = self.document(account=destination.pk)[1]
        self.assertEqual(transfer[2:4], ["Wallet", "Savings"])

    def test_arabic_category_fallback_quoting_and_formula_protection(self) -> None:
        self.account.name = '=HYPERLINK("https://example.com")'
        self.account.archived = True
        self.account.save(update_fields=["name", "archived"])
        self.category.name_en = "Food"
        self.category.name_ar = ""
        self.category.archived = True
        self.category.save(update_fields=["name_en", "name_ar", "archived"])
        MoneyTransaction.objects.create(
            user=self.user,
            account=self.account,
            category=self.category,
            kind="expense",
            currency="IQD",
            amount="0.01",
            date=date(2026, 1, 2),
            note='\t=SUM(1,2)\r\nمرحبا "نص"',
            request_id=uuid4(),
            creation_payload={},
        )
        # Language is a download option, not an Activity filter.
        response = self.client.get(self.export_url, {"language": "ar", "expected_count": 1})
        try:
            rows = list(
                csv.reader(
                    StringIO(b"".join(response.streaming_content).decode("utf-8-sig"), newline="")
                )
            )
        finally:
            response.close()
        self.assertEqual(rows[0][0:2], ["التاريخ", "النوع"])
        self.assertEqual(rows[1][1], "مصروف")
        self.assertTrue(rows[1][2].startswith("'="))
        self.assertEqual(rows[1][4], "Food")
        self.assertEqual(rows[1][-1], '\'\t=SUM(1,2)\r\nمرحبا "نص"')
        for value in ["=1", "+1", "-1", "@SUM(1)", " \ufeff\u200f=1", "\rtext", "＝1"]:
            self.assertEqual(safe_text(value), "'" + value)
        self.assertEqual(safe_text('مرحبا, "نص"\nعادي'), 'مرحبا, "نص"\nعادي')

    def test_owner_verified_scope_tombstones_and_empty_export(self) -> None:
        entry = self.create().json()
        MoneyTransaction.objects.filter(pk=entry["id"]).update(deleted_at=timezone.now())
        self.assertEqual(len(self.document()), 1)
        self.create(note="Private owner record")
        self.client.force_login(self.other)
        self.assertEqual(len(self.document()), 1)
        self.client.logout()
        for suffix in ["", "count/"]:
            self.assertEqual(self.client.get(self.export_url + suffix).status_code, 403)
        self.user.email_verified = False
        self.user.save(update_fields=["email_verified"])
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.export_url + "count/").status_code, 403)

    def test_strict_inputs_stale_count_and_current_edits(self) -> None:
        entry = self.create().json()
        self.assertEqual(
            self.client.get(self.export_url, {"language": "en", "expected_count": 0}).status_code,
            409,
        )
        for extra in [
            {"page": 2},
            {"user": self.other.pk},
            {"language": "fr"},
            {"expected_count": -1},
            {"start": "bad"},
            {"start": "2026-02-01", "end": "2026-01-01"},
        ]:
            self.assertEqual(
                self.client.get(
                    self.export_url, {"language": "en", "expected_count": 1, **extra}
                ).status_code,
                400,
            )
        self.assertEqual(self.client.get(self.export_url + "count/", {"page": 2}).status_code, 400)
        MoneyTransaction.objects.filter(pk=entry["id"]).update(note="Current text")
        self.assertEqual(self.document()[1][-1], "Current text")

    def test_debt_cash_stays_one_row_with_person_and_role(self) -> None:
        person = DebtPerson.objects.create(user=self.user, name="Mona")
        debt = Debt.objects.create(
            user=self.user,
            person=person,
            direction="i_owe",
            origin="movement",
            account=self.account,
            currency="USD",
            amount="10",
            date=date(2026, 1, 2),
            request_id=uuid4(),
            creation_payload={},
        )
        for kind, role in [("debt_in", "principal"), ("debt_out", "repayment")]:
            MoneyTransaction.objects.create(
                user=self.user,
                account=self.account,
                debt=debt,
                debt_role=role,
                kind=kind,
                currency="USD",
                amount="10",
                date=date(2026, 1, 2),
                request_id=uuid4(),
                creation_payload={},
            )
        rows = self.document()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1][10:12], ["Mona", "Repayment"])
        self.assertEqual(rows[2][10:12], ["Mona", "Principal"])

    @skipUnless(connection.vendor == "postgresql", "Exact maximum money requires PostgreSQL")
    def test_maximum_decimal_is_never_rounded_or_exponential(self) -> None:
        MoneyTransaction.objects.create(
            user=self.user,
            account=self.account,
            kind="income",
            currency="IQD",
            amount="9999999999999999.99",
            date=date(2026, 1, 2),
            request_id=uuid4(),
            creation_payload={},
        )
        self.assertEqual(self.document()[1][6], "9999999999999999.99")

    def test_bounded_queries_spill_and_cleanup_on_failure(self) -> None:
        self.create()
        MoneyTransaction.objects.bulk_create(
            [
                MoneyTransaction(
                    user=self.user,
                    account=self.account,
                    kind="income",
                    currency="IQD",
                    amount="0.01",
                    date=date(2026, 1, 2),
                    note="Text " * 100,
                    request_id=uuid4(),
                    creation_payload={},
                )
                for _ in range(2100)
            ]
        )
        # FileResponse closes the request connection; use real transaction boundaries.
        with self.assertNumQueries(5):
            document = export_transactions(self.user, {"language": "en", "expected_count": 2101})
        self.assertTrue(document._rolled)
        self.assertEqual(len(list(csv.reader(StringIO(document.read().decode("utf-8-sig"))))), 2102)
        document.close()
        temporary = SpooledTemporaryFile(mode="w+b")
        with (
            patch(
                "app.finance.services.transaction_export.SpooledTemporaryFile",
                return_value=temporary,
            ),
            patch(
                "app.finance.services.transaction_export.export_row",
                side_effect=RuntimeError("fixture"),
            ),
        ):
            with self.assertRaises(RuntimeError):
                export_transactions(self.user, {"language": "en", "expected_count": 2101})
        self.assertTrue(temporary.closed)


@skipUnless(connection.vendor == "postgresql", "Requires disposable PostgreSQL test database")
class TransactionExportConcurrencyTests(TransactionTestCase):
    simultaneously = race_tests.LedgerConcurrencyTests.simultaneously

    def setUp(self) -> None:
        race_tests.LedgerConcurrencyTests.setUp(self)

    def test_export_is_complete_or_requires_count_review_during_competing_write(self) -> None:
        documents = []

        def export() -> None:
            document = export_transactions(self.user, {"language": "en", "expected_count": 0})
            try:
                documents.append(document.read().decode("utf-8-sig"))
            finally:
                document.close()

        results = self.simultaneously(
            export, lambda: save_transaction(self.user, {**self.values, "request_id": uuid4()})
        )
        self.assertTrue(results[1])
        if results[0]:
            self.assertEqual(len(list(csv.reader(StringIO(documents[0])))), 1)
        else:
            with self.assertRaises(ExportCountChanged):
                export_transactions(self.user, {"language": "en", "expected_count": 0})
