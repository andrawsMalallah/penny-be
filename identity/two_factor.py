"""Encrypted authenticator secrets; framework-hashed, single-use recovery codes."""

import base64
import secrets
from datetime import timedelta

import pyotp
import qrcode
import qrcode.image.svg
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.contrib.auth import logout
from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
from django.http import HttpRequest
from django.middleware.csrf import rotate_token
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac
from pyotp.exceptions import OTPVerificationError
from rest_framework.exceptions import APIException, ValidationError

from .errors import InvalidCredentials
from .models import TwoFactorLogin, TwoFactorSettings, User
from .services import enforce_request_limit
from .sessions import revoke_user_sessions, start_user_session

LOGIN_KEY = "two_factor_login_id"
LIFETIME = timedelta(minutes=5)


class FactorAttemptLimit(APIException):
    status_code = 429
    default_detail = "Too many security attempts. Try again in 15 minutes."


class FactorUnavailable(APIException):
    status_code = 503
    default_detail = "Authenticator verification is temporarily unavailable."


def invalid_factor() -> ValidationError:
    return ValidationError({"otp": ["The code is invalid, expired or already used."]})


def encryption() -> Fernet:
    return Fernet(settings.TWO_FACTOR_ENCRYPTION_KEY.encode())


def decrypt_secret(ciphertext: str) -> str:
    try:
        return encryption().decrypt(ciphertext.encode()).decode()
    except InvalidToken, UnicodeError:
        # Do not expose ciphertext, configuration or library errors.
        raise FactorUnavailable() from None


def session_digest(request: HttpRequest) -> str:
    return salted_hmac("penny.two-factor-setup", request.session.session_key or "").hexdigest()


def enforce_factor_limits(user_id: int, remote_address: str) -> None:
    # Commit rate counters outside the action transaction, including on wrong proofs.
    enforce_request_limit(f"2fa:ip:{remote_address}", 30, 900, FactorAttemptLimit)
    enforce_request_limit(f"2fa:user:{user_id}", 10, 900, FactorAttemptLimit)


def password_matches(user: User, password: str) -> None:
    if not user.check_password(password):
        raise InvalidCredentials()


def lock_settings(user: User) -> TwoFactorSettings:
    # All callers hold the user row first, sharing one lock order with session revocation.
    record, _ = TwoFactorSettings.objects.get_or_create(user=user)
    return record


def current_user(user: User) -> User:
    current = User.objects.select_for_update().get(pk=user.pk)
    if not current.is_active or not current.email_verified:
        raise InvalidCredentials()
    return current


def begin_password_login(request: HttpRequest, user: User) -> bool:
    with transaction.atomic():
        current = current_user(user)
        if current.password != user.password:
            raise InvalidCredentials()
        record = TwoFactorSettings.objects.filter(user=current, enabled=True).first()
        if record is None:
            start_user_session(request, current)
            return False
        # A password-only browser must not keep an authenticated or previous challenge session.
        logout(request)
        request.session.cycle_key()
        challenge = TwoFactorLogin.objects.create(
            user=current,
            password_auth_hash=current.get_session_auth_hash(),
            session_generation=current.session_generation,
            expires_at=timezone.now() + LIFETIME,
        )
        request.session[LOGIN_KEY] = str(challenge.pk)
        rotate_token(request)
        return True


def pending_login(request: HttpRequest) -> TwoFactorLogin | None:
    challenge_id = request.session.get(LOGIN_KEY)
    if not challenge_id:
        return None
    return TwoFactorLogin.objects.filter(
        pk=challenge_id,
        consumed=False,
        expires_at__gt=timezone.now(),
        failed_attempts__lt=5,
        user__is_active=True,
        user__email_verified=True,
    ).first()


def valid_login(challenge: TwoFactorLogin, user: User) -> bool:
    return (
        challenge.session_generation == user.session_generation
        and constant_time_compare(challenge.password_auth_hash, user.get_session_auth_hash())
        and TwoFactorSettings.objects.filter(user=user, enabled=True).exists()
    )


def consume_factor(record: TwoFactorSettings, code: str, recovery: bool) -> bool:
    if not record.enabled:
        return False
    if recovery:
        # Fixed small list; only Django password hashes are stored, never plaintext codes.
        matching_index = None
        for index, code_hash in enumerate(record.recovery_hashes):
            if check_password(code, code_hash):
                matching_index = index
        if matching_index is None:
            return False
        record.recovery_hashes.pop(matching_index)
        record.save(update_fields=["recovery_hashes"])
        return True
    timecode = matching_timecode(decrypt_secret(record.secret_ciphertext), code)
    if timecode is None or timecode <= record.last_timecode:
        return False
    record.last_timecode = timecode
    record.save(update_fields=["last_timecode"])
    return True


