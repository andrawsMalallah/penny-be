import copy
import csv
import io
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.accounts.models import User
from apps.backups import services, snapshots
from apps.backups.models import FinancialJob, RestorePreview
from apps.debts.models import Debt, Person
from apps.finances.models import Operation, Portfolio
from tests.financial_helpers import FinancialApiTestMixin

PASSWORD = "Synthetic backup passphrase 123"


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class BackupApiTests(FinancialApiTestMixin, TestCase):
    def test_database_readiness_over_real_postgres(self) -> None:
        response = self.client.get("/api/v1/ready/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_snapshot_record_and_byte_limits_are_enforced(self) -> None:
        portfolio = Portfolio.objects.get(owner=self.user)
        with patch("apps.backups.snapshots.MAX_ROWS", 1), self.assertRaises(ValidationError):
            snapshots.snapshot(portfolio)
        with (
            patch("apps.backups.snapshots.MAX_DOCUMENT_BYTES", 100),
            self.assertRaises(ValidationError),
        ):
            snapshots.snapshot(portfolio)

    def test_backup_transaction_amount_must_match_its_ledger(self) -> None:
        self.write("expenses", self.expense("2.00"))
        document = snapshots.snapshot(Portfolio.objects.get(owner=self.user))
        expense = next(row for row in document["records"]["operations"] if row["kind"] == "expense")
        expense["details"]["amount"] = "999.00"
        with self.assertRaises(ValidationError):
            snapshots.validate(document)

    def test_revoked_requesting_session_blocks_queued_restore(self) -> None:
        from apps.accounts.models import DeviceSession

        archive = self.archive()
        preview = self.preview(archive).json()["preview_id"]
        queued = self.backup_write(
            "restore/confirm", {"preview_id": preview, "confirm_replace": True}
        ).json()["job_id"]
        original_ids = list(Operation.objects.filter(owner=self.user).values_list("id", flat=True))
        DeviceSession.objects.filter(user=self.user).delete()
        services.process_one()
        self.assertEqual(FinancialJob.objects.get(pk=queued).state, "failed")
        self.assertEqual(
            list(Operation.objects.filter(owner=self.user).values_list("id", flat=True)),
            original_ids,
        )

    def backup_write(self, path: str, data: dict, *, multipart: bool = False):
        return self.client.post(
            f"/api/v1/backups/{path}/",
            data,
            format="multipart" if multipart else "json",
            HTTP_IDEMPOTENCY_KEY=str(uuid4()),
        )

    def archive(self) -> bytes:
        response = self.backup_write("jobs", {"kind": "backup"})
        self.assertEqual(response.status_code, 200)
        job_id = response.json()["job_id"]
        self.assertTrue(services.process_one())
        self.assertEqual(FinancialJob.objects.get(pk=job_id).state, "ready")
        downloaded = self.backup_write(
            f"jobs/{job_id}/backup", {"password": PASSWORD, "password_confirmation": PASSWORD}
        )
        self.assertEqual(downloaded.status_code, 200)
        return downloaded.content

    def preview(self, payload: bytes):
        return self.backup_write(
            "restore/preview",
            {
                "file": SimpleUploadedFile(
                    "arbitrary-name.zip", payload, content_type="application/zip"
                ),
                "password": PASSWORD,
            },
            multipart=True,
        )

    def test_encrypted_backup_round_trip_explicit_confirm_and_credentials_unchanged(self) -> None:
        key = str(uuid4())
        self.assertEqual(self.write("expenses", self.expense("5.00"), key).status_code, 200)
        self.assertEqual(
            self.write("expenses", self.expense("1.00", note="Private synthetic note")).status_code,
            200,
        )
        archive = self.archive()
        self.assertNotIn(b"Private synthetic note", archive)
        document = services.decrypt(archive, PASSWORD)
        self.assertNotIn("password", str(document).lower())
        original_hash = User.objects.get(pk=self.user.pk).password
        self.write("income", self.dated(amount="100.00", currency="IQD"))
        preview = self.preview(archive)
        self.assertEqual(preview.status_code, 200, preview.content)
        preview_id = preview.json()["preview_id"]
        self.assertEqual(
            self.backup_write(
                "restore/confirm", {"preview_id": preview_id, "confirm_replace": False}
            ).status_code,
            400,
        )
        confirmation = self.backup_write(
            "restore/confirm", {"preview_id": preview_id, "confirm_replace": True}
        )
        self.assertEqual(confirmation.status_code, 200)
        self.assertTrue(services.process_one())
        job = FinancialJob.objects.get(pk=confirmation.json()["job_id"])
        self.assertEqual(job.state, "ready")
        self.assertTrue(job.result["restored"])
        new_budget = self.user.account_set.get(kind="budget")
        self.assertEqual(self.amount(new_budget), Decimal("94"))
        self.assertEqual(User.objects.get(pk=self.user.pk).password, original_hash)
        self.assertEqual(self.client.get("/api/v1/auth/me/").status_code, 200)
        self.assertEqual(
            self.write(
                "expenses", {**self.expense("5.00"), "source_id": new_budget.pk}, key
            ).status_code,
            409,
        )
        self.reconcile()

    def test_wrong_password_tampering_unsupported_and_inconsistent_records_are_rejected(
        self,
    ) -> None:
        archive = self.archive()
        with self.assertRaises(ValidationError):
            services.decrypt(archive, "incorrect")
        with self.assertRaises(ValidationError):
            services.decrypt(archive[: len(archive) // 2], PASSWORD)
        document = services.decrypt(archive, PASSWORD)
        for mutation in ("version", "balance", "credential", "foreign_reference"):
            malformed = copy.deepcopy(document)
            if mutation == "version":
                malformed["version"] = 999
            elif mutation == "balance":
                malformed["records"]["balances"][0]["amount"] = "999.00"
            elif mutation == "credential":
                malformed["password"] = "must-not-be-imported"
            else:
                malformed["records"]["balances"][0]["account_id"] = 99999999
            self.assertEqual(self.preview(services.encrypt(malformed, PASSWORD)).status_code, 400)
        self.assertEqual(self.amount(self.budget), Decimal("100"))

    def test_preview_and_queued_restore_become_stale_after_intervening_writes(self) -> None:
        archive = self.archive()
        preview = self.preview(archive).json()["preview_id"]
        self.write("income", self.dated(amount="1.00", currency="IQD"))
        self.assertEqual(
            self.backup_write(
                "restore/confirm", {"preview_id": preview, "confirm_replace": True}
            ).status_code,
            409,
        )
        preview = self.preview(archive).json()["preview_id"]
        queued = self.backup_write(
            "restore/confirm", {"preview_id": preview, "confirm_replace": True}
        ).json()["job_id"]
        self.write("income", self.dated(amount="1.00", currency="IQD"))
        self.assertTrue(services.process_one())
        self.assertEqual(FinancialJob.objects.get(pk=queued).state, "failed")
        self.assertEqual(self.amount(self.budget), Decimal("102"))

    def test_failed_restore_rolls_back_entire_replacement(self) -> None:
        archive = self.archive()
        preview = self.preview(archive).json()["preview_id"]
        queued = self.backup_write(
            "restore/confirm", {"preview_id": preview, "confirm_replace": True}
        ).json()["job_id"]
        original_ids = list(Operation.objects.filter(owner=self.user).values_list("id", flat=True))
        with patch(
            "apps.backups.snapshots.Account.objects.create",
            side_effect=RuntimeError("synthetic failure"),
        ):
            self.assertTrue(services.process_one())
        self.assertEqual(FinancialJob.objects.get(pk=queued).state, "failed")
        self.assertEqual(
            list(Operation.objects.filter(owner=self.user).values_list("id", flat=True)),
            original_ids,
        )
        self.assertEqual(self.amount(self.budget), Decimal("100"))

    def test_jobs_and_previews_are_user_isolated_and_expire(self) -> None:
        archive = self.archive()
        preview_id = self.preview(archive).json()["preview_id"]
        job = FinancialJob.objects.filter(owner=self.user).first()
        self.client.force_authenticate(
            user=User.objects.create_user(username="BackupOther", password=None)
        )
        self.assertEqual(self.client.get(f"/api/v1/backups/jobs/{job.pk}/").status_code, 404)
        self.assertEqual(
            self.backup_write(
                "restore/confirm", {"preview_id": preview_id, "confirm_replace": True}
            ).status_code,
            409,
        )
        FinancialJob.objects.filter(pk=job.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        RestorePreview.objects.filter(pk=preview_id).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        services.process_one()
        job.refresh_from_db()
        self.assertIsNone(job.result)
        self.assertEqual(RestorePreview.objects.get(pk=preview_id).document, {})

    def test_csv_is_queued_formula_safe_and_contains_record_only_operations(self) -> None:
        self.write("expenses", self.expense("1.00", note="=DANGEROUS()"))
        queued = self.backup_write("jobs", {"kind": "csv"}).json()["job_id"]
        self.assertTrue(services.process_one())
        download = self.client.get(f"/api/v1/backups/jobs/{queued}/csv/")
        self.assertEqual(download.status_code, 200)
        rows = list(csv.DictReader(io.StringIO(download.content.decode())))
        self.assertIn("'=DANGEROUS()", [row["note"] for row in rows])
        self.assertEqual(download["Cache-Control"], "no-store")

    def test_snapshot_preserves_debt_and_reversal_graphs(self) -> None:
        person = Person.objects.create(owner=self.user, name="Example")
        from datetime import date

        from apps.debts import services as debt_services
        from apps.finances.services import core

        data = {
            "person_id": person.pk,
            "direction": "receivable",
            "currency": "IQD",
            "amount": Decimal("5.00"),
            "movement": "record_only",
            "local_date": date(2026, 11, 10),
            "timezone": "UTC",
        }
        core.execute(
            self.user,
            uuid4(),
            "debt.create",
            data,
            lambda portfolio: debt_services.create_debt(portfolio, data),
        )
        archive = self.archive()
        preview = self.preview(archive)
        self.assertEqual(preview.status_code, 200, preview.content)
        queued = self.backup_write(
            "restore/confirm", {"preview_id": preview.json()["preview_id"], "confirm_replace": True}
        ).json()["job_id"]
        services.process_one()
        self.assertEqual(FinancialJob.objects.get(pk=queued).state, "ready")
        self.assertEqual(Debt.objects.get(owner=self.user).remaining_amount, Decimal("5.00"))
