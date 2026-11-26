"""Account group drill-down stays scoped to the signed-in owner."""

from django.test import TestCase
from rest_framework.test import APIClient

from app.finance.models import Account, AccountGroup
from app.identity.models import User


class AccountGroupFilterTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(
            "account-filter@example.com", language="en", email_verified=True
        )
        self.other = User.objects.create_user(
            "another-filter@example.com", language="en", email_verified=True
        )
        self.client = APIClient()
        self.client.force_login(self.user)
        self.group = AccountGroup.objects.create(user=self.user, name="Cash")
        self.other_group = AccountGroup.objects.create(user=self.user, name="Bank")
        self.foreign_group = AccountGroup.objects.create(user=self.other, name="Foreign")
        self.in_group = Account.objects.create(user=self.user, group=self.group, name="Wallet")
        Account.objects.create(user=self.user, group=self.other_group, name="Checking")
        Account.objects.create(user=self.user, group=None, name="Ungrouped")
        Account.objects.create(user=self.other, group=self.foreign_group, name="Private")

    def test_group_filter_returns_only_matching_owned_accounts(self) -> None:
        response = self.client.get(f"/api/v1/accounts/?group={self.group.pk}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual([row["id"] for row in response.json()["results"]], [self.in_group.pk])

    def test_invalid_and_foreign_group_are_rejected(self) -> None:
        self.assertEqual(self.client.get("/api/v1/accounts/?group=abc").status_code, 400)
        self.assertEqual(self.client.get("/api/v1/accounts/?group=-1").status_code, 400)
        self.assertEqual(
            self.client.get(f"/api/v1/accounts/?group={self.foreign_group.pk}").status_code,
            404,
        )

    def test_guest_cannot_filter_accounts(self) -> None:
        guest = APIClient()
        self.assertEqual(guest.get(f"/api/v1/accounts/?group={self.group.pk}").status_code, 403)
