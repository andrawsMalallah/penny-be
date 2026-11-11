"""MFA transitions serialize on the user before touching devices, challenges or codes."""

from base64 import b32encode
from datetime import timedelta
from urllib.parse import quote, urlencode

from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.sessions.models import Session
from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.views.decorators.debug import sensitive_variables
from django_otp.util import random_hex
from rest_framework.exceptions import AuthenticationFailed, ValidationError

from apps.accounts.models import (
    AuthenticatorDevice,
    DeviceSession,
    MfaChallenge,
    RecoveryCode,
    User,
)
from apps.accounts.services.authentication import consume_attempts, establish_session


def current_device(request: HttpRequest, user: User) -> DeviceSession:
    device = (
        DeviceSession.objects.select_for_update()
        .filter(
            user=user,
            session_key=request.session.session_key,
            expires_at__gt=timezone.now(),
            session_key__in=Session.objects.filter(expire_date__gt=timezone.now()).values(
                "session_key"
            ),
        )
        .first()
    )
    if device is None or not user.is_active:
        raise AuthenticationFailed("Session expired. Sign in again.")
    return device


@sensitive_variables()
def require_password(user: User, password: str) -> None:
    if not user.check_password(password):
        raise AuthenticationFailed("Invalid verification.")


@sensitive_variables()
def replace_codes(user: User) -> list[str]:
    codes = [random_hex(16) for _ in range(10)]
    RecoveryCode.objects.filter(user=user).delete()
    RecoveryCode.objects.bulk_create(
        [RecoveryCode(user=user, code_hash=make_password(code)) for code in codes]
    )
    return codes


@sensitive_variables()
def require_proof(user: User, authenticator: AuthenticatorDevice, code: str) -> None:
    if len(code) == 6 and authenticator.verify_token(code):
        return
    # Avoid ten expensive password-hash checks for malformed or failed TOTP input.
    if len(code) != 32:
        raise AuthenticationFailed("Invalid verification.")
    for recovery in RecoveryCode.objects.filter(user=user).order_by("pk"):
        if check_password(code, recovery.code_hash):
            recovery.delete()
            return
    raise AuthenticationFailed("Invalid verification.")


def rotate_current(request: HttpRequest, device: DeviceSession) -> None:
    request.session.cycle_key()
    request.session.save()
    device.session_key = request.session.session_key
    device.save(update_fields=["session_key"])


@sensitive_variables()
def begin_enrollment(request: HttpRequest, user: User, password: str) -> dict:
    consume_attempts(request, "login", user.username)
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user.pk)
        device = current_device(request, user)
        require_password(user, password)
        authenticator = AuthenticatorDevice.objects.filter(user=user).first()
        if authenticator and authenticator.confirmed:
            raise ValidationError("Two-factor authentication is already enabled.")
        secret = random_hex(20)
        encrypted = Fernet(settings.PENNY_MFA_ENCRYPTION_KEY).encrypt(secret.encode()).decode()
        AuthenticatorDevice.objects.update_or_create(
            user=user,
            defaults={
                "name": "Authenticator",
                "confirmed": False,
                "encrypted_secret": encrypted,
                "last_step": -1,
                "enrollment_session": device,
                "enrollment_expires_at": timezone.now() + timedelta(minutes=5),
            },
        )
        manual_key = b32encode(bytes.fromhex(secret)).decode().rstrip("=")
        uri = (
            "otpauth://totp/"
            + quote(f"Penny:{user.username}", safe="")
            + "?"
            + urlencode(
                {
                    "secret": manual_key,
                    "issuer": "Penny",
                    "algorithm": "SHA1",
                    "digits": 6,
                    "period": 30,
                }
            )
        )
        return {"manual_key": manual_key, "enrollment_uri": uri, "expires_in": 300}


@sensitive_variables()
def confirm_enrollment(request: HttpRequest, user: User, code: str) -> list[str]:
    consume_attempts(request, "login", user.username)
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user.pk)
        device = current_device(request, user)
        authenticator = AuthenticatorDevice.objects.filter(
            user=user,
            confirmed=False,
            enrollment_session=device,
            enrollment_expires_at__gt=timezone.now(),
        ).first()
        if authenticator is None or not authenticator.verify_token(code):
            raise AuthenticationFailed("Invalid verification.")
        authenticator.confirmed = True
        authenticator.enrollment_session = None
        authenticator.save(update_fields=["confirmed", "enrollment_session"])
        codes = replace_codes(user)
        others = DeviceSession.objects.filter(user=user).exclude(pk=device.pk)
        Session.objects.filter(session_key__in=others.values("session_key")).delete()
        others.delete()
        MfaChallenge.objects.filter(user=user).delete()
        rotate_current(request, device)
        return codes


@sensitive_variables()
def complete_sign_in(request: HttpRequest, code: str) -> User:
    pending = (
        MfaChallenge.objects.select_related("user")
        .filter(
            session_key=request.session.session_key,
        )
        .first()
    )
    # Unknown/expired sessions still consume the peer's attempt budget.
    consume_attempts(request, "login", pending.user.username if pending else "")
    if pending is None:
        raise AuthenticationFailed("Invalid verification.")
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=pending.user_id)
        challenge = MfaChallenge.objects.filter(
            pk=pending.pk,
            session_key=request.session.session_key,
            expires_at__gt=timezone.now(),
            session_key__in=Session.objects.filter(expire_date__gt=timezone.now()).values(
                "session_key"
            ),
        ).first()
        authenticator = AuthenticatorDevice.objects.filter(user=user, confirmed=True).first()
        if (
            challenge is None
            or not user.is_active
            or authenticator is None
            or not constant_time_compare(challenge.auth_hash, user.get_session_auth_hash())
        ):
            raise AuthenticationFailed("Invalid verification.")
        require_proof(user, authenticator, code)
        challenge.delete()
        establish_session(request, user, challenge.device_name)
        return user


@sensitive_variables()
def manage_mfa(
    request: HttpRequest, user: User, password: str, code: str, *, disable: bool
) -> list[str]:
    consume_attempts(request, "login", user.username)
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user.pk)
        device = current_device(request, user)
        require_password(user, password)
        authenticator = AuthenticatorDevice.objects.filter(user=user, confirmed=True).first()
        if authenticator is None:
            raise AuthenticationFailed("Invalid verification.")
        require_proof(user, authenticator, code)
        if disable:
            RecoveryCode.objects.filter(user=user).delete()
            authenticator.delete()
            codes = []
        else:
            codes = replace_codes(user)
        MfaChallenge.objects.filter(user=user).delete()
        rotate_current(request, device)
        return codes
