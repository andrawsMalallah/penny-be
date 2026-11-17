from datetime import date
from decimal import Decimal
from unittest import skipUnless
from uuid import uuid4

from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from rest_framework.response import Response

from identity.models import UserSettings

from . import test_transactions as ledger_tests
from .exchanges import exchange_amount
from .models import MoneyTransaction, OpeningBalance
from .services import accounts_for, archive_account, save_account


class ExchangeTests(TestCase):
    post = ledger_tests.TransactionTests.post
    create = ledger_tests.TransactionTests.create
    expense = ledger_tests.TransactionTests.expense
    balance = ledger_tests.TransactionTests.balance

    def setUp(self) -> None:
        ledger_tests.TransactionTests.setUp(self)
        UserSettings.objects.create(user=self.user, exchange_rate=Decimal("1310"))

    def exchange(self, **overrides: object) -> Response:
        return self.create(
            **{
                "kind": "exchange",
                "currency": "USD",
                "amount": "1.00",
                "exchange_rate": "1310",
                "received_amount": "1310.00",
                **overrides,
            }
        )

    def update(self, entry: dict, **overrides: object) -> Response:
        return ledger_tests.TransactionTests.update(
            self,
            entry,
            **{
                "exchange_rate": entry["exchange_rate"],
                "received_amount": entry["received_amount"],
                **overrides,
            },
        )

    def test_both_directions_exact_balances_and_one_non_income_record(self) -> None:
        entry = self.exchange()
        self.assertEqual(entry.status_code, 201)
        self.assertEqual(entry.json()["exchange_rate"], "1310.000000")
        self.assertEqual(self.balance(), Decimal("1410"))
        self.assertEqual(self.balance("usd"), Decimal("19"))
        self.assertEqual(
            self.exchange(currency="IQD", amount="1310.00", received_amount="1.00").status_code, 201
        )
        self.assertEqual(self.balance(), Decimal("100"))
        self.assertEqual(self.balance("usd"), Decimal("20"))
        for currency in ("IQD", "USD"):
            self.assertEqual(self.client.get(self.root, {"currency": currency}).json()["count"], 2)
        for kind in ("income", "expense", "transfer"):
            self.assertEqual(self.client.get(self.root, {"kind": kind}).json()["count"], 0)

    def test_halfway_rounds_up_and_preview_must_match(self) -> None:
        vectors = [
            ("0.01", "USD", "1.5", "0.02"),
            ("1", "IQD", "200", "0.01"),
            ("1", "IQD", "3", "0.33"),
            ("1", "USD", "1.004999", "1.00"),
            ("1", "USD", "1.005", "1.01"),
        ]
        for amount, currency, rate, expected in vectors:
            self.assertEqual(
                exchange_amount(Decimal(amount), currency, Decimal(rate)), Decimal(expected)
            )
        self.assertEqual(
            self.exchange(amount="0.01", exchange_rate="1.5", received_amount="0.02").status_code,
            201,
        )
        self.assertEqual(
            self.exchange(amount="0.01", exchange_rate="1.5", received_amount="0.01").status_code,
            400,
        )

    def test_requires_saved_rate_but_override_does_not_modify_settings(self) -> None:
        UserSettings.objects.filter(user=self.user).update(exchange_rate=None)
        self.assertEqual(self.exchange().status_code, 400)
        UserSettings.objects.filter(user=self.user).update(exchange_rate=Decimal("1310"))
        entry = self.exchange(exchange_rate="1400", received_amount="1400.00").json()
        self.assertEqual(UserSettings.objects.get(user=self.user).exchange_rate, Decimal("1310"))
        UserSettings.objects.filter(user=self.user).update(exchange_rate=None)
        self.assertEqual(self.update(entry, note="Historical correction").status_code, 200)
        self.assertEqual(
            self.client.get(f"{self.root}{entry['id']}/").json()["exchange_rate"], "1400.000000"
        )

    def test_strict_fields_rate_precision_money_and_zero_rounding(self) -> None:
        invalid = [
            {"exchange_rate": None},
            {"exchange_rate": 1310},
            {"exchange_rate": "0"},
            {"exchange_rate": "-1"},
            {"exchange_rate": "1.1234567"},
            {"exchange_rate": "1000000000000"},
            {"exchange_rate": "NaN"},
            {"received_amount": None},
            {"received_amount": 1310},
            {"received_amount": "0"},
            {"received_amount": "1.001"},
            {"amount": "0"},
            {"amount": "-1"},
            {"amount": "1.001"},
            {"category": self.category.pk},
            {"destination": self.account.pk},
            {
                "amount": "0.01",
                "currency": "IQD",
                "exchange_rate": "1310",
                "received_amount": "0.01",
            },
        ]
        for values in invalid:
            with self.subTest(values=values):
                self.assertEqual(self.exchange(**values).status_code, 400)
        self.assertEqual(self.create(exchange_rate="1", received_amount="1").status_code, 400)
        self.assertEqual(MoneyTransaction.objects.count(), 0)

    @skipUnless(connection.vendor == "postgresql", "Full precision requires PostgreSQL")
    def test_capacity_and_overdraw_rollback_both_currencies(self) -> None:
        self.assertEqual(self.exchange(amount="20.01", received_amount="26213.10").status_code, 400)
        OpeningBalance.objects.filter(account=self.account).update(
            iqd=Decimal("9999999999999999.99")
        )
        self.assertEqual(self.exchange().status_code, 400)
        self.assertEqual(self.balance("usd"), Decimal("20"))
        self.assertEqual(MoneyTransaction.objects.count(), 0)
        self.assertEqual(
            exchange_amount(Decimal("9999999999999999.99"), "USD", Decimal("1")),
            Decimal("9999999999999999.99"),
        )

    def test_backdate_and_delete_recheck_spent_received_currency(self) -> None:
        entry = self.exchange().json()
        self.expense(amount="1400", date="2026-01-03")
        for changes in (
            {"amount": "0.50", "received_amount": "655.00"},
            {"exchange_rate": "1000", "received_amount": "1000.00"},
            {"date": "2026-01-04"},
            {"kind": "income", "exchange_rate": None, "received_amount": None},
        ):
            self.assertEqual(self.update(entry, **changes).status_code, 400)
        self.assertEqual(self.post(f"{entry['id']}/delete/", {"version": 1}).status_code, 400)
        self.assertEqual(self.balance(), Decimal("10"))
        self.assertEqual(self.balance("usd"), Decimal("19"))

    def test_source_later_spend_and_opening_future_dates(self) -> None:
        self.expense(currency="USD", amount="20", date="2026-01-03")
        for changes in ({}, {"date": "2025-12-31"}, {"date": "2099-01-01"}):
            self.assertEqual(self.exchange(**changes).status_code, 400)

    def test_edit_account_direction_rate_and_delete_all_sides(self) -> None:
        target = save_account(
            self.user,
            {
                "name": "Other",
                "group": None,
                "opening_iqd": Decimal("1000"),
                "opening_usd": Decimal("0"),
            },
        )
        OpeningBalance.objects.filter(account=target).update(effective_date=date(2026, 1, 1))
        entry = self.exchange().json()
        response = self.update(
            entry,
            account=target.pk,
            currency="IQD",
            amount="1000.00",
            exchange_rate="1000",
            received_amount="1.00",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.balance(), Decimal("100"))
        self.assertEqual(self.balance("usd"), Decimal("20"))
        updated = accounts_for(self.user).get(pk=target.pk)
        self.assertEqual((updated.balance_iqd, updated.balance_usd), (Decimal("0"), Decimal("1")))
        self.assertEqual(self.post(f"{entry['id']}/delete/", {"version": 2}).status_code, 204)
        self.assertEqual(accounts_for(self.user).get(pk=target.pk).balance_iqd, Decimal("1000"))

    def test_archived_account_can_be_retained_and_settings_never_reprice(self) -> None:
        entry = self.exchange().json()
        archive_account(self.user, self.account.pk, True)
        UserSettings.objects.filter(user=self.user).update(exchange_rate=Decimal("999"))
        self.assertEqual(self.update(entry, note="Correction").status_code, 200)
        self.assertEqual(self.exchange().status_code, 400)
        self.assertEqual(self.balance(), Decimal("1410"))

    def test_explicit_retry_stale_update_and_tombstone(self) -> None:
        request_id = str(uuid4())
        entry = self.exchange(request_id=request_id).json()
        self.assertEqual(self.exchange(request_id=request_id).status_code, 200)
        self.assertEqual(
            self.exchange(
                request_id=request_id, exchange_rate="1400", received_amount="1400"
            ).status_code,
            409,
        )
        self.assertEqual(self.update(entry, note="Updated").status_code, 200)
        self.assertEqual(self.update(entry).status_code, 409)
        self.assertEqual(self.post(f"{entry['id']}/delete/", {"version": 1}).status_code, 409)
        self.assertEqual(self.post(f"{entry['id']}/delete/", {"version": 2}).status_code, 204)
        self.assertEqual(self.exchange(request_id=request_id).status_code, 409)

    def test_privacy_and_database_exchange_constraints(self) -> None:
        entry = self.exchange().json()
        for values in (
            {"exchange_rate": None},
            {"received_amount": None},
            {"exchange_rate": Decimal("0")},
            {"received_amount": Decimal("0")},
            {"kind": "income"},
        ):
            with self.assertRaises(IntegrityError), transaction.atomic():
                MoneyTransaction.objects.filter(pk=entry["id"]).update(**values)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(f"{self.root}{entry['id']}/").status_code, 404)
        self.assertEqual(self.update(entry).status_code, 404)
        self.assertEqual(self.post(f"{entry['id']}/delete/", {"version": 1}).status_code, 404)
        self.assertEqual(self.client.get(self.root).json()["count"], 0)
