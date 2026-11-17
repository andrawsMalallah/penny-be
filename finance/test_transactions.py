from datetime import UTC, date, datetime
from decimal import Decimal
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.apps import apps
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from rest_framework.test import APIClient

from identity.models import User

from .categories import initialize_categories
from .ledger import opening_date
from .models import ExpenseCategory, MoneyTransaction, OpeningBalance
from .serializers import TransactionSerializer
from .services import accounts_for, archive_account, save_account


class TransactionTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(
            "ledger@example.com", email_verified=True, language="en", timezone="Asia/Baghdad"
        )
        self.other = User.objects.create_user(
            "ledger-other@example.com", email_verified=True, language="en"
        )
        self.account = save_account(
            self.user,
            {
                "name": "Wallet",
                "group": None,
                "opening_iqd": Decimal("100.00"),
                "opening_usd": Decimal("20.00"),
            },
        )
        OpeningBalance.objects.filter(account=self.account).update(effective_date=date(2026, 1, 1))
        initialize_categories(self.user)
        self.category = ExpenseCategory.objects.filter(user=self.user).first()
        self.client = APIClient(enforce_csrf_checks=True)
        self.client.force_login(self.user)
        self.root = "/api/v1/transactions/"
        self.values = {
            "account": self.account.pk,
            "category": None,
            "kind": "income",
            "currency": "IQD",
            "amount": "10.00",
            "date": "2026-01-02",
            "note": "Salary",
            "timezone": "Asia/Baghdad",
        }

    def post(self, suffix: str, values: dict):
        token = self.client.get("/api/v1/auth/csrf/").json()["csrf_token"]
        return self.client.post(self.root + suffix, values, format="json", HTTP_X_CSRFTOKEN=token)

    def create(self, **overrides: object):
        return self.post("", {**self.values, "request_id": str(uuid4()), **overrides})

    def expense(self, **overrides: object):
        return self.create(**{"kind": "expense", "category": self.category.pk, **overrides})

    def update(self, entry: dict, **overrides: object):
        values = {
            field: entry[field]
            for field in (
                "account",
                "category",
                "kind",
                "currency",
                "amount",
                "date",
                "note",
                "version",
            )
        }
        return self.post(f"{entry['id']}/", {**values, "timezone": "Asia/Baghdad", **overrides})

    def balance(self, currency: str = "iqd") -> Decimal:
        return getattr(accounts_for(self.user).get(pk=self.account.pk), f"balance_{currency}")

    def test_income_expense_fresh_balances_and_openings_not_income(self) -> None:
        self.assertEqual(self.client.get(self.root).json()["count"], 0)
        income = self.create(amount="25.25")
        self.assertEqual(income.status_code, 201)
        self.assertEqual(income.json()["amount"], "25.25")
        self.assertEqual(income.json()["opening_date"], "2026-01-01")
        self.assertEqual(self.expense(amount="5.15").status_code, 201)
        self.assertEqual(self.balance(), Decimal("120.10"))
        self.assertEqual(self.balance("usd"), Decimal("20.00"))
        account = self.client.get(f"/api/v1/accounts/{self.account.pk}/").json()
        self.assertEqual(account["balance_iqd"], "120.10")
        self.assertEqual(account["opening"]["iqd"], "100.00")
        self.assertIn("no-store", income["Cache-Control"])
        self.assertNotIn("request_id", income.json())
        self.assertNotIn("user", income.json())

    def test_overspending_never_uses_other_currency_or_future_income(self) -> None:
        self.create(date="2026-01-04", amount="500.00")
        self.assertEqual(self.expense(date="2026-01-03", amount="100.01").status_code, 400)
        self.assertEqual(self.expense(currency="USD", amount="20.01").status_code, 400)
        self.assertEqual(MoneyTransaction.objects.count(), 1)

    def test_backdating_rechecks_all_later_balances_and_rolls_back(self) -> None:
        self.assertEqual(self.expense(date="2026-01-05", amount="90.00").status_code, 201)
        self.assertEqual(self.expense(date="2026-01-02", amount="11.00").status_code, 400)
        self.assertEqual(self.balance(), Decimal("10"))
        self.assertEqual(MoneyTransaction.objects.count(), 1)

    def test_income_precedes_expenses_on_same_day_regardless_of_entry_order(self) -> None:
        self.expense(amount="100.00")
        self.create(amount="40.00")
        self.assertEqual(self.expense(amount="40.00").status_code, 201)
        self.assertEqual(self.balance(), Decimal("0"))

    def test_dates_reject_before_opening_future_and_invalid_timezones(self) -> None:
        self.assertEqual(self.create(date="2025-12-31").status_code, 400)
        with patch(
            "finance.ledger.timezone.now", return_value=datetime(2026, 1, 3, 22, tzinfo=UTC)
        ):
            self.assertEqual(
                self.create(date="2026-01-04", timezone="Asia/Baghdad").status_code, 201
            )
            self.assertEqual(
                self.create(date="2026-01-04", timezone="America/New_York").status_code, 400
            )
        self.assertEqual(self.create(timezone="Invalid/Zone").status_code, 400)
        self.assertEqual(self.create(date="2026-02-30").status_code, 400)

    def test_fixed_dates_survive_user_timezone_changes(self) -> None:
        entry = self.create().json()
        self.user.timezone = "Pacific/Honolulu"
        self.user.save(update_fields=["timezone"])
        self.assertEqual(
            self.client.get(self.root + f"{entry['id']}/").json()["date"], "2026-01-02"
        )
        self.account.opening.refresh_from_db()
        self.assertEqual(self.account.opening.effective_date, date(2026, 1, 1))

    def test_correction_checks_old_account_currency_and_date(self) -> None:
        income = self.create(amount="50.00").json()
        self.expense(date="2026-01-04", amount="140.00")
        for overrides in [{"amount": "39.99"}, {"date": "2026-01-05"}, {"currency": "USD"}]:
            self.assertEqual(self.update(income, **overrides).status_code, 400)
        another = save_account(
            self.user,
            {
                "name": "Bank",
                "group": None,
                "opening_iqd": Decimal("0"),
                "opening_usd": Decimal("0"),
            },
        )
        OpeningBalance.objects.filter(account=another).update(effective_date=date(2026, 1, 1))
        self.assertEqual(self.update(income, account=another.pk).status_code, 400)
        self.assertEqual(self.balance(), Decimal("10"))
        self.assertEqual(MoneyTransaction.objects.get(pk=income["id"]).version, 1)

    def test_valid_correction_recomputes_both_accounts(self) -> None:
        income = self.create(amount="10.00").json()
        another = save_account(
            self.user,
            {
                "name": "Bank",
                "group": None,
                "opening_iqd": Decimal("0"),
                "opening_usd": Decimal("0"),
            },
        )
        OpeningBalance.objects.filter(account=another).update(effective_date=date(2026, 1, 1))
        response = self.update(income, account=another.pk, currency="USD", amount="5.25")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["version"], 2)
        self.assertEqual(self.balance(), Decimal("100"))
        self.assertEqual(accounts_for(self.user).get(pk=another.pk).balance_usd, Decimal("5.25"))
        self.assertEqual(self.update(income, amount="2.00").status_code, 409)

    def test_delete_income_refuses_breaking_later_expenses_then_allows_valid_deletion(self) -> None:
        income = self.create(amount="100.00").json()
        expense = self.expense(date="2026-01-03", amount="150.00").json()
        self.assertEqual(self.post(f"{income['id']}/delete/", {"version": 1}).status_code, 400)
        self.assertEqual(self.post(f"{expense['id']}/delete/", {"version": 1}).status_code, 204)
        self.assertEqual(self.post(f"{income['id']}/delete/", {"version": 1}).status_code, 204)
        self.assertEqual(self.balance(), Decimal("100"))
        self.assertEqual(self.client.get(self.root).json()["count"], 0)
        self.assertEqual(self.client.get(self.root + f"{income['id']}/").status_code, 404)
        self.assertEqual(MoneyTransaction.objects.filter(deleted_at__isnull=False).count(), 2)

    def test_idempotent_create_rejects_reused_payload_and_deleted_replay(self) -> None:
        request_id = str(uuid4())
        entry = self.create(request_id=request_id).json()
        self.assertEqual(self.create(request_id=request_id).status_code, 200)
        self.assertEqual(self.balance(), Decimal("110"))
        self.assertEqual(self.create(request_id=request_id, amount="11.00").status_code, 409)
        self.post(f"{entry['id']}/delete/", {"version": 1})
        self.assertEqual(self.create(request_id=request_id).status_code, 409)

    def test_archived_references_remain_readable_and_correctable_but_not_assignable(self) -> None:
        entry = self.expense(amount="5.00").json()
        archive_account(self.user, self.account.pk, True)
        self.category.archived = True
        self.category.save(update_fields=["archived"])
        self.assertEqual(self.create().status_code, 400)
        self.assertEqual(self.expense().status_code, 400)
        changed = self.update(entry, note="Corrected note")
        self.assertEqual(changed.status_code, 200)
        self.assertTrue(changed.json()["category_archived"])
        self.assertTrue(changed.json()["account_archived"])
        self.assertEqual(self.post(f"{entry['id']}/delete/", {"version": 1}).status_code, 409)
        self.assertEqual(self.post(f"{entry['id']}/delete/", {"version": 2}).status_code, 204)

    def test_foreign_accounts_categories_and_transaction_boundaries(self) -> None:
        foreign = save_account(
            self.other,
            {
                "name": "Foreign",
                "group": None,
                "opening_iqd": Decimal("0"),
                "opening_usd": Decimal("0"),
            },
        )
        initialize_categories(self.other)
        category = ExpenseCategory.objects.filter(user=self.other).first()
        self.assertEqual(self.create(account=foreign.pk).status_code, 400)
        self.assertEqual(self.expense(category=category.pk).status_code, 400)
        entry = self.create().json()
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.root).json()["count"], 0)
        self.assertEqual(self.client.get(self.root + f"{entry['id']}/").status_code, 404)
        self.assertEqual(self.update(entry).status_code, 404)
        self.assertEqual(self.post(f"{entry['id']}/delete/", {"version": 1}).status_code, 404)

    def test_guest_unverified_and_csrf(self) -> None:
        entry = self.create().json()
        for suffix in ["", f"{entry['id']}/", f"{entry['id']}/delete/"]:
            self.assertEqual(
                self.client.post(self.root + suffix, self.values, format="json").status_code, 403
            )
        self.client.logout()
        self.assertEqual(self.client.get(self.root).status_code, 403)
        self.assertEqual(self.create().status_code, 403)
        self.other.email_verified = False
        self.other.save(update_fields=["email_verified"])
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.root).status_code, 403)
        self.assertEqual(self.create().status_code, 403)

    def test_strict_positive_exact_amount_payload_validation(self) -> None:
        for overrides in [
            {"amount": value}
            for value in ["0", "-1", "0.001", "NaN", "Infinity", 1.25, True, "10000000000000000"]
        ] + [
            {"currency": "EUR"},
            {"user": self.other.pk},
            {"balance_iqd": "999"},
            {"note": "x" * 501},
            {"kind": "transfer"},
            {"category": self.category.pk},
            {"request_id": "invalid"},
        ]:
            with self.subTest(overrides=overrides):
                self.assertEqual(self.create(**overrides).status_code, 400)
        self.assertEqual(self.expense(category=None).status_code, 400)
        self.assertFalse(MoneyTransaction.objects.exists())

    def test_filter_pagination_and_eager_loading(self) -> None:
        for index in range(32):
            self.assertEqual(self.create(note=f"Income {index}").status_code, 201)
        self.expense(currency="USD", amount="1.00", date="2026-01-03")
        self.assertEqual(len(self.client.get(self.root).json()["results"]), 30)
        self.assertEqual(len(self.client.get(self.root + "?page=2").json()["results"]), 3)
        filtered = self.client.get(
            self.root
            + f"?account={self.account.pk}&kind=expense&currency=USD"
            + "&start=2026-01-03&end=2026-01-03"
        ).json()
        self.assertEqual(filtered["count"], 1)
        for query in [
            "kind=bad",
            "currency=EUR",
            "start=bad",
            "start=2026-01-04&end=2026-01-01",
            "user=1",
        ]:
            self.assertEqual(self.client.get(self.root + "?" + query).status_code, 400)
        from .ledger import transactions_for

        with self.assertNumQueries(1):
            self.assertEqual(
                len(TransactionSerializer(transactions_for(self.user)[:30], many=True).data), 30
            )
        from .serializers import AccountSerializer

        with self.assertNumQueries(1):
            self.assertEqual(len(AccountSerializer(accounts_for(self.user), many=True).data), 1)

    def test_database_constraints_reject_invalid_transaction(self) -> None:
        entry = self.create().json()
        for values in [
            {"amount": Decimal("0")},
            {"kind": "wrong"},
            {"currency": "EUR"},
            {"kind": "expense", "category": None},
        ]:
            with self.assertRaises(IntegrityError), transaction.atomic():
                MoneyTransaction.objects.filter(pk=entry["id"]).update(**values)

    def test_opening_date_backfill_uses_saved_timezone_and_does_not_change_money(self) -> None:
        instant = datetime(2026, 1, 1, 22, tzinfo=UTC)
        OpeningBalance.objects.filter(account=self.account).update(recorded_at=instant)
        migration = import_module(
            "finance.migrations.0003_openingbalance_effective_date_moneytransaction"
        )
        migration.fix_opening_dates(apps, SimpleNamespace(connection=connection))
        opening = OpeningBalance.objects.get(account=self.account)
        self.assertEqual(opening.effective_date, date(2026, 1, 2))
        self.assertEqual(opening.iqd, Decimal("100"))
        self.assertEqual(opening_date(instant, "America/New_York"), date(2026, 1, 1))

    def test_maximum_balance_precision_and_overflow_rollback(self) -> None:
        if connection.vendor != "postgresql":
            self.skipTest("Full precision requires PostgreSQL")
        OpeningBalance.objects.filter(account=self.account).update(
            iqd=Decimal("9999999999999999.98")
        )
        self.assertEqual(self.create(amount="0.01").status_code, 201)
        self.assertEqual(
            self.client.get(f"/api/v1/accounts/{self.account.pk}/").json()["balance_iqd"],
            "9999999999999999.99",
        )
        self.assertEqual(self.create(amount="0.01").status_code, 400)
        self.assertEqual(self.expense(amount="9999999999999999.99").status_code, 201)
        self.assertEqual(self.balance(), Decimal("0"))

    def test_request_id_generation_requires_verified_user_and_never_writes(self) -> None:
        from uuid import UUID

        first = self.client.get(self.root + "request-id/")
        second = self.client.get(self.root + "request-id/")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(UUID(first.json()["request_id"]).version, 4)
        self.assertNotEqual(first.json(), second.json())
        self.assertFalse(MoneyTransaction.objects.exists())
        self.assertIn("no-store", first["Cache-Control"])
        self.client.logout()
        self.assertEqual(self.client.get(self.root + "request-id/").status_code, 403)

    def test_all_account_picker_includes_archived_but_not_foreign_accounts(self) -> None:
        archive_account(self.user, self.account.pk, True)
        save_account(
            self.other,
            {
                "name": "Foreign picker",
                "group": None,
                "opening_iqd": Decimal("0"),
                "opening_usd": Decimal("0"),
            },
        )
        self.assertEqual(self.client.get("/api/v1/accounts/?archived=all").json()["count"], 1)
        self.assertEqual(self.client.get("/api/v1/accounts/").json()["count"], 0)

    def test_moving_expense_checks_new_account_and_currency(self) -> None:
        entry = self.expense(amount="30.00").json()
        self.assertEqual(self.update(entry, currency="USD").status_code, 400)
        self.assertEqual(self.balance(), Decimal("70"))
        response = self.update(entry, currency="USD", amount="15.00")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.balance(), Decimal("100"))
        self.assertEqual(self.balance("usd"), Decimal("5"))
