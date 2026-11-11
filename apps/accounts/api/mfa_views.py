from django.middleware.csrf import get_token
from django.views.decorators.debug import sensitive_variables
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import AuthenticatorDevice, RecoveryCode
from apps.accounts.services import mfa

from .serializers import (
    MfaCodeSerializer,
    MfaManagementSerializer,
    MfaSetupSerializer,
    UserSerializer,
)
from .views import PublicAuthView


class MfaStatusView(APIView):
    def get(self, request: Request) -> Response:
        return Response(
            {
                "enabled": AuthenticatorDevice.objects.filter(
                    user=request.user, confirmed=True
                ).exists(),
                "recovery_codes_remaining": RecoveryCode.objects.filter(user=request.user).count(),
            }
        )


class MfaSetupView(APIView):
    serializer_class = MfaSetupSerializer

    @sensitive_variables()
    def post(self, request: Request) -> Response:
        serializer = MfaSetupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(
            mfa.begin_enrollment(request._request, request.user, **serializer.validated_data)
        )


class MfaConfirmView(APIView):
    serializer_class = MfaCodeSerializer

    @sensitive_variables()
    def post(self, request: Request) -> Response:
        serializer = MfaCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        codes = mfa.confirm_enrollment(request._request, request.user, **serializer.validated_data)
        return Response({"recovery_codes": codes, "csrf_token": get_token(request._request)})


class MfaLoginView(PublicAuthView):
    serializer_class = MfaCodeSerializer

    @sensitive_variables()
    def post(self, request: Request) -> Response:
        serializer = MfaCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = mfa.complete_sign_in(request._request, **serializer.validated_data)
        return Response(
            {"user": UserSerializer(user).data, "csrf_token": get_token(request._request)}
        )


class MfaRegenerateView(APIView):
    serializer_class = MfaManagementSerializer
    disable = False

    @sensitive_variables()
    def post(self, request: Request) -> Response:
        serializer = MfaManagementSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        codes = mfa.manage_mfa(
            request._request, request.user, disable=self.disable, **serializer.validated_data
        )
        return Response({"recovery_codes": codes, "csrf_token": get_token(request._request)})


class MfaDisableView(MfaRegenerateView):
    disable = True
