from datetime import date
from decimal import Decimal
from unittest import skipUnless
from uuid import uuid4

from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from rest_framework.response import Response

from identity.models import User

from . import test_transactions as ledger_tests
from .models import Account, MoneyTransaction, OpeningBalance
from .serializers import TransactionSerializer
from .services import accounts_for, archive_account, save_account


class TransferTests(TestCase):
    post = ledger_tests.TransactionTests.post
    create = ledger_tests.TransactionTests.create
    expense = ledger_tests.TransactionTests.expense
    balance = ledger_tests.TransactionTests.balance

    def setUp(self) -> None:
        ledger_tests.TransactionTests.setUp(self)
        self.target = self.account_for("Savings")
        self.third = self.account_for("Reserve")

    def account_for(self, name: str, owner: User | None = None) -> Account:
        account = save_account(
            owner or self.user,
            {
                "name": name,
                "group": None,
                "opening_iqd": Decimal("0"),
                "opening_usd": Decimal("0"),
            },
        )
        OpeningBalance.objects.filter(account=account).update(effective_date=date(2026, 1, 1))
        return account

    def transfer(self, **overrides: object) -> Response:
        return self.create(
            **{
                "kind": "transfer",
                "destination": self.target.pk,
                "amount": "60.25",
                **overrides,
            }
        )

    def update(self, entry: dict, **overrides: object) -> Response:
        return ledger_tests.TransactionTests.update(
            self,
            entry,
            **{
                "destination": entry["destination"],
                **overrides,
            },
        )

    def balances(self) -> dict:
        return {
            account.pk: (account.balance_iqd, account.balance_usd)
            for account in accounts_for(self.user)
        }

    def test_one_record_moves_exact_money_and_excludes_income_expense(self) -> None:
        response = self.transfer()
        self.assertEqual(response.status_code, 201)
        entry = response.json()
        self.assertEqual(entry["destination_name"], "Savings")
        self.assertEqual(entry["destination_opening_date"], "2026-01-01")
        self.assertEqual(MoneyTransaction.objects.count(), 1)
        self.assertEqual(self.balances()[self.account.pk], (Decimal("39.75"), Decimal("20")))
        self.assertEqual(self.balances()[self.target.pk], (Decimal("60.25"), Decimal("0")))
        for account in (self.account, self.target):
            self.assertEqual(self.client.get(self.root, {"account": account.pk}).json()["count"], 1)
        for kind in ("income", "expense"):
            self.assertEqual(self.client.get(self.root, {"kind": kind}).json()["count"], 0)
        self.assertEqual(self.client.get(self.root, {"kind": "transfer"}).json()["count"], 1)

    @skipUnless(connection.vendor == "postgresql", "Exact decimal arithmetic requires PostgreSQL")
    def test_usd_does_not_touch_iqd(self) -> None:
        self.assertEqual(self.transfer(currency="USD", amount="19.99").status_code, 201)
        self.assertEqual(self.balances()[self.account.pk], (Decimal("100"), Decimal("0.01")))
        self.assertEqual(self.balances()[self.target.pk], (Decimal("0"), Decimal("19.99")))

    def test_rejects_missing_same_foreign_archived_destination(self) -> None:
        foreign = self.account_for("Foreign", self.other)
        archive_account(self.user, self.third.pk, True)
        for destination in (None, self.account.pk, foreign.pk, self.third.pk, 999999):
            with self.subTest(destination=destination):
                self.assertEqual(self.transfer(destination=destination).status_code, 400)
        self.assertEqual(MoneyTransaction.objects.count(), 0)

    def test_destination_only_on_transfers_and_no_category(self) -> None:
        self.assertEqual(self.create(destination=self.target.pk).status_code, 400)
        self.assertEqual(self.expense(destination=self.target.pk).status_code, 400)
        self.assertEqual(self.transfer(category=self.category.pk).status_code, 400)
        for values in (
            {"amount": "0"},
            {"amount": "-1"},
            {"amount": "1.001"},
            {"amount": 1},
            {"currency": "EUR"},
            {"destination_currency": "USD"},
        ):
            self.assertEqual(self.transfer(**values).status_code, 400)

    def test_date_must_follow_both_openings_and_not_future(self) -> None:
        OpeningBalance.objects.filter(account=self.target).update(effective_date=date(2026, 1, 5))
        self.assertEqual(self.transfer().status_code, 400)
        self.assertEqual(self.transfer(date="2026-01-05").status_code, 201)
        self.assertEqual(self.transfer(date="2099-01-01").status_code, 400)

    @skipUnless(connection.vendor == "postgresql", "Full precision requires PostgreSQL")
    def test_failed_source_and_destination_overflow_rollback(self) -> None:
        self.assertEqual(self.transfer(amount="100.01").status_code, 400)
        OpeningBalance.objects.filter(account=self.target).update(
            iqd=Decimal("9999999999999999.99")
        )
        self.assertEqual(self.transfer(amount="0.01").status_code, 400)
        self.assertEqual(self.balance(), Decimal("100"))
        self.assertEqual(MoneyTransaction.objects.count(), 0)

    def test_backdate_checks_source_later_spending(self) -> None:
        self.expense(amount="80.00", date="2026-01-05")
        self.assertEqual(self.transfer(amount="20.01").status_code, 400)
        self.assertEqual(self.balances()[self.target.pk][0], 0)

    def test_incoming_transfer_available_for_same_day_expense(self) -> None:
        self.transfer()
        self.assertEqual(self.expense(account=self.target.pk, amount="60.25").status_code, 201)
        self.assertEqual(self.balances()[self.target.pk][0], 0)

    def test_spent_destination_blocks_delete_reduction_move_currency_and_date(self) -> None:
        entry = self.transfer().json()
        self.expense(account=self.target.pk, amount="60.25", date="2026-01-03")
        before = self.balances()
        for values in (
            {"amount": "60.24"},
            {"destination": self.third.pk},
            {"currency": "USD", "amount": "1.00"},
            {"date": "2026-01-04"},
            {"kind": "expense", "destination": None, "category": self.category.pk},
        ):
            self.assertEqual(self.update(entry, **values).status_code, 400)
            self.assertEqual(self.balances(), before)
        self.assertEqual(self.post(f"{entry['id']}/delete/", {"version": 1}).status_code, 400)
        self.assertEqual(MoneyTransaction.objects.get(pk=entry["id"]).version, 1)

    def test_edit_source_destination_currency_then_delete_updates_all_sides(self) -> None:
        entry = self.transfer(amount="10.00").json()
        self.create(account=self.third.pk, currency="USD", amount="30.00")
        response = self.update(
            entry,
            account=self.third.pk,
            destination=self.account.pk,
            currency="USD",
            amount="25.15",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.balances()[self.target.pk], (Decimal("0"), Decimal("0")))
        self.assertEqual(self.balances()[self.account.pk], (Decimal("100"), Decimal("45.15")))
        self.assertEqual(self.balances()[self.third.pk][1], Decimal("4.85"))
        self.assertEqual(self.post(f"{entry['id']}/delete/", {"version": 2}).status_code, 204)
        self.assertEqual(self.balances()[self.account.pk][1], Decimal("20"))
        self.assertEqual(self.balances()[self.third.pk][1], Decimal("30"))

    def test_archived_references_can_remain_but_not_be_new_assignments(self) -> None:
        entry = self.transfer().json()
        archive_account(self.user, self.account.pk, True)
        archive_account(self.user, self.target.pk, True)
        response = self.update(entry, note="Correction")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["destination_archived"])
        self.assertEqual(self.transfer().status_code, 400)
        self.assertEqual(self.post(f"{entry['id']}/delete/", {"version": 2}).status_code, 204)

    def test_retry_payload_stale_version_and_tombstone(self) -> None:
        request_id = str(uuid4())
        entry = self.transfer(request_id=request_id).json()
        self.assertEqual(self.transfer(request_id=request_id).status_code, 200)
        self.assertEqual(
            self.transfer(request_id=request_id, destination=self.third.pk).status_code, 409
        )
        self.assertEqual(self.update(entry, note="Changed").status_code, 200)
        self.assertEqual(self.update(entry).status_code, 409)
        self.assertEqual(self.post(f"{entry['id']}/delete/", {"version": 1}).status_code, 409)
        self.assertEqual(self.post(f"{entry['id']}/delete/", {"version": 2}).status_code, 204)
        self.assertEqual(self.transfer(request_id=request_id).status_code, 409)
        self.assertEqual(self.client.get(self.root).json()["count"], 0)

    def test_other_user_cannot_read_edit_delete_or_filter_transfer(self) -> None:
        entry = self.transfer().json()
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(f"{self.root}{entry['id']}/").status_code, 404)
        self.assertEqual(self.update(entry).status_code, 404)
        self.assertEqual(self.post(f"{entry['id']}/delete/", {"version": 1}).status_code, 404)
        self.assertEqual(self.client.get(self.root, {"account": self.target.pk}).json()["count"], 0)

    def test_list_query_bound_and_model_constraints(self) -> None:
        for _ in range(3):
            self.transfer(amount="0.01")
        from .ledger import transactions_for

        with self.assertNumQueries(1):
            self.assertEqual(
                len(TransactionSerializer(transactions_for(self.user), many=True).data), 3
            )
        entry = MoneyTransaction.objects.first()
        for changes in ({"destination": self.account}, {"destination": None}, {"kind": "income"}):
            with self.assertRaises(IntegrityError), transaction.atomic():
                MoneyTransaction.objects.filter(pk=entry.pk).update(**changes)
