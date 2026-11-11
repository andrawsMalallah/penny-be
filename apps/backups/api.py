import hashlib
import json
from uuid import UUID

from django.http import HttpResponse
from django.utils.decorators import method_decorator
from django.views.decorators.debug import sensitive_post_parameters
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.api.serializers import StrictSerializer
from apps.accounts.models import DeviceSession
from apps.backups import services, snapshots
from apps.finances.api.base import PrivateResponse, WriteView
from apps.finances.models import Portfolio
from apps.finances.services.core import execute


class JobSerializer(StrictSerializer):
    kind = serializers.ChoiceField(choices=["backup", "csv"])


class PasswordSerializer(StrictSerializer):
    password = serializers.CharField(
        min_length=12, max_length=1024, trim_whitespace=False, write_only=True
    )
    password_confirmation = serializers.CharField(
        max_length=1024, trim_whitespace=False, write_only=True
    )

    def validate(self, attrs):
        if attrs["password"] != attrs["password_confirmation"]:
            raise ValidationError({"password_confirmation": "Passwords do not match."})
        return attrs


class PreviewSerializer(StrictSerializer):
    file = serializers.FileField(max_length=200)
    password = serializers.CharField(max_length=1024, trim_whitespace=False, write_only=True)

    def validate_file(self, value):
        if value.size > snapshots.MAX_ARCHIVE_BYTES or value.content_type not in {
            "application/zip",
            "application/x-zip-compressed",
            "application/octet-stream",
        }:
            raise ValidationError("Use a supported encrypted ZIP within the size limit.")
        return value


class ConfirmationSerializer(StrictSerializer):
    preview_id = serializers.UUIDField()
    confirm_replace = serializers.BooleanField()

    def validate_confirm_replace(self, value):
        if not value:
            raise ValidationError("Explicit replacement confirmation is required.")
        return value


class CreateJob(WriteView):
    serializer_class = JobSerializer
    action = "export.create"

    def work(self, portfolio: Portfolio, data: dict) -> dict:
        return services.enqueue(portfolio, data["kind"])


class JobStatus(PrivateResponse, APIView):
    def get(self, request: Request, pk) -> Response:
        job = services.owned_job(request.user, pk)
        return Response(
            {
                "job_id": str(job.pk),
                "kind": job.kind,
                "state": job.state,
                "failure_code": job.failure_code,
                "expires_at": job.expires_at.isoformat(),
                "snapshot_revision": job.result.get("revision") if job.result else None,
            }
        )


@method_decorator(sensitive_post_parameters("password", "password_confirmation"), name="dispatch")
class DownloadBackup(PrivateResponse, APIView):
    serializer_class = PasswordSerializer

    def post(self, request: Request, pk) -> HttpResponse:
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        job = services.owned_job(request.user, pk)
        if job.state != "ready" or job.kind != "backup":
            raise ValidationError("Backup job is not ready.")
        response = HttpResponse(
            services.encrypt(job.result["document"], serializer.validated_data["password"]),
            content_type="application/zip",
        )
        response["Content-Disposition"] = 'attachment; filename="penny-finances.zip"'
        return response


class DownloadCsv(PrivateResponse, APIView):
    def get(self, request: Request, pk) -> HttpResponse:
        job = services.owned_job(request.user, pk)
        if job.state != "ready" or job.kind != "csv":
            raise ValidationError("CSV job is not ready.")
        response = HttpResponse(job.result["csv"], content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="penny-activity.csv"'
        return response


@method_decorator(sensitive_post_parameters("password"), name="dispatch")
class PreviewRestore(WriteView):
    serializer_class = PreviewSerializer
    parser_classes = [MultiPartParser, FormParser]
    action = "restore.preview"

    def post(self, request: Request, **kwargs) -> Response:
        # Files/passwords never enter idempotency storage or its request fingerprint.
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        document = services.decrypt(
            serializer.validated_data["file"].read(snapshots.MAX_ARCHIVE_BYTES + 1),
            serializer.validated_data["password"],
        )
        try:
            key = UUID(request.headers.get("Idempotency-Key", ""))
        except ValueError:
            raise ValidationError("Supply a UUID Idempotency-Key.") from None
        digest = hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()
        result = execute(
            request.user,
            key,
            self.action,
            {"digest": digest},
            lambda portfolio: services.preview_restore(portfolio, document),
            device_id=request.auth.pk if isinstance(request.auth, DeviceSession) else None,
            session_key=request.session.session_key,
        )
        return Response(result)


class ConfirmRestore(WriteView):
    serializer_class = ConfirmationSerializer
    action = "restore.confirm"

    def work(self, portfolio: Portfolio, data: dict) -> dict:
        return services.confirm_restore(
            portfolio,
            data["preview_id"],
            self.request.auth.pk if isinstance(self.request.auth, DeviceSession) else None,
        )
