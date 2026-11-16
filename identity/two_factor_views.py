from django.contrib.auth import logout
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response

from .models import TwoFactorSettings
from .serializers import (
    CurrentUserSerializer,
    FactorManagementSerializer,
    FactorProofSerializer,
    PasswordConfirmationSerializer,
)
from .two_factor import (
    begin_setup,
    cancel_setup,
    enable_two_factor,
    enforce_factor_limits,
    finish_login,
    invalid_factor,
    manage_two_factor,
    pending_login,
    valid_login,
)
from .views import ProtectedAPIView


class FactorStatusView(ProtectedAPIView):
    def get(self, request: Request) -> Response:
        record = TwoFactorSettings.objects.filter(user=request.user).first()
        return Response(
            {
                "enabled": bool(record and record.enabled),
                "recovery_remaining": len(record.recovery_hashes)
                if record and record.enabled
                else 0,
            }
        )


class FactorLoginView(ProtectedAPIView):
    permission_classes = [AllowAny]

    def get(self, request: Request) -> Response:
        challenge = pending_login(request._request)
        pending = bool(challenge and valid_login(challenge, challenge.user))
        return Response({"pending": pending})

    def post(self, request: Request) -> Response:
        serializer = FactorProofSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = finish_login(
            request._request,
            serializer.validated_data["otp"],
            serializer.validated_data["recovery"],
            request.META.get("REMOTE_ADDR", "unknown"),
        )
        return Response(CurrentUserSerializer(user).data)


class FactorLoginCancelView(ProtectedAPIView):
    permission_classes = [AllowAny]

    def post(self, request: Request) -> Response:
        logout(request._request)
        return Response(status=204)


class FactorSetupView(ProtectedAPIView):
    def post(self, request: Request) -> Response:
        serializer = PasswordConfirmationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        enforce_factor_limits(request.user.pk, request.META.get("REMOTE_ADDR", "unknown"))
        return Response(begin_setup(request._request, serializer.validated_data["password"]))


class FactorSetupCancelView(ProtectedAPIView):
    def post(self, request: Request) -> Response:
        cancel_setup(request._request)
        return Response(status=204)


class FactorEnableView(ProtectedAPIView):
    def post(self, request: Request) -> Response:
        serializer = FactorProofSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        enforce_factor_limits(request.user.pk, request.META.get("REMOTE_ADDR", "unknown"))
        if serializer.validated_data["recovery"]:
            raise invalid_factor()
        codes = enable_two_factor(request._request, serializer.validated_data["otp"])
        return Response({"recovery_codes": codes})


class FactorManageView(ProtectedAPIView):
    disable = False

    def post(self, request: Request) -> Response:
        serializer = FactorManagementSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        enforce_factor_limits(request.user.pk, request.META.get("REMOTE_ADDR", "unknown"))
        values = serializer.validated_data
        codes = manage_two_factor(
            request._request,
            values["password"],
            values["otp"],
            values["recovery"],
            disable=self.disable,
        )
        return Response(status=204) if self.disable else Response({"recovery_codes": codes})


class FactorDisableView(FactorManageView):
    disable = True
