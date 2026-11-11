from django.conf import settings
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from drf_spectacular.openapi import AutoSchema
from drf_spectacular.utils import OpenApiParameter, OpenApiTypes
from rest_framework import serializers
from rest_framework.permissions import BasePermission

from apps.accounts.api import serializers as account_serializers


class LocalDocumentationPermission(BasePermission):
    def has_permission(self, request, view) -> bool:
        return getattr(settings, "PENNY_API_DOCS_ENABLED", False)


class DeviceSessionScheme(OpenApiAuthenticationExtension):
    target_class = "apps.accounts.authentication.DeviceSessionAuthentication"
    name = "pennySession"
    priority = 1

    def get_security_definition(self, auto_schema) -> dict:
        return {"type": "apiKey", "in": "cookie", "name": settings.SESSION_COOKIE_NAME}


class TokenResponse(serializers.Serializer):
    csrf_token = serializers.CharField()


class LoginResponse(TokenResponse):
    user = account_serializers.UserSerializer(required=False)
    mfa_required = serializers.BooleanField(required=False)
    expires_in = serializers.IntegerField(required=False)


class MfaStatusResponse(serializers.Serializer):
    enabled = serializers.BooleanField()
    recovery_codes_remaining = serializers.IntegerField(min_value=0, max_value=10)


class MfaSetupResponse(serializers.Serializer):
    manual_key = serializers.CharField()
    enrollment_uri = serializers.CharField()
    expires_in = serializers.IntegerField()


class RecoveryResponse(TokenResponse):
    recovery_codes = serializers.ListField(child=serializers.CharField())


class CommandResponse(serializers.Serializer):
    salary_operation_ids = serializers.ListField(child=serializers.IntegerField(), required=False)
    revision = serializers.IntegerField()
    operation_id = serializers.IntegerField(required=False)
    account_id = serializers.IntegerField(required=False)
    category_id = serializers.IntegerField(required=False)
    person_id = serializers.IntegerField(required=False)
    debt_id = serializers.IntegerField(required=False)
    event_id = serializers.IntegerField(required=False)
    active_month = serializers.DateField(required=False)
    remaining_amount = serializers.CharField(required=False)
    details = serializers.JSONField(required=False)
    replacement = serializers.JSONField(required=False)
    updated = serializers.BooleanField(required=False)
    job_id = serializers.UUIDField(required=False)
    state = serializers.CharField(required=False)


class RestorePreviewResponse(CommandResponse):
    preview_id = serializers.UUIDField()
    expires_at = serializers.DateTimeField()
    replacement_counts = serializers.DictField(child=serializers.IntegerField())
    current_counts = serializers.DictField(child=serializers.IntegerField())
    warning = serializers.CharField()


class PreviewResponse(serializers.Serializer):
    revision = serializers.IntegerField()
    allocations = serializers.ListField(child=serializers.DictField(), required=False)
    quote = serializers.DictField(required=False)


class JobStatusResponse(serializers.Serializer):
    job_id = serializers.UUIDField()
    kind = serializers.CharField()
    state = serializers.CharField()
    failure_code = serializers.CharField()
    expires_at = serializers.DateTimeField()
    snapshot_revision = serializers.IntegerField(allow_null=True)


class ApiSchema(AutoSchema):
    def get_override_parameters(self):
        parameters = super().get_override_parameters()
        name = type(self.view).__name__
        if self.method == "GET" and name in {"ActivityList", "ReportsView"}:
            parameters += [
                OpenApiParameter("start", OpenApiTypes.DATE),
                OpenApiParameter("end", OpenApiTypes.DATE),
                OpenApiParameter("currency", str, enum=["IQD", "USD"]),
                OpenApiParameter("kind", str),
                OpenApiParameter("account_id", int),
                OpenApiParameter("category_id", int),
            ]
        if self.method == "GET" and name in {"AccountList", "CategoryList", "PersonList"}:
            parameters.append(OpenApiParameter("archived", bool))
        if self.method == "GET" and name == "DebtList":
            parameters += [
                OpenApiParameter("person_id", int),
                OpenApiParameter("currency", str, enum=["IQD", "USD"]),
                OpenApiParameter("direction", str, enum=["receivable", "payable"]),
                OpenApiParameter("status", str, enum=["active", "settled", "cancelled", "all"]),
            ]
        return parameters

    def get_response_serializers(self):
        name = type(self.view).__name__
        if name == "RegisterView":
            return {201: LoginResponse}
        mapped = {
            "CsrfView": TokenResponse,
            "LoginView": LoginResponse,
            "PasswordChangeView": LoginResponse,
            "MfaLoginView": LoginResponse,
            "MfaStatusView": MfaStatusResponse,
            "MfaSetupView": MfaSetupResponse,
            "MfaConfirmView": RecoveryResponse,
            "MfaRegenerateView": RecoveryResponse,
            "MfaDisableView": RecoveryResponse,
            "SpendPreviewView": PreviewResponse,
            "ExchangePreviewView": PreviewResponse,
            "RolloverPreviewView": PreviewResponse,
            "PreviewRestore": RestorePreviewResponse,
            "JobStatus": JobStatusResponse,
        }
        if name in mapped:
            return mapped[name]
        if name == "LogoutView":
            return {204: None}
        if name == "MeView":
            return account_serializers.UserSerializer
        if name == "HealthView":
            return {
                200: {
                    "type": "object",
                    "properties": {"status": {"type": "string", "enum": ["ok"]}},
                    "required": ["status"],
                }
            }
        if name in {"ReportsView", "DebtSummary"}:
            return {
                200: {
                    "type": "object",
                    "additionalProperties": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    },
                }
            }
        if name in {"DownloadBackup", "DownloadCsv"}:
            return {
                (200, "application/zip" if name == "DownloadBackup" else "text/csv"): {
                    "type": "string",
                    "format": "binary",
                }
            }
        from apps.finances.api.base import WriteView

        if isinstance(self.view, WriteView):
            if name == "PreferencesView" and self.method == "GET":
                from apps.finances.api.serializers import PreferencesSerializer

                return PreferencesSerializer
            return CommandResponse
        return super().get_response_serializers()


def request_headers(result, generator, request, public) -> dict:
    for path, methods in result["paths"].items():
        for method, operation in methods.items():
            if method != "post":
                continue
            headers = operation.setdefault("parameters", [])
            headers.append(
                {
                    "name": "X-CSRFToken",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string"},
                    "description": (
                        "Use csrf/ and keep its cookie. Tokens rotate on authentication changes."
                    ),
                }
            )
            needs_key = (
                path.startswith("/api/v1/debts/")
                or (path.startswith("/api/v1/finances/") and not path.endswith("-preview/"))
                or path
                in {
                    "/api/v1/backups/jobs/",
                    "/api/v1/backups/restore/preview/",
                    "/api/v1/backups/restore/confirm/",
                }
            )
            if needs_key:
                headers.append(
                    {
                        "name": "Idempotency-Key",
                        "in": "header",
                        "required": True,
                        "schema": {"type": "string", "format": "uuid"},
                    }
                )
            for status, description in [
                (400, "Invalid input."),
                (403, "Authentication, verification or CSRF rejected."),
                (409, "State conflict, stale confirmation or conflicting idempotency key."),
                (429, "Attempt limit reached."),
            ]:
                operation["responses"].setdefault(str(status), {"description": description})
    return result
