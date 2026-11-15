from django.contrib.auth import login, logout
from django.http import HttpRequest, JsonResponse
from django.middleware.csrf import get_token
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import (
    CodeSerializer,
    CurrentUserSerializer,
    LoginSerializer,
    RegistrationSerializer,
)
from .services import (
    GENERIC_REQUEST_MESSAGE,
    authenticate_user,
    resend_code,
    start_registration,
    verify_registration,
)


@require_GET
@never_cache
def health(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "ok", "service": "penny-api"})


@require_GET
@never_cache
def csrf_token(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"csrf_token": get_token(request)})


def csrf_failure(request: HttpRequest, reason: str = "") -> JsonResponse:
    response = JsonResponse({"detail": "Refresh the page and try again."}, status=403)
    response["Cache-Control"] = "no-store"
    return response


@method_decorator([csrf_protect, never_cache], name="dispatch")
class ProtectedAPIView(APIView):
    """Explicit CSRF protection also applies to anonymous registration/verification."""


class RegisterView(ProtectedAPIView):
    permission_classes = [AllowAny]

    def post(self, request: Request) -> Response:
        serializer = RegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        registration = start_registration(
            serializer.validated_data, request.META.get("REMOTE_ADDR", "unknown")
        )
        request.session["registration_id"] = str(registration.pk)
        return Response({"detail": GENERIC_REQUEST_MESSAGE}, status=202)


class ResendCodeView(ProtectedAPIView):
    permission_classes = [AllowAny]

    def post(self, request: Request) -> Response:
        resend_code(
            request.session.get("registration_id"), request.META.get("REMOTE_ADDR", "unknown")
        )
        return Response({"detail": GENERIC_REQUEST_MESSAGE}, status=202)


class VerifyEmailView(ProtectedAPIView):
    permission_classes = [AllowAny]

    def post(self, request: Request) -> Response:
        serializer = CodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = verify_registration(
            request.session.get("registration_id"), serializer.validated_data["code"]
        )
        request.session.pop("registration_id", None)
        login(request._request, user, backend="django.contrib.auth.backends.ModelBackend")
        return Response(CurrentUserSerializer(user).data)


class CurrentUserView(ProtectedAPIView):
    def get(self, request: Request) -> Response:
        return Response(CurrentUserSerializer(request.user).data)


class LoginView(ProtectedAPIView):
    permission_classes = [AllowAny]

    def post(self, request: Request) -> Response:
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = authenticate_user(
            **serializer.validated_data, remote_address=request.META.get("REMOTE_ADDR", "unknown")
        )
        request.session.pop("registration_id", None)
        login(request._request, user)
        return Response(CurrentUserSerializer(user).data)


class LogoutView(ProtectedAPIView):
    def post(self, request: Request) -> Response:
        logout(request._request)
        return Response(status=204)
