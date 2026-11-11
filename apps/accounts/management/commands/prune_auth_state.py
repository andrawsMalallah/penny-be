from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.accounts.models import AuthAttemptBucket, DeviceSession, MfaChallenge


class Command(BaseCommand):
    help = "Remove expired devices/MFA challenges and rate-limit buckets older than one day."

    def handle(self, *args, **options) -> None:
        now = timezone.now()
        attempts, _ = AuthAttemptBucket.objects.filter(
            window_started_at__lt=now - timedelta(days=1)
        ).delete()
        devices, _ = DeviceSession.objects.filter(expires_at__lte=now).delete()
        challenges, _ = MfaChallenge.objects.filter(expires_at__lte=now).delete()
        self.stdout.write(
            f"Removed {attempts} expired attempt buckets, {devices} device records "
            f"and {challenges} MFA challenges."
        )
