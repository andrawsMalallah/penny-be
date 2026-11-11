from datetime import timedelta

from django.conf import settings
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.password_validation import validate_password
from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.http import HttpRequest
from django.utils import timezone
from django.utils.crypto import salted_hmac
from rest_framework.exceptions import APIException, AuthenticationFailed, ValidationError

from apps.accounts.models import (
    AuthAttemptBucket,
    AuthenticatorDevice,
    DeviceSession,
    MfaChallenge,
    User,
)


class AuthRateLimited(APIException):
    status_code = 429
    default_detail = "Too many attempts. Try again later."
    default_code = "rate_limited"


def consume_attempts(request: HttpRequest, action: str, username: str = "") -> None:
    address = request.META.get("REMOTE_ADDR", "unknown")
    # Forwarded headers are intentionally ignored until an actual trusted proxy is configured.
    rules = [
        (
            f"{action}:ip:{address}",
            settings.AUTH_REGISTRATION_IP_LIMIT,
            settings.AUTH_REGISTRATION_WINDOW_SECONDS,
        )
    ]
    if action == "login":
        rules = [
            (
                f"login:account:{username.lower()}",
                settings.AUTH_LOGIN_ACCOUNT_LIMIT,
                settings.AUTH_LOGIN_WINDOW_SECONDS,
            ),
            (
                f"login:ip:{address}",
                settings.AUTH_LOGIN_IP_LIMIT,
                settings.AUTH_LOGIN_WINDOW_SECONDS,
            ),
        ]
    now = timezone.now()
    with transaction.atomic():
        for value, limit, window in sorted(rules):
            key = salted_hmac("penny.auth.attempt", value, algorithm="sha256").hexdigest()
            bucket, _ = AuthAttemptBucket.objects.get_or_create(
                key=key, defaults={"window_started_at": now}
            )
            bucket = AuthAttemptBucket.objects.select_for_update().get(pk=bucket.pk)
            if now >= bucket.window_started_at + timedelta(seconds=window):
                bucket.attempts = 0
                bucket.window_started_at = now
            if bucket.attempts >= limit:
                raise AuthRateLimited()
            bucket.attempts += 1
            bucket.save(update_fields=["attempts", "window_started_at"])


def establish_session(request: HttpRequest, user: User, device_name: str) -> None:
    old_key = request.session.session_key
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    # login() only rotates automatically when changing identity or an anonymous session.
    # Always rotate for a fresh sign-in, including a repeat login of the same account.
    if request.session.session_key == old_key:
        request.session.cycle_key()
    request.session.set_expiry(settings.SESSION_COOKIE_AGE)
    request.session.save()
    if old_key:
        DeviceSession.objects.filter(session_key=old_key).delete()
    now = timezone.now()
    DeviceSession.objects.create(
        user=user,
        session_key=request.session.session_key,
        device_name=device_name,
        last_active_at=now,
        expires_at=now + timedelta(seconds=settings.SESSION_COOKIE_AGE),
    )


def register(
    request: HttpRequest, username: str, password: str, display_name: str, device_name: str
) -> User:
    consume_attempts(request, "register")
    user = User(username=username, display_name=display_name)
    try:
        validate_password(password, user)
    except DjangoValidationError as error:
        raise ValidationError({"password": error.messages}) from None
    try:
        with transaction.atomic():
            user.set_password(password)
            user.save()
            establish_session(request, user, device_name)
    except IntegrityError as error:
        constraint = getattr(getattr(error.__cause__, "diag", None), "constraint_name", "")
        if constraint in {"accounts_username_case_insensitive", "accounts_user_username_key"}:
            raise ValidationError({"username": ["This username is unavailable."]}) from None
        raise
    return user


def sign_in(request: HttpRequest, username: str, password: str, device_name: str) -> User | None:
    consume_attempts(request, "login", username)
    with transaction.atomic():
        # Serialize with password changes and future MFA activation before issuing a session.
        User.objects.for_username(username).select_for_update().first()
        user = authenticate(request, username=username, password=password)
        if user is None:
            raise AuthenticationFailed("Invalid username or password.")
        if AuthenticatorDevice.objects.filter(user=user, confirmed=True).exists():
            old_key = request.session.session_key
            if old_key:
                DeviceSession.objects.filter(session_key=old_key).delete()
                MfaChallenge.objects.filter(session_key=old_key).delete()
            logout(request)
            request.session.create()
            request.session.set_expiry(300)
            request.session.save()
            MfaChallenge.objects.create(
                user=user,
                session_key=request.session.session_key,
                auth_hash=user.get_session_auth_hash(),
                device_name=device_name,
                expires_at=timezone.now() + timedelta(minutes=5),
            )
            return None
        establish_session(request, user, device_name)
    return user


def sign_out(request: HttpRequest, user: User) -> None:
    with transaction.atomic():
        User.objects.select_for_update().get(pk=user.pk)
        DeviceSession.objects.filter(user=user, session_key=request.session.session_key).delete()
        logout(request)


def change_password(
    request: HttpRequest, user: User, current_password: str, new_password: str
) -> User:
    consume_attempts(request, "login", user.username)
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user.pk)
        device = (
            DeviceSession.objects.select_for_update()
            .filter(
                user=user, session_key=request.session.session_key, expires_at__gt=timezone.now()
            )
            .first()
        )
        if device is None:
            raise AuthenticationFailed("Session expired. Sign in again.")
        if not user.check_password(current_password):
            raise ValidationError({"current_password": ["Incorrect password."]})
        try:
            validate_password(new_password, user)
        except DjangoValidationError as error:
            raise ValidationError({"new_password": error.messages}) from None
        user.set_password(new_password)
        user.save(update_fields=["password"])
        MfaChallenge.objects.filter(user=user).delete()
        other_devices = DeviceSession.objects.filter(user=user).exclude(pk=device.pk)
        Session.objects.filter(session_key__in=other_devices.values("session_key")).delete()
        other_devices.delete()
        update_session_auth_hash(request, user)
        request.session.save()
        device.session_key = request.session.session_key
        device.save(update_fields=["session_key"])
        request.user = user
    return user
