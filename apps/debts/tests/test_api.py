from decimal import Decimal
from uuid import uuid4

from django.test import TestCase, override_settings

from apps.accounts.models import User
from apps.debts.models import Debt, DebtEvent
from tests.financial_helpers import FinancialApiTestMixin


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class DebtApiTests(FinancialApiTestMixin, TestCase):
    def debt_write(self, path: str, data: dict):
        return self.client.post(
            f"/api/v1/debts/{path}/", data, format="json", HTTP_IDEMPOTENCY_KEY=str(uuid4())
        )

    def person(self) -> int:
        response = self.debt_write("people/create", {"name": "Example Person"})
        self.assertEqual(response.status_code, 200)
        return response.json()["person_id"]

    def create(self, **kwargs):
        return self.debt_write(
            "records/create",
            self.dated(
                person_id=self.person(),
                direction="receivable",
                currency="IQD",
                amount="50.00",
                movement="record_only",
            )
            | kwargs,
        )

    def test_record_only_repayment_and_writeoff_never_move_balances(self) -> None:
        created = self.create().json()
        debt_id = created["debt_id"]
        repayment = self.debt_write(
            f"records/{debt_id}/repay",
            self.dated(
                settled_amount="20.00",
                payment_amount="20.00",
                payment_currency="IQD",
                movement="record_only",
            ),
        )
        self.assertEqual(repayment.status_code, 200)
        writeoff = self.debt_write(
            f"records/{debt_id}/write-off", self.dated(settled_amount="30.00", reason="Forgiven")
        )
        self.assertEqual(writeoff.status_code, 200)
        self.assertEqual(self.amount(self.budget), Decimal("100"))
        self.assertEqual(Debt.objects.get(pk=debt_id).remaining_amount, Decimal("0"))
        self.assertEqual(
            self.client.get("/api/v1/finances/reports/").json()["expense"]["IQD"], "0.00"
        )
        self.reconcile()

    def test_lending_and_outgoing_repayment_use_explicit_budget_split(self) -> None:
        data = self.dated(
            person_id=self.person(),
            direction="receivable",
            currency="IQD",
            amount="150.00",
            movement="account",
            account_id=self.budget.pk,
        )
        rejected = self.debt_write("records/create", data)
        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(Debt.objects.count(), 0)
        allocations = [
            {"account_id": item["account_id"], "amount": item["amount"]}
            for item in rejected.json()["allocations"]
        ]
        success = self.debt_write("records/create", {**data, "allocations": allocations})
        self.assertEqual(success.status_code, 200)
        self.assertEqual(self.amount(self.budget), Decimal("0"))
        self.assertEqual(self.amount(self.savings), Decimal("50"))
        self.assertEqual(
            self.client.get("/api/v1/finances/reports/").json()["expense"]["IQD"], "0.00"
        )
        self.reconcile()

    def test_cross_currency_repayments_preserve_both_amounts_and_rate(self) -> None:
        created = self.create(
            direction="payable",
            currency="USD",
            amount="10.00",
            movement="account",
            account_id=self.budget.pk,
        ).json()
        response = self.debt_write(
            f"records/{created['debt_id']}/repay",
            self.dated(
                settled_amount="2.00",
                payment_amount="5.00",
                payment_currency="IQD",
                movement="account",
                account_id=self.budget.pk,
            ),
        )
        self.assertEqual(response.status_code, 200)
        debt = Debt.objects.get(pk=created["debt_id"])
        self.assertEqual(debt.remaining_amount, Decimal("8"))
        self.assertEqual(self.amount(self.budget, "USD"), Decimal("20"))
        self.assertEqual(self.amount(self.budget), Decimal("95"))
        event = DebtEvent.objects.get(pk=response.json()["event_id"])
        self.assertEqual(event.operation.details["effective_rate"], "2.500000")
        reports = self.client.get("/api/v1/finances/reports/").json()
        self.assertEqual(
            reports,
            {"income": {"IQD": "0.00", "USD": "0.00"}, "expense": {"IQD": "0.00", "USD": "0.00"}},
        )
        self.reconcile()

    def test_older_debt_reverse_identifies_dependencies_and_posts_current_month(self) -> None:
        created = self.create(movement="account", account_id=self.budget.pk).json()
        repayment = self.debt_write(
            f"records/{created['debt_id']}/repay",
            self.dated(
                settled_amount="10.00",
                payment_amount="10.00",
                payment_currency="IQD",
                movement="account",
                account_id=self.budget.pk,
            ),
        ).json()
        self.write(
            "rollover",
            {"month": "2026-12-01", "timezone": "UTC", "salary": {"IQD": "0.00", "USD": "0.00"}},
        )
        current = {"local_date": "2026-12-02", "timezone": "UTC"}
        denied = self.debt_write(f"events/{created['event_id']}/reverse", current)
        self.assertEqual(denied.status_code, 409)
        self.assertIn(str(repayment["event_id"]), denied.json()["event_ids"])
        # Repayment reversal needs its original account funds; rollover moved them to savings.
        self.assertEqual(
            self.debt_write(f"events/{repayment['event_id']}/reverse", current).status_code, 409
        )
        self.write(
            "transfers",
            {
                **current,
                "source_id": self.savings.pk,
                "target_id": self.budget.pk,
                "currency": "IQD",
                "amount": "10.00",
            },
        )
        self.assertEqual(
            self.debt_write(f"events/{repayment['event_id']}/reverse", current).status_code, 200
        )
        self.assertEqual(
            self.debt_write(f"events/{created['event_id']}/reverse", current).status_code, 200
        )
        original = DebtEvent.objects.get(pk=created["event_id"])
        self.assertEqual(original.operation.local_date.isoformat(), "2026-11-10")
        self.assertEqual(original.operation.reversal.local_date.isoformat(), "2026-12-02")
        self.assertTrue(Debt.objects.get(pk=created["debt_id"]).cancelled)
        self.reconcile()

    def test_people_archive_requires_settlement_and_lists_are_owned(self) -> None:
        created = self.create().json()
        debt = Debt.objects.get(pk=created["debt_id"])
        self.assertEqual(
            self.debt_write(f"people/{debt.person_id}/change", {"archived": True}).status_code, 409
        )
        self.debt_write(
            f"records/{debt.pk}/write-off",
            self.dated(settled_amount="50.00", reason="Settled externally"),
        )
        self.assertEqual(
            self.debt_write(f"people/{debt.person_id}/change", {"archived": True}).status_code, 200
        )
        self.assertEqual(
            self.debt_write(f"people/{debt.person_id}/change", {"archived": False}).status_code, 200
        )
        other = User.objects.create_user(username="DebtOther", password="Synthetic!Password23")
        self.client.force_authenticate(user=other)
        self.assertEqual(self.client.get(f"/api/v1/debts/records/{debt.pk}/").status_code, 404)
        self.assertEqual(
            self.client.get(f"/api/v1/debts/records/{debt.pk}/events/").status_code, 404
        )
        self.assertEqual(
            self.client.get(f"/api/v1/debts/people/{debt.person_id}/debts/").status_code, 404
        )
        self.assertEqual(self.client.get("/api/v1/debts/people/").json()["results"], [])

    def test_overpayment_same_currency_mismatch_and_invalid_replacement_are_atomic(self) -> None:
        created = self.create().json()
        for settled, payment in [("51.00", "51.00"), ("1.00", "2.00")]:
            response = self.debt_write(
                f"records/{created['debt_id']}/repay",
                self.dated(
                    settled_amount=settled,
                    payment_amount=payment,
                    payment_currency="IQD",
                    movement="record_only",
                ),
            )
            self.assertIn(response.status_code, (400, 409))
        correction = self.debt_write(
            f"events/{created['event_id']}/reverse",
            self.dated(
                replacement={
                    "kind": "debt_create",
                    "data": self.dated(
                        person_id=self.person(),
                        direction="receivable",
                        currency="IQD",
                        amount="999.00",
                        movement="account",
                        account_id=self.budget.pk,
                    ),
                }
            ),
        )
        self.assertEqual(correction.status_code, 409)
        self.assertFalse(Debt.objects.get(pk=created["debt_id"]).cancelled)
        self.assertEqual(DebtEvent.objects.count(), 1)
