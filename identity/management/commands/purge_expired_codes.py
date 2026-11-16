"""Bounded maintenance command; no daemon, cache, or queue infrastructure."""

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from identity.models import EmailVerification, PasswordReset


class Command(BaseCommand):
    help = (
        "Remove expired verification code hashes while retaining coordination and rate-limit state."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("--dry-run", action="store_true", help="Count eligible codes only.")
        parser.add_argument("--batch-size", type=int, default=500)
        parser.add_argument("--max-batches", type=int, default=20)

    def handle(self, *args, **options) -> None:
        batch_size = options["batch_size"]
        max_batches = options["max_batches"]
        if not 1 <= batch_size <= 5000:
            raise CommandError("--batch-size must be between 1 and 5000.")
        if not 1 <= max_batches <= 100:
            raise CommandError("--max-batches must be between 1 and 100.")

        cutoff = timezone.now()
        expired_sets = [
            model.objects.filter(expires_at__lte=cutoff).exclude(code_hash="")
            for model in (EmailVerification, PasswordReset)
        ]
        if options["dry_run"]:
            eligible_count = sum(expired.count() for expired in expired_sets)
            self.stdout.write(
                f"Expired code hashes eligible: {eligible_count}. Dry run; no changes."
            )
            return

        cleared = 0
        remaining_batches = max_batches
        for expired in expired_sets:
            for _ in range(remaining_batches):
                identifiers = list(
                    expired.order_by("expires_at", "pk").values_list("pk", flat=True)[:batch_size]
                )
                if not identifiers:
                    break
                # Recheck expiry during UPDATE; a resend may have refreshed a selected row.
                # Keep coordination rows so active requests cannot lose their lock target.
                cleared += expired.filter(pk__in=identifiers).update(code_hash="")
                remaining_batches -= 1

        self.stdout.write(f"Expired code hashes cleared: {cleared}.")
        if any(expired.exists() for expired in expired_sets):
            self.stdout.write(
                "Batch limit reached; remaining expired codes can be cleared next run."
            )
