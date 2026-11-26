"""Email-confirmed, atomic removal of one user's Penny account and data."""

import secrets
import uuid

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.sessions.models import Session
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from app.finance.models import (
    Account,
    Debt,
    MoneyTransaction,
    OpeningBalance,
    RecurringSchedule,
)
from app.identity.errors import ServiceUnavailable
from app.identity.models import (
    AccountDeletionChallenge,
    EmailVerification,
    Registration,
    SignedInSession,
    User,
    UserSettings,
)
from app.identity.services.email_codes import consume_email_code
from app.identity.services.registration import (
    CODE_LIFETIME,
    INVALID_CODE_MESSAGE,
    MAX_CODE_ATTEMPTS,
    enforce_request_limit,
)

REQUEST_MESSAGE = "A confirmation code has been sent to your email address."


def request_account_deletion(user: User, remote_address: str) -> None:
    enforce_request_limit(f"delete:ip:{remote_address}", 20)
    enforce_request_limit(f"delete:user:{user.pk}", 3)
    code = f"{secrets.randbelow(1_000_000):06d}"
    generation = uuid.uuid4()
    with transaction.atomic():
        current = User.objects.select_for_update().get(pk=user.pk)
        challenge, _ = AccountDeletionChallenge.objects.get_or_create(user=current)
        challenge.generation = generation
        challenge.code_hash = ""
        challenge.expires_at = None
        challenge.failed_attempts = 0
        challenge.save()

    if current.language == "ar":
        subject = "تأكيد حذف حساب Penny"
        body = f"رمز تأكيد حذف حساب Penny هو: {code}\nينتهي خلال 10 دقائق. لا تشاركه مع أحد."
    else:
        subject = "Confirm your Penny account deletion"
        body = (
            f"Your Penny account deletion code is: {code}\n"
            "It expires in 10 minutes. Do not share it with anyone."
        )
    try:
        if send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [current.email]) != 1:
            raise ServiceUnavailable()
    except Exception:
        raise ServiceUnavailable() from None

    with transaction.atomic():
        challenge = (
            AccountDeletionChallenge.objects.select_for_update().filter(user=current).first()
        )
        if challenge is not None and challenge.generation == generation:
            challenge.code_hash = make_password(code)
            challenge.expires_at = timezone.now() + CODE_LIFETIME
            challenge.save(update_fields=["code_hash", "expires_at"])


def confirm_account_deletion(user: User, code: str) -> None:
    deleted = False
    with transaction.atomic():
        current = User.objects.select_for_update().get(pk=user.pk)
        challenge = (
            AccountDeletionChallenge.objects.select_for_update().filter(user=current).first()
        )
        if (
            challenge is not None
            and challenge.code_hash
            and challenge.expires_at is not None
            and challenge.expires_at > timezone.now()
            and challenge.failed_attempts < MAX_CODE_ATTEMPTS
        ):
            if check_password(code, challenge.code_hash):
                consume_email_code(challenge)
                # Protected finance references require this order. Every queryset is
                # owned by the locked user and commits or rolls back as one unit.
                RecurringSchedule.objects.filter(user=current).delete()
                MoneyTransaction.objects.filter(user=current).delete()
                Debt.objects.filter(user=current).delete()
                owned_accounts = Account.objects.filter(user=current)
                OpeningBalance.objects.filter(account__in=owned_accounts).delete()
                UserSettings.objects.filter(user=current).delete()
                owned_accounts.delete()
                Session.objects.filter(
                    session_key__in=SignedInSession.objects.filter(user=current).values(
                        "session_id"
                    )
                ).delete()
                EmailVerification.objects.filter(email=current.email).delete()
                Registration.objects.filter(email=current.email).delete()
                current.delete()
                deleted = True
            else:
                challenge.failed_attempts += 1
                if challenge.failed_attempts >= MAX_CODE_ATTEMPTS:
                    consume_email_code(challenge)
                challenge.save(update_fields=["failed_attempts"])
    # Commit failed-attempt state before returning the validation error.
    if not deleted:
        raise ValidationError({"code": INVALID_CODE_MESSAGE})
