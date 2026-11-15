"""Bounded maintenance command; no daemon, cache, or queue infrastructure."""

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from identity.models import EmailVerification


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
        expired = EmailVerification.objects.filter(expires_at__lte=cutoff).exclude(code_hash="")
        if options["dry_run"]:
            self.stdout.write(
                f"Expired code hashes eligible: {expired.count()}. Dry run; no changes."
            )
            return

        cleared = 0
        for _ in range(max_batches):
            identifiers = list(
                expired.order_by("expires_at", "pk").values_list("pk", flat=True)[:batch_size]
            )
            if not identifiers:
                break
            # Recheck expiry in the UPDATE: a concurrent resend may have refreshed a selected row.
            # Keep the coordination row so active requests cannot lose their lock target.
            cleared += expired.filter(pk__in=identifiers).update(code_hash="")

        self.stdout.write(f"Expired code hashes cleared: {cleared}.")
        if expired.exists():
            self.stdout.write(
                "Batch limit reached; remaining expired codes can be cleared next run."
            )
