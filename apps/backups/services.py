import csv
import io
import json
from datetime import timedelta

import pyzipper
from django.contrib.sessions.models import Session
from django.db import transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from rest_framework.exceptions import NotFound, ValidationError

from apps.accounts.models import DeviceSession, User
from apps.backups import snapshots
from apps.backups.models import FinancialJob, RestorePreview
from apps.finances.models import Operation, Portfolio
from apps.finances.queries import operation_query
from apps.finances.services.core import Conflict


def enqueue(
    portfolio: Portfolio,
    kind: str,
    *,
    preview: RestorePreview | None = None,
    device_id: int | None = None,
) -> dict:
    if (
        FinancialJob.objects.filter(
            owner_id=portfolio.owner_id, expires_at__gt=timezone.now()
        ).count()
        >= 10
        or FinancialJob.objects.filter(
            owner_id=portfolio.owner_id,
            state__in=["queued", "running"],
            expires_at__gt=timezone.now(),
        ).count()
        >= 2
    ):
        raise Conflict({"code": "job_limit", "detail": "Wait for existing jobs or their expiry."})
    job = FinancialJob.objects.create(
        owner_id=portfolio.owner_id,
        kind=kind,
        preview=preview,
        expected_revision=portfolio.revision + 1 if preview else None,
        request_device_id=device_id,
        expires_at=timezone.now() + timedelta(hours=1),
    )
    return {"job_id": str(job.pk), "state": job.state}


def csv_cell(value) -> str:
    text = str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else text


def csv_export(portfolio: Portfolio) -> str:
    if Operation.objects.filter(owner_id=portfolio.owner_id).count() > snapshots.MAX_ROWS:
        raise ValidationError("Export record limit exceeded.")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        ["operation_id", "created_at", "local_date", "timezone", "kind", "note", "allocations"]
    )
    for item in operation_query(
        Operation.objects.filter(owner_id=portfolio.owner_id).order_by("pk")
    ).iterator(chunk_size=500):
        allocations = [
            {
                "account_id": entry.balance.account_id,
                "currency": entry.balance.currency,
                "delta": str(entry.delta),
            }
            for entry in item.entries.all()
        ]
        writer.writerow(
            [
                csv_cell(value)
                for value in [
                    item.pk,
                    item.created_at.isoformat(),
                    item.local_date.isoformat(),
                    item.timezone,
                    item.kind,
                    item.note,
                    json.dumps(allocations),
                ]
            ]
        )
        if output.tell() > snapshots.MAX_DOCUMENT_BYTES:
            raise ValidationError("Export size limit exceeded.")
    return output.getvalue()


def process_one() -> bool:
    now = timezone.now()
    FinancialJob.objects.filter(expires_at__lte=now).update(
        result=None, state="failed", failure_code="expired_job"
    )
    FinancialJob.objects.filter(state="running", started_at__lt=now - timedelta(minutes=10)).update(
        result=None, state="failed", failure_code="interrupted_job"
    )
    RestorePreview.objects.filter(expires_at__lte=now).update(document={})
    # Claim briefly, then release the job lock before acquiring the universal user lock.
    with transaction.atomic():
        job = (
            FinancialJob.objects.select_for_update(skip_locked=True)
            .filter(state="queued", expires_at__gt=now)
            .order_by("created_at")
            .first()
        )
        if job is None:
            return False
        job.state = "running"
        job.started_at = now
        job.save(update_fields=["state", "started_at"])
        job_id, owner_id = job.pk, job.owner_id
    try:
        with transaction.atomic():
            owner = User.objects.select_for_update().get(pk=owner_id)
            if not owner.is_active:
                raise Conflict({"code": "inactive_owner"})
            portfolio = Portfolio.objects.get(owner_id=owner_id)
            job = FinancialJob.objects.select_for_update().get(pk=job_id)
            if job.state != "running" or job.expires_at <= timezone.now():
                raise Conflict({"code": "expired_job"})
            if job.kind == "restore":
                if (
                    job.request_device_id is None
                    or not DeviceSession.objects.filter(
                        pk=job.request_device_id,
                        user_id=owner_id,
                        expires_at__gt=timezone.now(),
                        session_key__in=Session.objects.filter(
                            expire_date__gt=timezone.now()
                        ).values("session_key"),
                    ).exists()
                ):
                    raise Conflict({"code": "ended_session"})
                preview = RestorePreview.objects.select_for_update().get(
                    pk=job.preview_id, owner_id=owner_id, consumed=True
                )
                if (
                    portfolio.revision != job.expected_revision
                    or preview.expires_at <= timezone.now()
                ):
                    raise Conflict({"code": "stale_restore"})
                snapshots.replace(portfolio, preview.document)
                job.result = {"restored": True, "revision": portfolio.revision}
                preview.document = {}
                preview.save(update_fields=["document"])
            elif job.kind == "backup":
                document = snapshots.snapshot(portfolio)
                job.result = {"document": document, "revision": portfolio.revision}
            else:
                job.result = {"csv": csv_export(portfolio), "revision": portfolio.revision}
            job.state = "ready"
            job.save(update_fields=["state", "result"])
    except Exception:
        # Never expose source data, database errors, passwords or traceback text in job state.
        FinancialJob.objects.filter(pk=job_id).update(
            state="failed", result=None, failure_code="job_failed"
        )
    return True


