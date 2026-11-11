from django.contrib.sessions.models import Session
from django.http import HttpRequest, JsonResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect
from rest_framework.generics import ListAPIView
from rest_framework.pagination import CursorPagination
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import DeviceSession
from apps.accounts.services import authentication as services

from .serializers import (
    DeviceSessionSerializer,
    LoginSerializer,
    PasswordChangeSerializer,
    RegistrationSerializer,
    UserSerializer,
)


def csrf_failure(request: HttpRequest, reason: str = "") -> JsonResponse:
    return JsonResponse({"detail": "CSRF verification failed."}, status=403)


class CsrfView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request: Request) -> Response:
        return Response(
            {"csrf_token": get_token(request._request)}, headers={"Cache-Control": "no-store"}
        )


@method_decorator(csrf_protect, name="dispatch")
class PublicAuthView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]


class RegisterView(PublicAuthView):
    serializer_class = RegistrationSerializer

    def post(self, request: Request) -> Response:
        serializer = RegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = services.register(request._request, **serializer.validated_data)
        return Response(
            {"user": UserSerializer(user).data, "csrf_token": get_token(request._request)},
            status=201,
        )


class LoginView(PublicAuthView):
    serializer_class = LoginSerializer

    def post(self, request: Request) -> Response:
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = services.sign_in(request._request, **serializer.validated_data)
        if user is None:
            return Response(
                {"mfa_required": True, "expires_in": 300, "csrf_token": get_token(request._request)}
            )
        return Response(
            {"user": UserSerializer(user).data, "csrf_token": get_token(request._request)}
        )


class MeView(APIView):
    def get(self, request: Request) -> Response:
        return Response(UserSerializer(request.user).data)


class LogoutView(APIView):
    # Preserve the published schema during cleanup; the runtime logout ignores the body.
    serializer_class = LoginSerializer

    def post(self, request: Request) -> Response:
        services.sign_out(request._request, request.user)
        return Response(status=204)


class PasswordChangeView(APIView):
    serializer_class = PasswordChangeSerializer

    def post(self, request: Request) -> Response:
        serializer = PasswordChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = services.change_password(request._request, request.user, **serializer.validated_data)
        return Response(
            {"user": UserSerializer(user).data, "csrf_token": get_token(request._request)}
        )


class DevicePagination(CursorPagination):
    ordering = ("-created_at", "-id")


class DevicesView(ListAPIView):
    serializer_class = DeviceSessionSerializer
    pagination_class = DevicePagination

    def get_queryset(self):
        now = timezone.now()
        return DeviceSession.objects.filter(
            user=self.request.user,
            expires_at__gt=now,
            session_key__in=Session.objects.filter(expire_date__gt=now).values("session_key"),
        )