def matching_timecode(secret: str, code: str) -> int | None:
    try:
        return pyotp.TOTP(secret).verify_and_get_timecode(code, valid_window=1)
    except OTPVerificationError:
        return None


def finish_login(request: HttpRequest, code: str, recovery: bool, remote_address: str) -> User:
    candidate = pending_login(request)
    if candidate is None:
        raise invalid_factor()
    enforce_factor_limits(candidate.user_id, remote_address)
    user = None
    with transaction.atomic():
        current = current_user(candidate.user)
        challenge = TwoFactorLogin.objects.select_for_update().filter(pk=candidate.pk).first()
        if (
            challenge is not None
            and not challenge.consumed
            and challenge.expires_at > timezone.now()
            and challenge.failed_attempts < 5
            and valid_login(challenge, current)
        ):
            record = lock_settings(current)
            if consume_factor(record, code, recovery):
                challenge.consumed = True
                user = current
                request.session.pop(LOGIN_KEY, None)
                start_user_session(request, current, two_factor_verified=True)
            else:
                challenge.failed_attempts += 1
            challenge.save(update_fields=["consumed", "failed_attempts"])
    if user is None:
        raise invalid_factor()
    return user


def begin_setup(request: HttpRequest, password: str) -> dict:
    with transaction.atomic():
        user = current_user(request.user)
        password_matches(user, password)
        record = lock_settings(user)
        if record.enabled:
            raise ValidationError({"otp": ["Two-factor authentication is already enabled."]})
        secret = pyotp.random_base32()
        record.pending_ciphertext = encryption().encrypt(secret.encode()).decode()
        record.pending_expires_at = timezone.now() + timedelta(minutes=10)
        record.pending_session_digest = session_digest(request)
        record.save(
            update_fields=["pending_ciphertext", "pending_expires_at", "pending_session_digest"]
        )
        uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name="Penny")
        svg = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage).to_string()
        return {
            "secret": secret,
            "qr_code": "data:image/svg+xml;base64," + base64.b64encode(svg).decode(),
        }


def replace_recovery_codes(record: TwoFactorSettings) -> list[str]:
    codes = [secrets.token_hex(8) for _ in range(10)]
    record.recovery_hashes = [make_password(code) for code in codes]
    record.save(update_fields=["recovery_hashes"])
    return codes


def renew_current_session(request: HttpRequest, user: User) -> None:
    revoke_user_sessions(user)
    request.session.flush()
    start_user_session(request, user, two_factor_verified=True)


def enable_two_factor(request: HttpRequest, code: str) -> list[str]:
    codes = None
    with transaction.atomic():
        user = current_user(request.user)
        record = lock_settings(user)
        pending_valid = (
            not record.enabled
            and record.pending_ciphertext
            and record.pending_expires_at
            and record.pending_expires_at > timezone.now()
            and constant_time_compare(record.pending_session_digest, session_digest(request))
        )
        if pending_valid:
            timecode = matching_timecode(decrypt_secret(record.pending_ciphertext), code)
            if timecode is not None:
                record.secret_ciphertext = record.pending_ciphertext
                record.enabled = True
                record.last_timecode = timecode
                record.pending_ciphertext = ""
                record.pending_session_digest = ""
                record.pending_expires_at = None
                record.save()
                codes = replace_recovery_codes(record)
                renew_current_session(request, user)
    if codes is None:
        raise invalid_factor()
    return codes


def manage_two_factor(
    request: HttpRequest, password: str, code: str, recovery: bool, *, disable: bool
) -> list[str] | None:
    accepted = False
    codes = None
    with transaction.atomic():
        user = current_user(request.user)
        password_matches(user, password)
        record = lock_settings(user)
        if consume_factor(record, code, recovery):
            accepted = True
            if disable:
                record.enabled = False
                record.secret_ciphertext = ""
                record.recovery_hashes = []
                record.last_timecode = -1
                record.save()
            else:
                codes = replace_recovery_codes(record)
            renew_current_session(request, user)
    if not accepted:
        raise invalid_factor()
    return codes


def cancel_setup(request: HttpRequest) -> None:
    with transaction.atomic():
        user = current_user(request.user)
        record = lock_settings(user)
        if constant_time_compare(record.pending_session_digest, session_digest(request)):
            record.pending_ciphertext = ""
            record.pending_expires_at = None
            record.pending_session_digest = ""
            record.save(
                update_fields=["pending_ciphertext", "pending_expires_at", "pending_session_digest"]
            )
