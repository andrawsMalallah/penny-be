"""Account-scoped revocation without changing passwords or inventing session tokens."""

from collections.abc import Callable

from django.contrib.auth import HASH_SESSION_KEY, login, logout
from django.contrib.sessions.models import Session
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from app.identity.errors import InvalidCredentials
from app.identity.models import SignedInSession, TwoFactorSettings, User

GENERATION_KEY = "penny_session_generation"


def device_details(user_agent: str) -> tuple[str, str]:
    # Keep only conservative labels, never the raw, client-controlled header or IP.
    user_agent = user_agent[:1024]
    browser = "Unknown browser"
    for marker, label in [
        ("Edg", "Edge"),
        ("OPR/", "Opera"),
        ("Firefox/", "Firefox"),
        ("FxiOS/", "Firefox"),
        ("Chrome/", "Chrome"),
        ("CriOS/", "Chrome"),
        ("Safari/", "Safari"),
    ]:
        if marker in user_agent:
            browser = label
            break
    device = "Unknown device"
    for marker, label in [
        ("iPad", "iPad"),
        ("iPhone", "iPhone"),
        ("Android", "Android"),
        ("Windows", "Windows"),
        ("Macintosh", "Mac"),
        ("Linux", "Linux"),
    ]:
        if marker in user_agent:
            device = label
            break
    return browser, device


def start_user_session(
    request: HttpRequest, user: User, *, two_factor_verified: bool = False
) -> None:
    with transaction.atomic():
        current = User.objects.select_for_update().get(pk=user.pk)
        # Authentication may have begun just before a concurrent password reset.
        if current.password != user.password or not current.is_active or not current.email_verified:
            raise InvalidCredentials()
        if (
            not two_factor_verified
            and TwoFactorSettings.objects.filter(user=current, enabled=True).exists()
        ):
            raise InvalidCredentials()
        login(request, current, backend="django.contrib.auth.backends.ModelBackend")
        request.session[GENERATION_KEY] = current.session_generation
        request.session.save()
        browser, device = device_details(request.META.get("HTTP_USER_AGENT", ""))
        now = timezone.now()
        SignedInSession.objects.update_or_create(
            session_id=request.session.session_key,
            defaults={
                "user": current,
                "generation": current.session_generation,
                "browser": browser,
                "device": device,
                "signed_in_at": now,
                "last_active_at": now,
            },
        )


def revoke_user_sessions(user: User) -> None:
    with transaction.atomic():
        current = User.objects.select_for_update().get(pk=user.pk)
        current.session_generation += 1
        current.save(update_fields=["session_generation"])
        # The generation also rejects legacy/untracked and concurrently saved sessions.
        Session.objects.filter(
            session_key__in=SignedInSession.objects.filter(user=current).values("session_id")
        ).delete()


class SessionActivityMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.user.is_authenticated:
            with transaction.atomic():
                current = User.objects.select_for_update().get(pk=request.user.pk)
                valid = (
                    current.is_active
                    and current.email_verified
                    and request.session.get(GENERATION_KEY, 0) == current.session_generation
                    and constant_time_compare(
                        request.session.get(HASH_SESSION_KEY, ""), current.get_session_auth_hash()
                    )
                )
                if not valid:
                    logout(request)
                else:
                    request.user = current
                    browser, device = device_details(request.META.get("HTTP_USER_AGENT", ""))
                    # Legacy sessions are enrolled without inventing a sign-in time.
                    record, created = SignedInSession.objects.get_or_create(
                        session_id=request.session.session_key,
                        defaults={
                            "user": current,
                            "generation": current.session_generation,
                            "browser": browser,
                            "device": device,
                            "last_active_at": timezone.now(),
                        },
                    )
                    if not created:
                        # Browser updates/emulation changes must not leave stale descriptions.
                        # Missing or unrecognized headers must not erase known labels.
                        updates = {"last_active_at": timezone.now()}
                        if browser != "Unknown browser":
                            updates["browser"] = browser
                        if device != "Unknown device":
                            updates["device"] = device
                        SignedInSession.objects.filter(pk=record.pk).update(**updates)
        return self.get_response(request)
