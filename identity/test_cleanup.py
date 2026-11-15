from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from .models import CodeRequestLimit, EmailVerification, Registration


class ExpiredCodeCleanupTests(TestCase):
    def setUp(self) -> None:
        self.now = timezone.now()
        self.draft = Registration.objects.create(
            email="cleanup@example.com",
            display_name="Cleanup test",
            password_hash="draft-hash",
            language="en",
            timezone="UTC",
        )
        self.expired = EmailVerification.objects.create(
            email="expired@example.com",
            registration=self.draft,
            code_hash="expired-hash",
            expires_at=self.now,
            failed_attempts=3,
        )
        self.active = EmailVerification.objects.create(
            email="active@example.com",
            registration=self.draft,
            code_hash="active-hash",
            expires_at=self.now + timedelta(minutes=10),
        )

    def run_cleanup(self, *arguments: str) -> str:
        output = StringIO()
        call_command("purge_expired_codes", *arguments, stdout=output)
        return output.getvalue()

    def test_clears_only_expired_hashes_and_preserves_coordination_and_attempts(self) -> None:
        generation = self.expired.generation
        result = self.run_cleanup()
        self.expired.refresh_from_db()
        self.active.refresh_from_db()
        self.assertEqual(self.expired.code_hash, "")
        self.assertEqual(self.expired.failed_attempts, 3)
        self.assertEqual(self.expired.generation, generation)
        self.assertEqual(self.expired.registration_id, self.draft.pk)
        self.assertEqual(self.active.code_hash, "active-hash")
        self.assertIn("cleared: 1", result)

    def test_dry_run_changes_no_data_and_exposes_no_hash_or_address(self) -> None:
        result = self.run_cleanup("--dry-run")
        self.expired.refresh_from_db()
        self.assertEqual(self.expired.code_hash, "expired-hash")
        self.assertIn("eligible: 1", result)
        self.assertNotIn(self.expired.email, result)
        self.assertNotIn(self.expired.code_hash, result)

    def test_pending_sends_and_rate_limit_allowances_are_preserved(self) -> None:
        pending = EmailVerification.objects.create(email="pending@example.com", expires_at=None)
        bucket = CodeRequestLimit.objects.create(
            identity_hash="test-identity", request_times=[1, 2, 3]
        )
        self.run_cleanup()
        pending.refresh_from_db()
        bucket.refresh_from_db()
        self.draft.refresh_from_db()
        self.assertIsNone(pending.expires_at)
        self.assertEqual(bucket.request_times, [1, 2, 3])
        self.assertEqual(self.draft.password_hash, "draft-hash")

    def test_repeat_runs_are_idempotent(self) -> None:
        self.run_cleanup()
        self.assertIn("cleared: 0", self.run_cleanup())

    def test_batch_limit_bounds_work_and_later_run_finishes(self) -> None:
        for number in range(3):
            EmailVerification.objects.create(
                email=f"expired-{number}@example.com",
                code_hash="expired-hash",
                expires_at=self.now,
            )
        result = self.run_cleanup("--batch-size=2", "--max-batches=1")
        self.assertIn("cleared: 2", result)
        self.assertIn("Batch limit reached", result)
        self.assertIn("cleared: 2", self.run_cleanup())

    def test_invalid_batch_options_are_rejected(self) -> None:
        for argument in ["--batch-size=0", "--batch-size=5001", "--max-batches=0"]:
            with self.assertRaises(CommandError):
                self.run_cleanup(argument)
