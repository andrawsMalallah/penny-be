"""Registration state lives in PostgreSQL; email is sent without a worker."""

import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import authenticate
from django.contrib.auth.hashers import check_password, make_password
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac
from rest_framework.exceptions import APIException, ValidationError

from app.identity.errors import CodeRequestLimit as RequestLimitExceeded
from app.identity.errors import InvalidCredentials, LoginAttemptLimit, ServiceUnavailable
from app.identity.models import CodeRequestLimit, EmailVerification, Registration, User
from app.identity.services.email_codes import consume_email_code

GENERIC_REQUEST_MESSAGE = "If this address can be registered, a verification code has been sent."
INVALID_CODE_MESSAGE = "This code is invalid or expired. Request a new code and try again."
CODE_LIFETIME = timedelta(minutes=10)
MAX_CODE_ATTEMPTS = 5


def enforce_request_limit(
    identity: str,
    maximum: int,
    window_seconds: int = 3600,
    error_type: type[APIException] = RequestLimitExceeded,
) -> None:
    identity_hash = salted_hmac(
        "penny.code-request-limit", identity, algorithm="sha256"
    ).hexdigest()
    with transaction.atomic():
        CodeRequestLimit.objects.get_or_create(identity_hash=identity_hash)
        bucket = CodeRequestLimit.objects.select_for_update().get(identity_hash=identity_hash)
        now = timezone.now().timestamp()
        recent = [stamp for stamp in bucket.request_times if stamp > now - window_seconds]
        if len(recent) >= maximum:
            raise error_type()
        bucket.request_times = [*recent, now]
        bucket.save(update_fields=["request_times", "updated_at"])


def authenticate_user(email: str, password: str, remote_address: str) -> User:
    enforce_request_limit(f"login:ip:{remote_address}", 30, 900, LoginAttemptLimit)
    enforce_request_limit(f"login:email:{email}", 10, 900, LoginAttemptLimit)
    # Django's backend handles password checking and nonexistent-user timing mitigation.
    user = authenticate(email=email, password=password)
    if user is None or not user.email_verified:
        raise InvalidCredentials()
    return user


def lock_verification(email: str) -> EmailVerification:
    # Uniqueness handles simultaneous first requests; callers lock inside a transaction.
    EmailVerification.objects.get_or_create(email=email)
    return EmailVerification.objects.select_for_update().get(email=email)


def prepare_code(registration: Registration) -> tuple[str, uuid.UUID]:
    code = f"{secrets.randbelow(1_000_000):06d}"
    generation = uuid.uuid4()
    with transaction.atomic():
        challenge = lock_verification(registration.email)
        challenge.registration = registration
        challenge.generation = generation
        # Invalidate immediately, including when sending a replacement fails.
        challenge.code_hash = ""
        challenge.expires_at = None
        challenge.failed_attempts = 0
        challenge.save()
    return code, generation


def send_registration_code(registration: Registration, code: str, generation: uuid.UUID) -> None:
    if User.objects.filter(email=registration.email).exists():
        return
    if registration.language == "ar":
        subject = "رمز التحقق من بريدك في Penny"
        body = f"رمز التحقق الخاص بك في Penny هو: {code}\nينتهي خلال 10 دقائق. لا تشاركه مع أحد."
    else:
        subject = "Your Penny verification code"
        body = (
            f"Your Penny verification code is: {code}\n"
            "It expires in 10 minutes. Do not share it with anyone."
        )
    try:
        if send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [registration.email]) != 1:
            raise ServiceUnavailable()
    except Exception:
        # Provider exception text may contain private details.
        raise ServiceUnavailable() from None
    with transaction.atomic():
        challenge = lock_verification(registration.email)
        if challenge.generation != generation:
            # Another request superseded this email while the provider call was in flight.
            return
        challenge.code_hash = make_password(code)
        challenge.expires_at = timezone.now() + CODE_LIFETIME
        challenge.save(update_fields=["code_hash", "expires_at"])


def start_registration(data: dict, remote_address: str) -> Registration:
    # Commit counters separately: email failure must not allow unlimited requests.
    enforce_request_limit(f"ip:{remote_address}", 20)
    enforce_request_limit(f"email:{data['email']}", 3)
    registration = Registration.objects.create(
        email=data["email"],
        display_name=data["display_name"],
        password_hash=make_password(data["password"]),
        language=data["language"],
        timezone=data["timezone"],
    )
    code, generation = prepare_code(registration)
    send_registration_code(registration, code, generation)
    return registration


def resend_code(registration_id: str | None, remote_address: str) -> None:
    enforce_request_limit(f"ip:{remote_address}", 20)
    registration = (
        Registration.objects.filter(pk=registration_id).first() if registration_id else None
    )
    if registration is None:
        return
    enforce_request_limit(f"email:{registration.email}", 3)
    code, generation = prepare_code(registration)
    send_registration_code(registration, code, generation)


def verify_registration(registration_id: str | None, code: str) -> User:
    registration = (
        Registration.objects.filter(pk=registration_id).first() if registration_id else None
    )
    if registration is None:
        raise ValidationError({"code": INVALID_CODE_MESSAGE})
    user = None
    with transaction.atomic():
        challenge = lock_verification(registration.email)
        valid_challenge = (
            challenge.registration_id == registration.pk
            and challenge.code_hash
            and challenge.expires_at is not None
            and challenge.expires_at > timezone.now()
            and challenge.failed_attempts < MAX_CODE_ATTEMPTS
        )
        if valid_challenge:
            if not check_password(code, challenge.code_hash):
                challenge.failed_attempts += 1
                if challenge.failed_attempts >= MAX_CODE_ATTEMPTS:
                    challenge.code_hash = ""
                challenge.save(update_fields=["failed_attempts", "code_hash"])
            else:
                try:
                    with transaction.atomic():
                        user = User.objects.create(
                            email=registration.email,
                            display_name=registration.display_name,
                            password=registration.password_hash,
                            language=registration.language,
                            timezone=registration.timezone,
                            email_verified=True,
                        )
                except IntegrityError:
                    user = None
                # Code consumption, draft cleanup and user creation commit together.
                consume_email_code(challenge)
                registration.password_hash = ""
                registration.save(update_fields=["password_hash"])
    # Fail outside atomic so wrong-attempt increments remain committed.
    if user is None:
        raise ValidationError({"code": INVALID_CODE_MESSAGE})
    return user
