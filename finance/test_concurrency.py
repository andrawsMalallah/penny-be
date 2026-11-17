from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from unittest import skipUnless

from django.db import close_old_connections, connection
from django.test import TransactionTestCase
from rest_framework.exceptions import ValidationError

from identity.models import User, UserSettings
from identity.user_settings import save_user_settings

from .models import Account, AccountGroup, OpeningBalance
from .services import archive_account, archive_group, save_account


@skipUnless(connection.vendor == "postgresql", "Requires isolated PostgreSQL test database")
class AccountConcurrencyTests(TransactionTestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(
            "finance-race@example.com", language="en", email_verified=True
        )
        self.group = AccountGroup.objects.create(user=self.user, name="Daily")
        self.values = {
            "name": "Wallet",
            "group": self.group.pk,
            "opening_iqd": Decimal("1.25"),
            "opening_usd": Decimal("0"),
        }
        self.account = save_account(self.user, self.values)

    def simultaneously(self, first: Callable, second: Callable) -> list[bool]:
        barrier = Barrier(2, timeout=15)

        def attempt(action: Callable) -> bool:
            close_old_connections()
            try:
                barrier.wait()
                try:
                    action()
                    return True
                except ValidationError:
                    return False
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            return list(executor.map(attempt, [first, second]))

    def test_archiving_and_selecting_default_never_leave_archived_default(self) -> None:
        results = self.simultaneously(
            lambda: archive_account(self.user, self.account.pk, True),
            lambda: save_user_settings(
                self.user,
                {
                    "language": "en",
                    "default_currency": "IQD",
                    "exchange_rate": None,
                    "default_account": self.account.pk,
                },
            ),
        )
        self.assertTrue(results[0])
        self.assertTrue(Account.objects.get(pk=self.account.pk).archived)
        self.assertFalse(
            UserSettings.objects.filter(user=self.user, default_account__isnull=False).exists()
        )
        self.assertEqual(OpeningBalance.objects.get(account=self.account).iqd, Decimal("1.25"))

    def test_simultaneous_same_name_creates_only_one_complete_account(self) -> None:
        values = {**self.values, "name": "New account"}
        results = self.simultaneously(
            lambda: save_account(self.user, values), lambda: save_account(self.user, values)
        )
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(Account.objects.filter(name="New account").count(), 1)
        self.assertEqual(OpeningBalance.objects.count(), 2)

    def test_group_archive_and_assignment_never_leave_active_account_in_archived_group(
        self,
    ) -> None:
        archive_account(self.user, self.account.pk, True)
        results = self.simultaneously(
            lambda: archive_group(self.user, self.group.pk, True),
            lambda: save_account(self.user, {**self.values, "name": "Another"}),
        )
        self.assertEqual(sorted(results), [False, True])
        self.assertFalse(Account.objects.filter(archived=False, group__archived=True).exists())
