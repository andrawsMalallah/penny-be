import uuid

from django.conf import settings
from django.db import models


class RestorePreview(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    document = models.JSONField()
    revision = models.PositiveBigIntegerField()
    expires_at = models.DateTimeField(db_index=True)
    consumed = models.BooleanField(default=False)


class FinancialJob(models.Model):
    class Kind(models.TextChoices):
        BACKUP = "backup"
        CSV = "csv"
        RESTORE = "restore"

    class State(models.TextChoices):
        QUEUED = "queued"
        RUNNING = "running"
        READY = "ready"
        FAILED = "failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    kind = models.CharField(max_length=8, choices=Kind.choices)
    state = models.CharField(max_length=8, choices=State.choices, default=State.QUEUED)
    preview = models.ForeignKey(RestorePreview, null=True, on_delete=models.PROTECT)
    expected_revision = models.PositiveBigIntegerField(null=True)
    request_device_id = models.PositiveBigIntegerField(null=True)
    result = models.JSONField(null=True)
    failure_code = models.CharField(max_length=32, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    started_at = models.DateTimeField(null=True)

    class Meta:
        indexes = [
            models.Index(fields=["state", "created_at"]),
            models.Index(fields=["owner", "created_at"]),
        ]
