from datetime import date
from decimal import Decimal
from unittest import skipUnless
from uuid import uuid4

from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from rest_framework.response import Response
from rest_framework.test import APIClient

from . import test_transactions as ledger_tests
from .debt_serializers import DebtSerializer
from .debts import debts_for
from .models import Debt, DebtPerson, MoneyTransaction, OpeningBalance
from .services import accounts_for, archive_account, save_account


class DebtTests(TestCase):
    post = ledger_tests.TransactionTests.post

    def setUp(self) -> None:
        ledger_tests.TransactionTests.setUp(self)
        self.root = "/api/v1/debts/"
        self.values = {
            "person": None,
            "person_name": "Mona",
            "direction": "owed_to_me",
            "origin": "existing",
            "account": None,
            "currency": "IQD",
            "amount": "100.00",
            "date": "2020-01-01",
            "note": "Original loan",
            "timezone": "Asia/Baghdad",
        }

    def debt(self, **overrides: object) -> Response:
        return self.post("", {**self.values, "request_id": str(uuid4()), **overrides})

    def pay(self, record: dict, **overrides: object) -> Response:
        return self.post(
            f"{record['id']}/repayments/",
            {
                "account": self.account.pk,
                "amount": "30.00",
                "date": "2026-01-02",
                "note": "Repayment",
                "timezone": "Asia/Baghdad",
                "request_id": str(uuid4()),
                **overrides,
            },
        )

    def detail(self, debt: dict) -> dict:
        return self.client.get(f"{self.root}{debt['id']}/").json()

    def update_debt(self, debt: dict, **overrides: object) -> Response:
        values = {
            field: debt[field]
            for field in (
                "person",
                "direction",
                "origin",
                "account",
                "currency",
                "amount",
                "date",
                "note",
                "version",
            )
        }
        return self.post(
            f"{debt['id']}/", {**values, "person_name": "", "timezone": "Asia/Baghdad", **overrides}
        )

    def update_payment(self, debt: dict, payment: dict, **overrides: object) -> Response:
        values = {
            field: payment[field] for field in ("account", "amount", "date", "note", "version")
        }
        return self.post(
            f"{debt['id']}/repayments/{payment['id']}/",
            {**values, "timezone": "Asia/Baghdad", **overrides},
        )

    def balance(self, currency: str = "iqd") -> Decimal:
        return getattr(accounts_for(self.user).get(pk=self.account.pk), f"balance_{currency}")

    def delete_payment(self, debt: dict, payment: dict) -> Response:
        return self.post(
            f"{debt['id']}/repayments/{payment['id']}/delete/", {"version": payment["version"]}
        )

    def test_existing_debts_original_dates_do_not_move_cash(self) -> None:
        for direction in ("owed_to_me", "i_owe"):
            response = self.debt(direction=direction)
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.json()["remaining"], "100.00")
            self.assertEqual(response.json()["repaid"], "0.00")
        self.assertEqual(self.balance(), Decimal("100"))
        self.assertEqual(MoneyTransaction.objects.count(), 0)
        self.assertEqual(DebtPerson.objects.count(), 1)

    def test_new_lending_borrowing_and_activity_exclusions(self) -> None:
        lent = self.debt(origin="movement", account=self.account.pk, amount="60", date="2026-01-02")
        self.assertEqual(lent.status_code, 201)
        self.assertEqual(self.balance(), Decimal("40"))
        borrowed = self.debt(
            direction="i_owe",
            origin="movement",
            account=self.account.pk,
            amount="50",
            date="2026-01-02",
        )
        self.assertEqual(borrowed.status_code, 201)
        self.assertEqual(self.balance(), Decimal("90"))
        activity = self.client.get("/api/v1/transactions/").json()
        self.assertEqual(activity["count"], 2)
        self.assertEqual(activity["results"][0]["debt_person_name"], "Mona")
        self.assertEqual(activity["results"][0]["debt_role"], "principal")
        for kind in ("income", "expense"):
            self.assertEqual(
                self.client.get("/api/v1/transactions/", {"kind": kind}).json()["count"], 0
            )

    def test_partial_repayments_both_directions_update_remaining_and_cash(self) -> None:
        owed = self.debt().json()
        payment = self.pay(owed)
        self.assertEqual(payment.status_code, 201)
        self.assertEqual(payment.json()["kind"], "debt_in")
        self.assertEqual(self.detail(owed)["remaining"], "70.00")
        self.assertEqual(self.balance(), Decimal("130"))
        owing = self.debt(direction="i_owe").json()
        self.assertEqual(self.pay(owing, amount="40").status_code, 201)
        self.assertEqual(self.balance(), Decimal("90"))
        self.assertEqual(self.detail(owing)["remaining"], "60.00")

    def test_same_person_independent_currencies_and_case_insensitive_grouping(self) -> None:
        first = self.debt().json()
        second = self.debt(person_name="mONA", currency="USD").json()
        self.assertEqual(first["person"], second["person"])
        self.assertEqual(self.pay(second, amount="10").status_code, 201)
        self.assertEqual(self.detail(first)["remaining"], "100.00")
        self.assertEqual(self.balance("usd"), Decimal("30"))
        self.assertEqual(
            self.client.get(self.root, {"person": first["person"], "currency": "USD"}).json()[
                "count"
            ],
            1,
        )

    def test_settled_filter_and_overpayment_roll_back(self) -> None:
        debt = self.debt().json()
        self.assertEqual(self.pay(debt, amount="100.01").status_code, 400)
        self.assertEqual(self.balance(), Decimal("100"))
        self.assertEqual(self.pay(debt, amount="100").status_code, 201)
        self.assertEqual(self.client.get(self.root).json()["count"], 0)
        self.assertEqual(self.client.get(self.root, {"status": "settled"}).json()["count"], 1)
        self.assertEqual(self.pay(debt, amount="0.01").status_code, 400)

    def test_repayment_cannot_predate_debt_account_or_future(self) -> None:
        debt = self.debt(date="2026-01-03").json()
        for payment_date in ("2026-01-02", "2099-01-01", "2025-12-31"):
            self.assertEqual(self.pay(debt, date=payment_date).status_code, 400)
        self.assertEqual(self.pay(debt, date="2026-01-03").status_code, 201)
        self.assertEqual(self.debt(date="2099-01-01").status_code, 400)
        self.assertEqual(self.debt(origin="movement", account=self.account.pk).status_code, 400)

    def test_correction_respects_repayments_date_currency_direction_and_version(self) -> None:
        debt = self.debt().json()
        self.pay(debt)
        self.assertEqual(self.update_debt(debt, amount="200").status_code, 409)
        current = self.detail(debt)
        for values in (
            {"amount": "29.99"},
            {"currency": "USD"},
            {"direction": "i_owe"},
            {"date": "2026-01-03"},
        ):
            self.assertEqual(self.update_debt(current, **values).status_code, 400)
        response = self.update_debt(current, amount="40", note="Corrected")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["remaining"], "10.00")

    def test_delete_requires_removing_repayments_even_when_settled(self) -> None:
        debt = self.debt().json()
        payment = self.pay(debt, amount="100").json()
        self.assertEqual(self.post(f"{debt['id']}/delete/", {"version": 2}).status_code, 400)
        self.assertEqual(self.delete_payment(debt, payment).status_code, 204)
        current = self.detail(debt)
        self.assertEqual(current["remaining"], "100.00")
        self.assertEqual(
            self.post(f"{debt['id']}/delete/", {"version": current["version"]}).status_code, 204
        )
        self.assertEqual(self.client.get(f"{self.root}{debt['id']}/").status_code, 404)

    def test_borrowed_principal_spent_blocks_delete_and_reduction(self) -> None:
        debt = self.debt(
            direction="i_owe",
            origin="movement",
            account=self.account.pk,
            amount="100",
            date="2026-01-02",
        ).json()
        from .ledger import save_transaction

        save_transaction(
            self.user,
            {
                "account": self.account.pk,
                "category": self.category.pk,
                "kind": "expense",
                "currency": "IQD",
                "amount": Decimal("190"),
                "date": date(2026, 1, 3),
                "note": "",
                "timezone": "UTC",
                "request_id": uuid4(),
            },
        )
        self.assertEqual(self.post(f"{debt['id']}/delete/", {"version": 1}).status_code, 400)
        self.assertEqual(self.update_debt(debt, amount="80").status_code, 400)
        self.assertEqual(self.balance(), Decimal("10"))

    def test_received_repayment_spent_blocks_delete_and_reduction(self) -> None:
        debt = self.debt().json()
        payment = self.pay(debt, amount="100").json()
        from .ledger import save_transaction

        save_transaction(
            self.user,
            {
                "account": self.account.pk,
                "category": self.category.pk,
                "kind": "expense",
                "currency": "IQD",
                "amount": Decimal("190"),
                "date": date(2026, 1, 3),
                "note": "",
                "timezone": "UTC",
                "request_id": uuid4(),
            },
        )
        self.assertEqual(self.delete_payment(debt, payment).status_code, 400)
        self.assertEqual(self.update_payment(debt, payment, amount="80").status_code, 400)
        self.assertEqual(self.detail(debt)["remaining"], "0.00")

    def test_principal_and_repayment_outflows_never_overdraw(self) -> None:
        self.assertEqual(
            self.debt(
                origin="movement", account=self.account.pk, date="2026-01-02", amount="100.01"
            ).status_code,
            400,
        )
        debt = self.debt(direction="i_owe", amount="200").json()
        self.assertEqual(self.pay(debt, amount="100.01").status_code, 400)
        self.assertEqual(MoneyTransaction.objects.count(), 0)

    def test_repayment_edit_account_amount_and_delete_check_all_sides(self) -> None:
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
        debt = self.debt().json()
        payment = self.pay(debt).json()
        changed = self.update_payment(debt, payment, account=target.pk, amount="40").json()
        self.assertEqual(self.balance(), Decimal("100"))
        self.assertEqual(accounts_for(self.user).get(pk=target.pk).balance_iqd, Decimal("40"))
        self.assertEqual(self.detail(debt)["remaining"], "60.00")
        self.assertEqual(self.delete_payment(debt, changed).status_code, 204)
        self.assertEqual(accounts_for(self.user).get(pk=target.pk).balance_iqd, 0)

    def test_origin_changes_update_cash_and_restore_one_principal(self) -> None:
        debt = self.debt(date="2026-01-02", amount="30").json()
        linked = self.update_debt(debt, origin="movement", account=self.account.pk).json()
        self.assertEqual(self.balance(), Decimal("70"))
        existing = self.update_debt(linked, origin="existing", account=None).json()
        self.assertEqual(self.balance(), Decimal("100"))
        self.assertEqual(
            self.update_debt(existing, origin="movement", account=self.account.pk).status_code, 200
        )
        self.assertEqual(MoneyTransaction.objects.filter(debt_id=debt["id"]).count(), 1)

    def test_person_corrections_and_foreign_references_are_private(self) -> None:
        foreign = save_account(
            self.other,
            {
                "name": "Foreign",
                "group": None,
                "opening_iqd": Decimal("0"),
                "opening_usd": Decimal("0"),
            },
        )
        person = DebtPerson.objects.create(user=self.other, name="Private")
        self.assertEqual(self.debt(person=person.pk, person_name="").status_code, 404)
        self.assertEqual(
            self.debt(origin="movement", account=foreign.pk, date="2026-01-02").status_code, 400
        )
        debt = self.debt().json()
        self.assertEqual(self.pay(debt, account=foreign.pk).status_code, 400)
        changed = self.update_debt(debt, person=None, person_name="Sam").json()
        self.assertEqual(changed["person_name"], "Sam")

    def test_archive_retention_new_references_and_restore_rules(self) -> None:
        debt = self.debt(
            origin="movement", account=self.account.pk, date="2026-01-02", amount="20"
        ).json()
        payment = self.pay(debt, amount="10").json()
        archive_account(self.user, self.account.pk, True)
        self.assertEqual(self.update_debt(self.detail(debt), note="Correction").status_code, 200)
        self.assertEqual(self.update_payment(debt, payment, note="Correction").status_code, 200)
        self.assertEqual(self.pay(debt, amount="1").status_code, 400)
        self.assertEqual(
            self.debt(origin="movement", account=self.account.pk, date="2026-01-02").status_code,
            400,
        )

    def test_debt_request_payload_retry_and_deleted_tombstone(self) -> None:
        request_id = str(uuid4())
        debt = self.debt(request_id=request_id).json()
        self.assertEqual(self.debt(request_id=request_id).status_code, 200)
        self.assertEqual(self.debt(request_id=request_id, amount="101").status_code, 409)
        self.assertEqual(self.post(f"{debt['id']}/delete/", {"version": 1}).status_code, 204)
        self.assertEqual(self.debt(request_id=request_id).status_code, 409)

    def test_repayment_retry_stale_version_and_tombstone(self) -> None:
        debt = self.debt().json()
        request_id = str(uuid4())
        payment = self.pay(debt, request_id=request_id).json()
        self.assertEqual(self.pay(debt, request_id=request_id).status_code, 200)
        self.assertEqual(self.pay(debt, request_id=request_id, amount="31").status_code, 409)
        edited = self.update_payment(debt, payment, note="Corrected").json()
        self.assertEqual(self.update_payment(debt, payment).status_code, 409)
        self.assertEqual(self.delete_payment(debt, payment).status_code, 409)
        self.assertEqual(self.delete_payment(debt, edited).status_code, 204)
        self.assertEqual(self.pay(debt, request_id=request_id).status_code, 409)

    def test_generic_activity_mutations_cannot_bypass_debt_integrity(self) -> None:
        debt = self.debt().json()
        payment = self.pay(debt).json()
        token = self.client.get("/api/v1/auth/csrf/").json()["csrf_token"]
        base = "/api/v1/transactions/"
        ordinary = {
            field: payment[field] for field in ("account", "amount", "date", "note", "version")
        }
        for suffix, values in [
            (f"{payment['id']}/delete/", {"version": 1}),
            (
                f"{payment['id']}/",
                {
                    **ordinary,
                    "kind": "income",
                    "currency": "IQD",
                    "category": None,
                    "timezone": "UTC",
                },
            ),
            (
                "",
                {
                    **ordinary,
                    "kind": "debt_in",
                    "currency": "IQD",
                    "timezone": "UTC",
                    "request_id": str(uuid4()),
                },
            ),
        ]:
            values.pop("version", None) if suffix == "" else None
            self.assertEqual(
                self.client.post(
                    base + suffix, values, format="json", HTTP_X_CSRFTOKEN=token
                ).status_code,
                400,
            )
        self.assertEqual(self.detail(debt)["remaining"], "70.00")

    def test_privacy_csrf_verified_user_and_read_no_side_effects(self) -> None:
        self.assertEqual(self.client.get(self.root).status_code, 200)
        self.assertEqual(DebtPerson.objects.count(), 0)
        debt = self.debt().json()
        payment = self.pay(debt).json()
        self.assertEqual(self.client.post(self.root, self.values, format="json").status_code, 403)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.root).json()["count"], 0)
        self.assertEqual(self.client.get(f"{self.root}{debt['id']}/").status_code, 404)
        self.assertEqual(self.update_debt(debt).status_code, 404)
        self.assertEqual(self.pay(debt).status_code, 404)
        self.assertEqual(self.delete_payment(debt, payment).status_code, 404)
        self.client = APIClient()
        self.assertEqual(self.client.get(self.root).status_code, 403)
        self.user.email_verified = False
        self.user.save(update_fields=["email_verified"])
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.root).status_code, 403)

    def test_strict_input_and_fixed_repayment_currency(self) -> None:
        for values in (
            {"amount": "0"},
            {"amount": "-1"},
            {"amount": "1.001"},
            {"amount": 1},
            {"currency": "EUR"},
            {"person_name": ""},
            {"user": self.other.pk},
            {"origin": "existing", "account": self.account.pk},
            {"direction": "wrong"},
        ):
            self.assertEqual(self.debt(**values).status_code, 400)
        debt = self.debt().json()
        for values in ({"amount": "0"}, {"amount": 1}, {"currency": "USD"}, {"debt": 999}):
            self.assertEqual(self.pay(debt, **values).status_code, 400)

    @skipUnless(connection.vendor == "postgresql", "Full precision requires PostgreSQL")
    def test_exact_maximum_amount_and_incoming_overflow(self) -> None:
        debt = self.debt(amount="9999999999999999.99").json()
        self.assertEqual(debt["remaining"], "9999999999999999.99")
        OpeningBalance.objects.filter(account=self.account).update(
            iqd=Decimal("9999999999999999.99")
        )
        self.assertEqual(self.pay(debt, amount="0.01").status_code, 400)
        self.assertEqual(self.detail(debt)["repaid"], "0.00")

    def test_paginated_people_debts_payments_filters_and_query_bounds(self) -> None:
        for index in range(31):
            self.debt(person_name=f"Person {index}")
        self.assertEqual(len(self.client.get(self.root).json()["results"]), 30)
        self.assertEqual(len(self.client.get(self.root, {"page": 2}).json()["results"]), 1)
        people = self.client.get("/api/v1/debt-people/").json()
        self.assertEqual(len(people["results"]), 30)
        self.assertNotIn("user", people["results"][0])
        debt = self.debt(amount="100").json()
        for _ in range(31):
            self.pay(debt, amount="1")
        self.assertEqual(
            len(self.client.get(f"{self.root}{debt['id']}/repayments/").json()["results"]), 30
        )
        with self.assertNumQueries(1):
            self.assertEqual(len(DebtSerializer(debts_for(self.user)[:30], many=True).data), 30)
        for query in ({"status": "wrong"}, {"person": -1}, {"currency": "EUR"}, {"user": 1}):
            self.assertEqual(self.client.get(self.root, query).status_code, 400)

    def test_database_constraints(self) -> None:
        debt = self.debt().json()
        for values in (
            {"amount": 0},
            {"direction": "bad"},
            {"currency": "EUR"},
            {"origin": "movement", "account": None},
        ):
            with self.assertRaises(IntegrityError), transaction.atomic():
                Debt.objects.filter(pk=debt["id"]).update(**values)
        payment = self.pay(debt).json()
        for values in ({"debt": None}, {"debt_role": ""}, {"kind": "income"}):
            with self.assertRaises(IntegrityError), transaction.atomic():
                MoneyTransaction.objects.filter(pk=payment["id"]).update(**values)

    def test_debt_request_uuid_does_not_collide_with_ordinary_money_request(self) -> None:
        from .ledger import save_transaction

        request_id = uuid4()
        save_transaction(
            self.user,
            {
                "account": self.account.pk,
                "category": None,
                "kind": "income",
                "currency": "IQD",
                "amount": Decimal("10"),
                "date": date(2026, 1, 2),
                "note": "",
                "timezone": "UTC",
                "request_id": request_id,
            },
        )
        response = self.debt(
            request_id=str(request_id),
            origin="movement",
            account=self.account.pk,
            date="2026-01-02",
            amount="10",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.balance(), Decimal("100"))
        self.assertEqual(
            self.debt(
                request_id=str(request_id),
                origin="movement",
                account=self.account.pk,
                date="2026-01-02",
                amount="10",
            ).status_code,
            200,
        )
