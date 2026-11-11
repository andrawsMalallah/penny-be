"""Shared financial setup and assertions without inheritable test cases."""

from decimal import Decimal
from uuid import uuid4

from django.db.models import Sum
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.finances.models import Account, Balance, Category, LedgerEntry


class FinancialApiTestMixin:
    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="FinanceUser", password="Synthetic!Password23"
        )
        self.client = APIClient(enforce_csrf_checks=True)
        token = self.client.get("/api/v1/auth/csrf/").json()["csrf_token"]
        login = self.client.post(
            "/api/v1/auth/login/",
            {"username": self.user.username, "password": "Synthetic!Password23"},
            format="json",
            HTTP_X_CSRFTOKEN=token,
        )
        self.client.credentials(HTTP_X_CSRFTOKEN=login.json()["csrf_token"])
        self.assertEqual(
            self.write(
                "setup",
                {
                    "month": "2026-11-01",
                    "timezone": "Asia/Baghdad",
                    "budget": {"IQD": "100.00", "USD": "10.00"},
                    "main_savings": {"IQD": "100.00", "USD": "0.00"},
                },
            ).status_code,
            200,
        )
        self.budget = Account.objects.get(owner=self.user, kind="budget")
        self.savings = Account.objects.get(owner=self.user, kind="main_savings")
        self.category = Category.objects.filter(owner=self.user).first()

    def write(self, path: str, data: dict, key: str | None = None):
        return self.client.post(
            f"/api/v1/finances/{path}/",
            data,
            format="json",
            HTTP_IDEMPOTENCY_KEY=key or str(uuid4()),
        )

    def dated(self, **kwargs) -> dict:
        return {"local_date": "2026-11-10", "timezone": "Asia/Baghdad", **kwargs}

    def expense(self, amount: str, **kwargs) -> dict:
        return self.dated(
            amount=amount,
            currency="IQD",
            source_id=self.budget.pk,
            category_id=self.category.pk,
            **kwargs,
        )

    def amount(self, account: Account, currency: str = "IQD") -> Decimal:
        return Balance.objects.get(account=account, currency=currency).amount

    def reconcile(self) -> None:
        for balance in Balance.objects.filter(account__owner=self.user):
            total = LedgerEntry.objects.filter(balance=balance).aggregate(total=Sum("delta"))[
                "total"
            ] or Decimal("0")
            self.assertEqual(balance.amount, total)