def owned_job(owner: User, job_id) -> FinancialJob:
    job = FinancialJob.objects.filter(owner=owner, pk=job_id).first()
    if job is None:
        raise NotFound()
    if job.expires_at <= timezone.now():
        raise Conflict({"code": "expired_job"})
    return job


@sensitive_variables()
def encrypt(document: dict, password: str) -> bytes:
    payload = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()
    if len(payload) > snapshots.MAX_DOCUMENT_BYTES:
        raise ValidationError("Backup size limit exceeded.")
    output = io.BytesIO()
    with pyzipper.AESZipFile(output, "w", compression=pyzipper.ZIP_DEFLATED) as archive:
        archive.setpassword(password.encode())
        archive.setencryption(pyzipper.WZ_AES, nbits=256, force_wz_aes_version=2)
        archive.writestr("financial-data.json", payload)
    return output.getvalue()


@sensitive_variables()
def decrypt(payload: bytes, password: str) -> dict:
    if len(payload) > snapshots.MAX_ARCHIVE_BYTES:
        raise ValidationError("Backup size limit exceeded.")
    try:
        with pyzipper.AESZipFile(io.BytesIO(payload)) as archive:
            files = archive.infolist()
            if (
                len(files) != 1
                or files[0].filename != "financial-data.json"
                or files[0].file_size > snapshots.MAX_DOCUMENT_BYTES
                or not files[0].flag_bits & 1
                or files[0].wz_aes_strength != 3
                or files[0].wz_aes_version != 2
            ):
                raise ValueError
            archive.setpassword(password.encode())
            with archive.open(files[0]) as stream:
                content = stream.read(snapshots.MAX_DOCUMENT_BYTES + 1)
                if len(content) > snapshots.MAX_DOCUMENT_BYTES:
                    raise ValueError
            document = json.loads(content)
        snapshots.validate(document)
        return document
    except (
        ValueError,
        TypeError,
        KeyError,
        OSError,
        RuntimeError,
        EOFError,
        RecursionError,
        pyzipper.BadZipFile,
        NotImplementedError,
    ):
        raise ValidationError("Backup password, format or integrity check failed.") from None


def preview_restore(portfolio: Portfolio, document: dict) -> dict:
    snapshots.validate(document)
    if (
        RestorePreview.objects.filter(
            owner_id=portfolio.owner_id, expires_at__gt=timezone.now()
        ).count()
        >= 5
    ):
        raise Conflict({"code": "preview_limit"})
    preview = RestorePreview.objects.create(
        owner_id=portfolio.owner_id,
        document=document,
        revision=portfolio.revision + 1,
        expires_at=timezone.now() + timedelta(minutes=10),
    )
    return {
        "preview_id": str(preview.pk),
        "expires_at": preview.expires_at.isoformat(),
        "replacement_counts": {name: len(rows) for name, rows in document["records"].items()},
        "current_counts": {
            name: model.objects.filter(**{ownership: portfolio.owner_id}).count()
            for name, (model, ownership, _) in snapshots.SPECS.items()
        },
        "warning": (
            "Confirmation replaces all financial records. Account credentials remain unchanged."
        ),
    }


def confirm_restore(portfolio: Portfolio, preview_id, device_id: int | None) -> dict:
    preview = (
        RestorePreview.objects.select_for_update()
        .filter(
            owner_id=portfolio.owner_id,
            pk=preview_id,
            consumed=False,
            expires_at__gt=timezone.now(),
        )
        .first()
    )
    if preview is None:
        raise Conflict({"code": "invalid_preview"})
    if preview.revision != portfolio.revision:
        raise Conflict({"code": "stale_preview"})
    if device_id is None:
        raise Conflict({"code": "device_session_required"})
    result = enqueue(portfolio, "restore", preview=preview, device_id=device_id)
    preview.consumed = True
    preview.save(update_fields=["consumed"])
    return result
