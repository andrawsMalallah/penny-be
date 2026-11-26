"""Account deletion requires a fresh, single-use email proof."""

import re
import uuid
from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.core import mail
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from app.finance.models import (
    Account,
    AccountGroup,
    BudgetRule,
    BudgetSchedule,
    CategorySetup,
    Debt,
    DebtPerson,
    ExpenseCategory,
    MoneyTransaction,
    OpeningBalance,
    RecurringOutcome,
    RecurringSchedule,
)
from app.identity.models import (
    AccountDeletionChallenge,
    EmailVerification,
    Registration,
    SignedInSession,
    User,
    UserSettings,
)


class AccountDeletionTests(TestCase):
    def setUp(self) -> None:
        self.password = "Private account deletion phrase 731!"
        self.user = User.objects.create_user(
            "delete-me@example.com", self.password, language="en", email_verified=True
        )
        self.other = User.objects.create_user(
            "keep-me@example.com", self.password, language="en", email_verified=True
        )
        self.client = self.sign_in(self.user)

    def post(self, client: APIClient, path: str, body: dict, *, csrf: bool = True):
        headers = {}
        if csrf:
            headers["HTTP_X_CSRFTOKEN"] = client.get("/api/v1/auth/csrf/").json()["csrf_token"]
        return client.post(f"/api/v1/auth/account-deletion/{path}/", body, format="json", **headers)

    def sign_in(self, user: User) -> APIClient:
        client = APIClient(enforce_csrf_checks=True)
        token = client.get("/api/v1/auth/csrf/").json()["csrf_token"]
        response = client.post(
            "/api/v1/auth/login/",
            {"email": user.email, "password": self.password},
            format="json",
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(response.status_code, 200)
        return client

    def request_code(self, client: APIClient | None = None) -> str:
        response = self.post(client or self.client, "request", {})
        self.assertEqual(response.status_code, 202)
        return re.search(r"[0-9]{6}", mail.outbox[-1].body).group()

    def confirm(self, code: str, client: APIClient | None = None, *, confirmation: bool = True):
        return self.post(
            client or self.client,
            "confirm",
            {"code": code, "confirmation": confirmation},
        )

    def test_confirmation_deletes_owned_data_and_revokes_sessions_only_for_owner(self) -> None:
        second_device = self.sign_in(self.user)
        other_device = self.sign_in(self.other)
        group = AccountGroup.objects.create(user=self.user, name="Private group")
        account = Account.objects.create(user=self.user, group=group, name="Private account")
        opening = OpeningBalance.objects.create(
            account=account,
            iqd=Decimal("100.00"),
            usd=Decimal("0.00"),
            effective_date=timezone.localdate(),
        )
        entry = MoneyTransaction.objects.create(
            user=self.user,
            account=account,
            kind="income",
            currency="IQD",
            amount=Decimal("10.00"),
            date=timezone.localdate(),
            request_id=uuid.uuid4(),
            creation_payload={},
        )
        UserSettings.objects.create(user=self.user, default_account=account)
        category = ExpenseCategory.objects.create(user=self.user, name_en="Private category")
        CategorySetup.objects.create(user=self.user)
        schedule = BudgetSchedule.objects.create(category=category)
        BudgetRule.objects.create(
            schedule=schedule,
            month=timezone.localdate().replace(day=1),
            recurring=False,
            limit_iqd=Decimal("20.00"),
        )
        person = DebtPerson.objects.create(user=self.user, name="Private person")
        debt = Debt.objects.create(
            user=self.user,
            person=person,
            direction="owed_to_me",
            origin="existing",
            currency="IQD",
            amount=Decimal("5.00"),
            date=timezone.localdate(),
            request_id=uuid.uuid4(),
            creation_payload={},
        )
        recurring = RecurringSchedule.objects.create(
            user=self.user,
            name="Private plan",
            kind="income",
            account=account,
            currency="IQD",
            amount=Decimal("10.00"),
            frequency="monthly",
            start_date=timezone.localdate(),
            request_id=uuid.uuid4(),
            creation_payload={},
        )
        outcome = RecurringOutcome.objects.create(
            schedule=recurring,
            scheduled_date=timezone.localdate(),
            transaction=entry,
        )
        other_group = AccountGroup.objects.create(user=self.other, name="Keep group")
        stale_draft = Registration.objects.create(
            email=self.user.email,
            display_name="Stale draft",
            password_hash="stale-hash",
            language="en",
            timezone="UTC",
        )
        EmailVerification.objects.create(email=self.user.email, registration=stale_draft)
        unrelated_draft = Registration.objects.create(
            email=self.other.email,
            display_name="Other draft",
            password_hash="other-hash",
            language="en",
            timezone="UTC",
        )

        code = self.request_code()
        self.assertEqual(self.confirm(code).status_code, 204)
        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())
        self.assertFalse(Account.objects.filter(pk=account.pk).exists())
        self.assertFalse(OpeningBalance.objects.filter(pk=opening.pk).exists())
        self.assertFalse(MoneyTransaction.objects.filter(pk=entry.pk).exists())
        self.assertFalse(Debt.objects.filter(pk=debt.pk).exists())
        self.assertFalse(DebtPerson.objects.filter(pk=person.pk).exists())
        self.assertFalse(RecurringSchedule.objects.filter(pk=recurring.pk).exists())
        self.assertFalse(RecurringOutcome.objects.filter(pk=outcome.pk).exists())
        self.assertFalse(BudgetSchedule.objects.filter(pk=schedule.pk).exists())
        self.assertFalse(AccountGroup.objects.filter(pk=group.pk).exists())
        self.assertFalse(ExpenseCategory.objects.filter(user_id=self.user.pk).exists())
        self.assertFalse(CategorySetup.objects.filter(user_id=self.user.pk).exists())
        self.assertFalse(UserSettings.objects.filter(user_id=self.user.pk).exists())
        self.assertFalse(SignedInSession.objects.filter(user_id=self.user.pk).exists())
        self.assertFalse(AccountDeletionChallenge.objects.filter(user_id=self.user.pk).exists())
        self.assertFalse(Registration.objects.filter(pk=stale_draft.pk).exists())
        self.assertFalse(EmailVerification.objects.filter(email=self.user.email).exists())
        self.assertEqual(self.client.get("/api/v1/auth/me/").status_code, 403)
        self.assertEqual(second_device.get("/api/v1/auth/me/").status_code, 403)
        self.assertTrue(User.objects.filter(pk=self.other.pk).exists())
        self.assertTrue(AccountGroup.objects.filter(pk=other_group.pk).exists())
        self.assertTrue(Registration.objects.filter(pk=unrelated_draft.pk).exists())
        self.assertEqual(other_device.get("/api/v1/auth/me/").status_code, 200)

    def test_code_is_single_use_and_wrong_code_cannot_delete(self) -> None:
        code = self.request_code()
        self.assertEqual(self.confirm("999999" if code != "999999" else "000000").status_code, 400)
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())
        self.assertEqual(self.confirm(code).status_code, 204)
        self.assertNotEqual(self.confirm(code).status_code, 204)

    def test_code_expires_after_ten_minutes(self) -> None:
        code = self.request_code()
        challenge = AccountDeletionChallenge.objects.get(user=self.user)
        challenge.expires_at = timezone.now() - timedelta(seconds=1)
        challenge.save(update_fields=["expires_at"])
        self.assertEqual(self.confirm(code).status_code, 400)
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())

    def test_expired_deletion_code_is_cleared_by_bounded_cleanup(self) -> None:
        code = self.request_code()
        challenge = AccountDeletionChallenge.objects.get(user=self.user)
        challenge.expires_at = timezone.now() - timedelta(seconds=1)
        challenge.save(update_fields=["expires_at"])
        output = StringIO()
        call_command("purge_expired_codes", stdout=output)
        challenge.refresh_from_db()
        self.assertEqual(challenge.code_hash, "")
        self.assertIn("Expired code hashes cleared: 1", output.getvalue())
        self.assertEqual(self.confirm(code).status_code, 400)
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())

    def test_five_wrong_attempts_invalidate_code(self) -> None:
        code = self.request_code()
        wrong = "999999" if code != "999999" else "000000"
        for _ in range(5):
            self.assertEqual(self.confirm(wrong).status_code, 400)
        self.assertEqual(self.confirm(code).status_code, 400)
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())

    def test_request_and_confirmation_require_authentication_csrf_and_explicit_confirmation(
        self,
    ) -> None:
        guest = APIClient(enforce_csrf_checks=True)
        self.assertEqual(self.post(guest, "request", {}).status_code, 403)
        self.assertEqual(
            self.post(guest, "confirm", {"code": "000000", "confirmation": True}).status_code,
            403,
        )
        self.assertEqual(self.post(self.client, "request", {}, csrf=False).status_code, 403)
        code = self.request_code()
        self.assertEqual(self.confirm(code, confirmation=False).status_code, 400)
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())
