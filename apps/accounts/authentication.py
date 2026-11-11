from django.utils import timezone
from rest_framework.authentication import SessionAuthentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.request import Request

from .models import DeviceSession


class DeviceSessionAuthentication(SessionAuthentication):
    def authenticate(self, request: Request):
        authenticated = super().authenticate(request)
        if authenticated is None:
            return None
        user, _ = authenticated
        device = DeviceSession.objects.filter(
            user=user, session_key=request.session.session_key, expires_at__gt=timezone.now()
        ).first()
        if device is None:
            request.session.flush()
            raise AuthenticationFailed("Session expired. Sign in again.")
        request._request.penny_device_id = device.pk
        return user, device
