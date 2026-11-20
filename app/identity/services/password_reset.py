"""Email-code recovery with session-bound proof and transactional consumption."""

import secrets
import uuid

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from app.identity.models import PasswordReset, User
from app.identity.services.registration import (
    CODE_LIFETIME,
    INVALID_CODE_MESSAGE,
    MAX_CODE_ATTEMPTS,
    enforce_request_limit,
)
from app.identity.services.sessions import revoke_user_sessions

GENERIC_RESET_MESSAGE = (
    "If this address has an eligible account, a password reset code will be sent."
)


def request_password_reset(email: str, remote_address: str) -> str:
    # Counters commit before SMTP; failures cannot grant extra sends.
    enforce_request_limit(f"reset:ip:{remote_address}", 20)
    enforce_request_limit(f"reset:email:{email}", 3)
    generation = uuid.uuid4()
    user = User.objects.filter(email=email, is_active=True, email_verified=True).first()
    if user is None:
        return str(generation)
    code = f"{secrets.randbelow(1_000_000):06d}"
    with transaction.atomic():
        PasswordReset.objects.get_or_create(user=user)
        challenge = PasswordReset.objects.select_for_update().get(user=user)
        challenge.generation = generation
        challenge.code_hash = ""
        challenge.expires_at = None
        challenge.failed_attempts = 0
        challenge.verified = False
        challenge.save()
    if user.language == "ar":
        subject = "إعادة تعيين كلمة مرور Penny"
        body = (
            f"رمز إعادة تعيين كلمة المرور في Penny هو: {code}\n"
            "ينتهي خلال 10 دقائق. لا تشاركه مع أحد. "
            "إذا لم تطلب تغيير كلمة المرور، يمكنك تجاهل هذه الرسالة."
        )
    else:
        subject = "Reset your Penny password"
        body = (
            f"Your Penny password reset code is: {code}\n"
            "It expires in 10 minutes. Do not share it with anyone. "
            "If you did not request a password reset, you can ignore this email."
        )
    try:
        delivered = send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [user.email]) == 1
    except Exception:
        # Preserve the generic response for unknown addresses and failed delivery alike.
        delivered = False
    if delivered:
        with transaction.atomic():
            challenge = PasswordReset.objects.select_for_update().get(user=user)
            if challenge.generation == generation:
                challenge.code_hash = make_password(code)
                challenge.expires_at = timezone.now() + CODE_LIFETIME
                challenge.save(update_fields=["code_hash", "expires_at"])
    return str(generation)


def verify_reset_code(generation: str | None, code: str) -> None:
    verified = False
    with transaction.atomic():
        challenge = PasswordReset.objects.select_for_update().filter(generation=generation).first()
        if (
            challenge is not None
            and challenge.code_hash
            and challenge.expires_at is not None
            and challenge.expires_at > timezone.now()
            and challenge.failed_attempts < MAX_CODE_ATTEMPTS
        ):
            if check_password(code, challenge.code_hash):
                challenge.verified = True
                challenge.code_hash = ""
                verified = True
            else:
                challenge.failed_attempts += 1
                if challenge.failed_attempts >= MAX_CODE_ATTEMPTS:
                    challenge.code_hash = ""
            challenge.save(update_fields=["verified", "code_hash", "failed_attempts"])
    # Raise after committing so invalid attempts cannot roll back their own counter.
    if not verified:
        raise ValidationError({"code": INVALID_CODE_MESSAGE})


def complete_password_reset(generation: str | None, password: str) -> None:
    with transaction.atomic():
        challenge = PasswordReset.objects.select_for_update().filter(generation=generation).first()
        if (
            challenge is None
            or not challenge.verified
            or challenge.expires_at is None
            or challenge.expires_at <= timezone.now()
        ):
            raise ValidationError({"code": INVALID_CODE_MESSAGE})
        user = User.objects.select_for_update().get(pk=challenge.user_id)
        if not user.is_active or not user.email_verified:
            raise ValidationError({"code": INVALID_CODE_MESSAGE})
        try:
            validate_password(password, user)
        except DjangoValidationError as error:
            raise ValidationError({"password": error.messages}) from error
        if user.check_password(password):
            raise ValidationError(
                {"password": ["Choose a password different from your current one."]}
            )
        user.set_password(password)
        user.save(update_fields=["password"])
        revoke_user_sessions(user)
        # Django rejects old sessions by their auth hash on their next request.
        # Do not call update_session_auth_hash: recovery must sign out every device.
        challenge.verified = False
        challenge.code_hash = ""
        challenge.expires_at = None
        challenge.generation = uuid.uuid4()
        challenge.save()
