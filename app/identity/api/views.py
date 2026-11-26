from django.contrib.auth import logout
from django.http import HttpRequest, JsonResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from app.identity.api.serializers import (
    AccountDeletionConfirmSerializer,
    CodeSerializer,
    CurrentUserSerializer,
    LoginSerializer,
    PasswordResetCompleteSerializer,
    PasswordResetRequestSerializer,
    RegistrationSerializer,
    SignedInSessionSerializer,
    UserSettingsSerializer,
)
from app.identity.models import SignedInSession, User
from app.identity.permissions import IsVerifiedUser
from app.identity.services.account_deletion import (
    REQUEST_MESSAGE,
    confirm_account_deletion,
    request_account_deletion,
)
from app.identity.services.password_reset import (
    GENERIC_RESET_MESSAGE,
    complete_password_reset,
    request_password_reset,
    verify_reset_code,
)
from app.identity.services.registration import (
    GENERIC_REQUEST_MESSAGE,
    INVALID_CODE_MESSAGE,
    authenticate_user,
    resend_code,
    start_registration,
    verify_registration,
)
from app.identity.services.sessions import revoke_user_sessions, start_user_session
from app.identity.services.two_factor import begin_password_login, pending_login, valid_login
from app.identity.services.user_settings import read_user_settings, save_user_settings


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
        start_user_session(request._request, user)
        return Response(CurrentUserSerializer(user).data)


class CurrentUserView(ProtectedAPIView):
    def get(self, request: Request) -> Response:
        return Response(CurrentUserSerializer(request.user).data)


class SessionStateView(ProtectedAPIView):
    permission_classes = [AllowAny]

    def get(self, request: Request) -> Response:
        # A guest is a normal startup state; private identity still requires verification.
        if IsVerifiedUser().has_permission(request, self):
            return Response(
                {"user": CurrentUserSerializer(request.user).data, "two_factor_pending": False}
            )
        challenge = pending_login(request._request)
        return Response(
            {
                "user": None,
                "two_factor_pending": bool(challenge and valid_login(challenge, challenge.user)),
            }
        )


class UserSettingsView(ProtectedAPIView):
    def get(self, request: Request) -> Response:
        return Response(UserSettingsSerializer(read_user_settings(request.user)).data)

    def post(self, request: Request) -> Response:
        serializer = UserSettingsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = save_user_settings(request.user, serializer.validated_data)
        return Response(UserSettingsSerializer(values).data)


class LoginView(ProtectedAPIView):
    permission_classes = [AllowAny]

    def post(self, request: Request) -> Response:
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = authenticate_user(
            **serializer.validated_data, remote_address=request.META.get("REMOTE_ADDR", "unknown")
        )
        request.session.pop("registration_id", None)
        if begin_password_login(request._request, user):
            return Response({"two_factor_required": True}, status=202)
        return Response(CurrentUserSerializer(user).data)


class LogoutView(ProtectedAPIView):
    def post(self, request: Request) -> Response:
        logout(request._request)
        return Response(status=204)


class SessionListView(ProtectedAPIView):
    def get(self, request: Request) -> Response:
        generation = User.objects.values_list("session_generation", flat=True).get(
            pk=request.user.pk
        )
        sessions = SignedInSession.objects.filter(
            user=request.user, generation=generation, session__expire_date__gt=timezone.now()
        ).order_by("-last_active_at", "session_id")
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(sessions, request, view=self)
        serializer = SignedInSessionSerializer(page, many=True, context={"request": request})
        return paginator.get_paginated_response(serializer.data)


class LogoutAllView(ProtectedAPIView):
    def post(self, request: Request) -> Response:
        revoke_user_sessions(request.user)
        logout(request._request)
        return Response(status=204)


class AccountDeletionRequestView(ProtectedAPIView):
    def post(self, request: Request) -> Response:
        request_account_deletion(request.user, request.META.get("REMOTE_ADDR", "unknown"))
        return Response({"detail": REQUEST_MESSAGE}, status=202)


class AccountDeletionConfirmView(ProtectedAPIView):
    def post(self, request: Request) -> Response:
        serializer = AccountDeletionConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        confirm_account_deletion(request.user, serializer.validated_data["code"])
        logout(request._request)
        return Response(status=204)


class PasswordResetRequestView(ProtectedAPIView):
    permission_classes = [AllowAny]

    def post(self, request: Request) -> Response:
        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        generation = request_password_reset(
            serializer.validated_data["email"], request.META.get("REMOTE_ADDR", "unknown")
        )
        request.session["password_reset_generation"] = generation
        request.session.pop("password_reset_verified", None)
        return Response({"detail": GENERIC_RESET_MESSAGE}, status=202)


class PasswordResetVerifyView(ProtectedAPIView):
    permission_classes = [AllowAny]

    def post(self, request: Request) -> Response:
        serializer = CodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        verify_reset_code(
            request.session.get("password_reset_generation"), serializer.validated_data["code"]
        )
        request.session.cycle_key()
        request.session["password_reset_verified"] = True
        return Response(status=204)


class PasswordResetCompleteView(ProtectedAPIView):
    permission_classes = [AllowAny]

    def post(self, request: Request) -> Response:
        serializer = PasswordResetCompleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if not request.session.get("password_reset_verified"):
            raise ValidationError({"code": INVALID_CODE_MESSAGE})
        complete_password_reset(
            request.session.get("password_reset_generation"), serializer.validated_data["password"]
        )
        logout(request._request)
        return Response(status=204)
