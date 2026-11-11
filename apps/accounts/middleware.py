from collections.abc import Callable
from datetime import timedelta

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.utils import timezone

from .models import DeviceSession


class SessionActivityMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        if request.path.startswith("/api/v1/auth/"):
            response["Cache-Control"] = "no-store"
        device_id = getattr(request, "penny_device_id", None)
        if response.status_code < 400 and device_id and request.session.session_key:
            now = timezone.now()
            updated = DeviceSession.objects.filter(
                pk=device_id,
                user=request.user,
                session_key=request.session.session_key,
                expires_at__gt=now,
            ).update(
                last_active_at=now, expires_at=now + timedelta(seconds=settings.SESSION_COOKIE_AGE)
            )
            if updated:
                request.session.set_expiry(settings.SESSION_COOKIE_AGE)
        return response
