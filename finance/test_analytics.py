from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from identity.models import User, UserSettings

from .analytics import report_period
from .models import (
    Account,
    AccountGroup,
    Debt,
    DebtPerson,
    ExpenseCategory,
    MoneyTransaction,
    OpeningBalance,
)
from .services import save_account


class AnalyticsTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user("reports@example.com", email_verified=True)
        self.other = User.objects.create_user("other-reports@example.com", email_verified=True)
        self.client = APIClient()
        self.client.force_login(self.user)
        self.group = AccountGroup.objects.create(user=self.user, name="Savings")
        self.account = save_account(
            self.user,
            {
                "name": "Wallet",
                "group": self.group.pk,
                "opening_iqd": Decimal("1000"),
                "opening_usd": Decimal("100"),
            },
        )
        OpeningBalance.objects.filter(account=self.account).update(effective_date=date(2025, 1, 1))
        self.category = ExpenseCategory.objects.create(
            user=self.user, name_en="Food", name_ar="طعام"
        )
        self.clock = patch(
            "finance.analytics.timezone.now", return_value=datetime(2026, 11, 18, 12, tzinfo=UTC)
        )
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def get(self, section: str = "", **values: str):
        return self.client.get(
            f"/api/v1/analytics/{section}", {"timezone": "Asia/Baghdad", **values}
        )

    def entry(self, kind: str, amount: str, currency: str = "IQD", **values: object):
        return MoneyTransaction.objects.create(
            user=self.user,
            account=self.account,
            kind=kind,
            currency=currency,
            amount=Decimal(amount),
            category=self.category if kind == "expense" else None,
            date=date(2026, 11, 10),
            request_id=uuid4(),
            creation_payload={},
            **values,
        )

    def debt(self, direction: str, currency: str, amount: str, person: DebtPerson | None = None):
        person = person or DebtPerson.objects.create(user=self.user, name=str(uuid4()))
        return Debt.objects.create(
            user=self.user,
            person=person,
            direction=direction,
            origin="existing",
            account=None,
            currency=currency,
            amount=Decimal(amount),
            date=date(2020, 1, 1),
            request_id=uuid4(),
            creation_payload={},
        )

    def test_empty_report_reads_never_create_records(self) -> None:
        self.client.force_login(self.other)
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["current"]["USD"],
            {"held": "0.00", "owed_to_me": "0.00", "i_owe": "0.00", "net": "0.00"},
        )
        self.assertEqual(len(response.json()["trends"]), 6)
        self.assertFalse(Account.objects.filter(user=self.other).exists())
        self.assertFalse(UserSettings.objects.exists())
        self.assertIn("no-store", response["Cache-Control"])

    def test_income_spending_and_changes_exclude_deleted_entries(self) -> None:
        self.entry("income", "200.25")
        self.entry("expense", "50.10")
        previous = self.entry("income", "100")
        previous.date = date(2026, 10, 10)
        previous.save()
        deleted = self.entry("expense", "800")
        deleted.deleted_at = datetime(2026, 11, 11, tzinfo=UTC)
        deleted.save()
        data = self.get().json()
        self.assertEqual(
            data["activity"]["IQD"],
            {
                "income": "200.25",
                "expense": "50.10",
                "difference": "150.15",
                "previous_income": "100.00",
                "previous_expense": "0.00",
                "income_change": "100.25",
                "expense_change": "50.10",
            },
        )
        self.assertEqual(data["current"]["IQD"]["held"], "1250.15")

    def test_transfers_exchanges_and_debt_cash_do_not_count_as_income_or_expense(self) -> None:
        other_account = save_account(
            self.user,
            {
                "name": "Other",
                "group": None,
                "opening_iqd": Decimal("0"),
                "opening_usd": Decimal("0"),
            },
        )
        self.entry("transfer", "100", destination=other_account)
        self.entry(
            "exchange", "100", exchange_rate=Decimal("1000"), received_amount=Decimal("0.10")
        )
        debt = self.debt("i_owe", "USD", "20")
        self.entry("debt_in", "20", "USD", debt=debt, debt_role="principal")
        self.entry("debt_out", "5", "USD", debt=debt, debt_role="repayment")
        data = self.get().json()
        self.assertEqual(data["activity"]["USD"]["income"], "0.00")
        self.assertEqual(data["activity"]["IQD"]["expense"], "0.00")
        self.assertEqual(data["current"]["IQD"]["held"], "900.00")
        self.assertEqual(
            data["current"]["USD"],
            {"held": "115.10", "owed_to_me": "0.00", "i_owe": "15.00", "net": "100.10"},
        )
        self.assertTrue(all(row["IQD"]["income"] == "0.00" for row in data["trends"]))
        self.assertEqual(self.get("categories/").json()["count"], 0)

    def test_all_archived_and_ungrouped_accounts_are_included_without_page_sums(self) -> None:
        for index in range(31):
            account = save_account(
                self.user,
                {
                    "name": f"Extra {index:02}",
                    "group": None,
                    "opening_iqd": Decimal("1.25"),
                    "opening_usd": Decimal("2.50"),
                },
            )
            Account.objects.filter(pk=account.pk).update(archived=True)
        self.assertEqual(self.get().json()["current"]["IQD"]["held"], "1038.75")
        listing = self.get("accounts/").json()
        self.assertEqual(listing["count"], 32)
        self.assertEqual(len(listing["results"]), 30)
        self.assertEqual(len(self.get("accounts/", page="2").json()["results"]), 2)
        groups = self.get("groups/").json()["results"]
        self.assertEqual(next(row for row in groups if row["group"] is None)["USD"], "77.50")

    def test_categories_group_by_id_with_bilingual_names_and_full_period_total(self) -> None:
        self.entry("expense", "20.25")
        self.entry("expense", "10.75")
        self.entry("expense", "5.50", "USD")
        ExpenseCategory.objects.filter(pk=self.category.pk).update(archived=True)
        rows = self.get("categories/").json()["results"]
        self.assertEqual(
            rows,
            [
                {
                    "category": self.category.pk,
                    "category_name_en": "Food",
                    "category_name_ar": "طعام",
                    "category_archived": True,
                    "IQD": "31.00",
                    "USD": "5.50",
                }
            ],
        )
        activity = self.client.get(
            "/api/v1/transactions/",
            {
                "category": self.category.pk,
                "currency": "USD",
                "kind": "expense",
                "start": "2026-11-01",
                "end": "2026-11-18",
            },
        ).json()
        self.assertEqual(activity["count"], 1)

    def test_outstanding_debts_keep_directions_currencies_and_persons_separate(self) -> None:
        person = DebtPerson.objects.create(user=self.user, name="Rima")
        debt = self.debt("owed_to_me", "IQD", "100", person)
        self.entry("debt_in", "20", debt=debt, debt_role="repayment")
        self.entry("debt_in", "10", debt=debt, debt_role="repayment")
        self.debt("owed_to_me", "IQD", "50", person)
        self.debt("i_owe", "IQD", "70", person)
        self.debt("i_owe", "USD", "200", person)
        removed = self.debt("i_owe", "USD", "999")
        removed.deleted_at = datetime(2026, 11, 10, tzinfo=UTC)
        removed.save()
        data = self.get(period="previous_month").json()
        self.assertEqual(data["current"]["IQD"]["owed_to_me"], "120.00")
        self.assertEqual(data["current"]["USD"]["net"], "-100.00")
        row = self.get("people/").json()["results"][0]
        self.assertEqual(row["owed_to_me_IQD"], "120.00")
        self.assertEqual(row["i_owe_IQD"], "70.00")
        self.assertEqual(row["i_owe_USD"], "200.00")

    def test_custom_period_changes_activity_but_not_current_position_or_trends(self) -> None:
        self.entry("income", "20")
        current = self.get().json()
        historical = self.get(period="custom", start="2020-01-01", end="2020-01-31").json()
        self.assertEqual(current["current"], historical["current"])
        self.assertEqual(current["trends"], historical["trends"])
        self.assertEqual(historical["activity"]["IQD"]["income"], "0.00")
        self.assertEqual(historical["period"]["previous_start"], "2019-12-01")

    def test_six_months_fill_missing_months_and_keep_partial_current_month(self) -> None:
        self.entry("income", "10")
        earlier = self.entry("income", "15", "USD")
        earlier.date = date(2026, 6, 30)
        earlier.save()
        data = self.get().json()["trends"]
        self.assertEqual(
            [row["month"] for row in data], [f"2026-{month:02}" for month in range(6, 12)]
        )
        self.assertEqual(data[0]["USD"]["income"], "15.00")
        self.assertEqual(data[1]["USD"]["income"], "0.00")
        self.assertEqual(data[-1]["IQD"]["income"], "10.00")

    def test_private_owned_reports_and_verified_session_required(self) -> None:
        self.entry("income", "999")
        self.debt("i_owe", "USD", "555")
        self.client.force_login(self.other)
        for section in ("", "accounts/", "categories/", "groups/", "people/"):
            response = self.get(section)
            self.assertEqual(response.status_code, 200)
            if section:
                self.assertEqual(response.json()["count"], 0)
        self.assertEqual(self.get().json()["current"]["IQD"]["held"], "0.00")
        foreign = self.client.get("/api/v1/transactions/", {"category": self.category.pk})
        self.assertEqual(foreign.json()["count"], 0)
        self.other.email_verified = False
        self.other.save()
        self.assertEqual(self.get().status_code, 403)
        self.client.logout()
        self.assertEqual(self.get("accounts/").status_code, 403)

    def test_invalid_dates_timezone_period_and_unknown_filters(self) -> None:
        for values in (
            {"timezone": "invalid"},
            {"period": "invalid"},
            {"period": "custom"},
            {"period": "custom", "start": "2026-11-19", "end": "2026-11-18"},
            {"period": "custom", "start": "2026-11-01", "end": "2026-11-19"},
            {"start": "2026-01-01"},
            {"user": "2"},
            {"page": "0"},
        ):
            self.assertEqual(self.get(**values).status_code, 400)

    def test_summary_rejects_paging_and_section_links_keep_report_filters(self) -> None:
        self.assertEqual(self.get(page="2").status_code, 400)
        for index in range(31):
            save_account(
                self.user,
                {
                    "name": f"Paged {index:02}",
                    "group": None,
                    "opening_iqd": Decimal("0"),
                    "opening_usd": Decimal("0"),
                },
            )
        report = self.get(period="custom", start="2026-01-01", end="2026-01-31").json()
        accounts = report["breakdowns"]["accounts"]
        self.assertEqual(len(accounts["results"]), 30)
        next_page = urlsplit(accounts["next"])
        self.assertEqual(next_page.path, "/api/v1/analytics/accounts/")
        self.assertEqual(
            parse_qs(next_page.query),
            {
                "timezone": ["Asia/Baghdad"],
                "period": ["custom"],
                "start": ["2026-01-01"],
                "end": ["2026-01-31"],
                "page": ["2"],
            },
        )
        response = self.client.get(f"{next_page.path}?{next_page.query}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["results"]), 2)

    def test_timezone_and_calendar_boundaries_include_leap_years(self) -> None:
        with patch(
            "finance.analytics.timezone.now", return_value=datetime(2024, 3, 1, 0, 30, tzinfo=UTC)
        ):
            east = report_period({"period": "this_month", "timezone": "Asia/Baghdad"})
            west = report_period({"period": "this_month", "timezone": "America/Los_Angeles"})
            self.assertEqual(east["start"], date(2024, 3, 1))
            self.assertEqual(west["end"], date(2024, 2, 29))
            self.assertEqual(west["previous_end"], date(2024, 1, 29))
            yearly = report_period({"period": "this_year", "timezone": "America/Los_Angeles"})
            self.assertEqual(yearly["previous_end"], date(2023, 2, 28))

    def test_maximum_precision_and_aggregate_above_single_account_capacity(self) -> None:
        if connection.vendor != "postgresql":
            self.skipTest("Full Decimal precision requires PostgreSQL")
        OpeningBalance.objects.filter(account=self.account).update(
            iqd=Decimal("9999999999999999.99")
        )
        save_account(
            self.user,
            {
                "name": "Large",
                "group": None,
                "opening_iqd": Decimal("9999999999999999.99"),
                "opening_usd": Decimal("0.01"),
            },
        )
        self.assertEqual(self.get().json()["current"]["IQD"]["held"], "19999999999999999.98")

    def test_query_counts_do_not_grow_with_accounts_or_debts(self) -> None:
        self.debt("owed_to_me", "IQD", "100")
        self.get()  # Enroll session metadata before comparing steady-state request queries.
        with CaptureQueriesContext(connection) as first:
            self.get()
        for _index in range(5):
            self.debt("owed_to_me", "IQD", "100")
        with CaptureQueriesContext(connection) as second:
            self.get()
        self.assertEqual(len(first), len(second))
        self.assertLessEqual(len(second), 30)
