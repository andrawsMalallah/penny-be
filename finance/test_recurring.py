from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from django.db import IntegrityError, transaction
from django.test import TestCase
from rest_framework.test import APIClient

from identity.models import User

from . import recurring
from .ledger import delete_transaction
from .models import (
    ExpenseCategory,
    MoneyTransaction,
    OpeningBalance,
    RecurringOutcome,
    RecurringSchedule,
    RecurringWindow,
)
from .services import save_account


class RecurringTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user("repeat@example.com", email_verified=True)
        self.other = User.objects.create_user("other-repeat@example.com", email_verified=True)
        self.account = save_account(
            self.user,
            {
                "name": "Wallet",
                "group": None,
                "opening_iqd": Decimal("1000"),
                "opening_usd": Decimal("100"),
            },
        )
        OpeningBalance.objects.filter(account=self.account).update(effective_date=date(2024, 1, 1))
        self.category = ExpenseCategory.objects.create(user=self.user, name_en="Rent")
        self.client = APIClient(enforce_csrf_checks=True)
        self.client.force_login(self.user)
        self.values = {
            "name": "Rent",
            "kind": "expense",
            "account": self.account.pk,
            "category": self.category.pk,
            "currency": "IQD",
            "amount": "25.00",
            "note": "",
            "frequency": "monthly",
            "start_date": "2026-01-31",
            "end_date": None,
            "timezone": "UTC",
        }

    def post(self, suffix: str = "", **values: object):
        token = self.client.get("/api/v1/auth/csrf/").json()["csrf_token"]
        return self.client.post(
            f"/api/v1/recurring/{suffix}", values, format="json", HTTP_X_CSRFTOKEN=token
        )

    def create(self, **changes: object) -> dict:
        response = self.post(**{**self.values, "request_id": str(uuid4()), **changes})
        self.assertEqual(response.status_code, 201, response.json())
        return response.json()

    def pending(self, schedule: dict, after: str = "") -> dict:
        response = self.client.get(
            f"/api/v1/recurring/{schedule['id']}/pending/",
            {"timezone": "UTC", **({"after": after} if after else {})},
        )
        self.assertEqual(response.status_code, 200, response.json())
        return response.json()

    def update(self, schedule: dict, **changes: object):
        return self.post(
            f"{schedule['id']}/", **{**self.values, "version": schedule["version"], **changes}
        )

    def state(self, schedule: dict, state: str):
        return self.post(
            f"{schedule['id']}/state/", version=schedule["version"], state=state, timezone="UTC"
        )

    def resolve(self, schedule: dict, day: str, skip: bool = False, **changes: object):
        values = {"version": schedule["version"], "scheduled_date": day, "timezone": "UTC"}
        if not skip:
            values["transaction"] = {
                "kind": "expense",
                "account": self.account.pk,
                "category": self.category.pk,
                "currency": "IQD",
                "amount": "25.00",
                "date": "2026-03-02",
                "note": "Actual paid date",
                "timezone": "UTC",
                **changes,
            }
        return self.post(f"{schedule['id']}/{'skip' if skip else 'confirm'}/", **values)

    def test_calendar_clamping_and_anchor_preservation(self) -> None:
        self.assertEqual(
            list(
                recurring.dates_from(
                    date(2024, 1, 31), "monthly", date(2024, 1, 1), date(2024, 4, 30)
                )
            ),
            [date(2024, 1, 31), date(2024, 2, 29), date(2024, 3, 31), date(2024, 4, 30)],
        )
        self.assertEqual(
            list(
                recurring.dates_from(
                    date(2024, 2, 29), "yearly", date(2025, 1, 1), date(2028, 3, 1)
                )
            ),
            [date(2025, 2, 28), date(2026, 2, 28), date(2027, 2, 28), date(2028, 2, 29)],
        )
        self.assertEqual(
            list(
                recurring.dates_from(
                    date(2026, 1, 1), "weekly", date(2026, 1, 3), date(2026, 1, 16)
                )
            ),
            [date(2026, 1, 8), date(2026, 1, 15)],
        )
        self.assertEqual(
            list(recurring.dates_from(date.max, "monthly", date.max, date.max)), [date.max]
        )

    @patch("finance.recurring.today_in", return_value=date(2026, 3, 15))
    def test_reads_are_virtual_bounded_and_do_not_move_money(self, _today) -> None:
        schedule = self.create()
        self.assertEqual(self.pending(schedule)["results"], ["2026-01-31", "2026-02-28"])
        self.assertFalse(RecurringOutcome.objects.exists())
        self.assertFalse(MoneyTransaction.objects.exists())
        self.assertEqual(RecurringWindow.objects.count(), 1)
        self.assertEqual(RecurringSchedule.objects.get().version, 1)
        future = self.create(start_date="2027-01-31")
        self.assertEqual(self.pending(future)["results"], [])

    @patch("finance.recurring.today_in", return_value=date(2026, 3, 15))
    def test_end_inclusive_and_future_start(self, _today) -> None:
        schedule = self.create(end_date="2026-02-28")
        self.assertEqual(self.pending(schedule)["results"], ["2026-01-31", "2026-02-28"])

    def test_schedule_create_idempotency_and_stale_edits(self) -> None:
        request_id = str(uuid4())
        schedule = self.create(request_id=request_id)
        self.assertEqual(self.post(**{**self.values, "request_id": request_id}).status_code, 200)
        self.assertEqual(
            self.post(**{**self.values, "request_id": request_id, "amount": "26"}).status_code, 409
        )
        self.assertEqual(self.update(schedule, amount="26").status_code, 200)
        self.assertEqual(self.update(schedule, amount="27").status_code, 409)
        self.assertEqual(RecurringSchedule.objects.count(), 1)

    @patch("finance.recurring.today_in", return_value=date(2026, 3, 15))
    def test_confirm_once_ordinary_transaction_and_delete_never_reopens(self, _today) -> None:
        schedule = self.create()
        result = self.resolve(schedule, "2026-01-31")
        self.assertEqual(result.status_code, 200, result.json())
        self.assertEqual(self.resolve(schedule, "2026-01-31").status_code, 409)
        current = self.pending(schedule)["schedule"]
        self.assertEqual(self.resolve(current, "2026-01-31").status_code, 409)
        entry = MoneyTransaction.objects.get()
        self.assertEqual(entry.date, date(2026, 3, 2))
        self.assertEqual(entry.amount, Decimal("25"))
        delete_transaction(self.user, entry.pk, entry.version)
        self.assertEqual(self.pending(schedule)["results"], ["2026-02-28"])
        self.assertEqual(RecurringOutcome.objects.count(), 1)

    @patch("finance.recurring.today_in", return_value=date(2026, 3, 15))
    def test_skip_and_invalid_slots(self, _today) -> None:
        schedule = self.create()
        for day in ("2026-01-30", "2026-03-31", "2025-12-31"):
            self.assertEqual(self.resolve(schedule, day, skip=True).status_code, 400)
        self.assertEqual(self.resolve(schedule, "2026-01-31", skip=True).status_code, 200)
        self.assertEqual(self.pending(schedule)["results"], ["2026-02-28"])
        self.assertFalse(MoneyTransaction.objects.exists())

    @patch("finance.recurring.today_in", return_value=date(2026, 3, 15))
    def test_failed_confirmation_rolls_back_outcome_version_and_money(self, _today) -> None:
        schedule = self.create()
        for changes in (
            {"amount": "1001"},
            {"date": "2023-12-31"},
            {"date": "9999-12-31"},
            {"kind": "transfer", "category": None, "destination": self.account.pk + 100},
        ):
            self.assertEqual(self.resolve(schedule, "2026-01-31", **changes).status_code, 400)
        self.assertFalse(RecurringOutcome.objects.exists())
        self.assertFalse(MoneyTransaction.objects.exists())
        self.assertEqual(RecurringSchedule.objects.get().version, 1)

    def test_pause_resume_skips_gap_stop_preserves_pending_and_is_permanent(self) -> None:
        with patch("finance.recurring.today_in", return_value=date(2026, 1, 1)):
            schedule = self.create()
        with patch("finance.recurring.today_in", return_value=date(2026, 2, 15)):
            paused = self.state(schedule, "paused").json()
        with patch("finance.recurring.today_in", return_value=date(2026, 4, 15)):
            self.assertEqual(self.pending(paused)["results"], ["2026-01-31"])
            resumed = self.state(paused, "active").json()
        with patch("finance.recurring.today_in", return_value=date(2026, 5, 1)):
            self.assertEqual(self.pending(resumed)["results"], ["2026-01-31", "2026-04-30"])
            stopped = self.state(resumed, "stopped").json()
        with patch("finance.recurring.today_in", return_value=date(2026, 7, 1)):
            self.assertEqual(self.pending(stopped)["results"], ["2026-01-31", "2026-04-30"])
            self.assertEqual(self.state(stopped, "active").status_code, 400)
            self.assertEqual(self.update(stopped).status_code, 400)
            self.assertEqual(self.resolve(stopped, "2026-01-31", skip=True).status_code, 200)

    def test_timing_edit_preserves_pending_and_details_update_without_money(self) -> None:
        with patch("finance.recurring.today_in", return_value=date(2026, 1, 1)):
            schedule = self.create()
        with patch("finance.recurring.today_in", return_value=date(2026, 3, 15)):
            edited = self.update(
                schedule, frequency="weekly", start_date="2026-03-01", amount="40"
            ).json()
        with patch("finance.recurring.today_in", return_value=date(2026, 3, 30)):
            report = self.pending(edited)
            self.assertEqual(
                report["results"], ["2026-01-31", "2026-02-28", "2026-03-22", "2026-03-29"]
            )
            self.assertEqual(report["schedule"]["amount"], "40.00")
        self.assertFalse(MoneyTransaction.objects.exists())

    def test_timezone_rewind_does_not_overlap_timing_windows(self) -> None:
        with patch("finance.recurring.today_in", return_value=date(2026, 1, 1)):
            schedule = self.create()
        with patch("finance.recurring.today_in", return_value=date(2026, 2, 1)):
            paused = self.state(schedule, "paused").json()
        with patch("finance.recurring.today_in", return_value=date(2026, 1, 31)):
            resumed = self.state(paused, "active").json()
        with patch("finance.recurring.today_in", return_value=date(2026, 3, 1)):
            self.assertEqual(self.pending(resumed)["results"], ["2026-01-31", "2026-02-28"])

    def test_privacy_csrf_verified_user_and_foreign_references(self) -> None:
        schedule = self.create()
        self.client.force_login(self.other)
        self.assertEqual(self.client.get("/api/v1/recurring/").json()["count"], 0)
        self.assertEqual(self.client.get(f"/api/v1/recurring/{schedule['id']}/").status_code, 404)
        self.assertEqual(self.update(schedule).status_code, 404)
        self.assertEqual(self.state(schedule, "paused").status_code, 404)
        self.assertEqual(self.resolve(schedule, "2026-01-31", skip=True).status_code, 404)
        self.assertEqual(self.post(**{**self.values, "request_id": str(uuid4())}).status_code, 404)
        self.client.cookies.clear()
        self.client.force_login(self.user)
        self.assertEqual(self.client.post("/api/v1/recurring/", {}, format="json").status_code, 403)
        self.user.email_verified = False
        self.user.save(update_fields=["email_verified"])
        self.assertEqual(self.client.get("/api/v1/recurring/").status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get("/api/v1/recurring/").status_code, 403)

    def test_invalid_inputs_and_archived_references(self) -> None:
        for changes in (
            {"amount": "0"},
            {"amount": 25},
            {"amount": "0.001"},
            {"kind": "transfer"},
            {"frequency": "daily"},
            {"end_date": "2025-01-01"},
            {"start_date": "2023-01-01"},
            {"timezone": "bad"},
            {"user": self.other.pk},
            {"category": None},
        ):
            self.assertEqual(
                self.post(**{**self.values, "request_id": str(uuid4()), **changes}).status_code, 400
            )
        schedule = self.create()
        self.account.archived = True
        self.account.save(update_fields=["archived"])
        self.assertEqual(self.post(**{**self.values, "request_id": str(uuid4())}).status_code, 400)
        self.assertEqual(self.update(schedule, amount="30").status_code, 200)
        self.assertEqual(self.resolve({**schedule, "version": 2}, "2026-01-31").status_code, 400)

    def test_long_backlog_paging_and_resolved_scan_bounds(self) -> None:
        schedule = self.create(start_date="2024-01-01", frequency="weekly")
        model = RecurringSchedule.objects.get(pk=schedule["id"])
        with self.assertNumQueries(2):
            first = recurring.pending(model, date(2026, 1, 1))
        self.assertEqual(len(first["results"]), 30)
        second = recurring.pending(
            model, date(2026, 1, 1), date.fromisoformat(first["next_cursor"])
        )
        self.assertTrue(set(first["results"]).isdisjoint(second["results"]))
        dates = list(
            recurring.dates_from(date(2024, 1, 1), "weekly", date(2024, 1, 1), date(2025, 12, 31))
        )[:100]
        RecurringOutcome.objects.bulk_create(
            [RecurringOutcome(schedule=model, scheduled_date=day, skipped=True) for day in dates]
        )
        scanned = recurring.pending(model, date(2026, 1, 1))
        self.assertEqual(scanned["results"], [])
        self.assertEqual(scanned["next_cursor"], dates[-1].isoformat())
        self.assertTrue(recurring.pending(model, date(2026, 1, 1), dates[-1])["results"])

    def test_schedule_list_pagination_and_no_private_payload(self) -> None:
        for index in range(31):
            self.create(name=f"Schedule {index}")
        response = self.client.get("/api/v1/recurring/").json()
        self.assertEqual(response["count"], 31)
        self.assertEqual(len(response["results"]), 30)
        self.assertNotIn("creation_payload", response["results"][0])
        self.assertNotIn("request_id", response["results"][0])

    def test_database_integrity_and_device_today(self) -> None:
        schedule = self.create()
        with self.assertRaises(IntegrityError), transaction.atomic():
            RecurringOutcome.objects.create(
                schedule_id=schedule["id"], scheduled_date=date(2026, 1, 31), skipped=False
            )
        with patch(
            "finance.recurring.timezone.now",
            return_value=datetime(2026, 1, 1, 22, tzinfo=UTC),
        ):
            self.assertEqual(recurring.today_in("Asia/Baghdad"), date(2026, 1, 2))
