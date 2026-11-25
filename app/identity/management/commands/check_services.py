from django.conf import settings
from django.core.mail import get_connection
from django.core.management.base import BaseCommand, CommandError
from django.db import connection


class Command(BaseCommand):
    help = "Check PostgreSQL and Brevo connectivity without sending email."

    def handle(self, *args, **options) -> None:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
        except Exception:
            raise CommandError("PostgreSQL check failed; verify configuration.") from None
        self.stdout.write("PostgreSQL: connected.")
        if settings.EMAIL_TRANSPORT == "brevo_api":
            if not settings.PENNY_BREVO_API_KEY or not settings.DEFAULT_FROM_EMAIL:
                raise CommandError("Configure the Brevo API key and verified sender.")
            self.stdout.write("Brevo API: configured. Connectivity and delivery remain unverified.")
        else:
            if not all(
                [
                    settings.EMAIL_HOST_USER,
                    settings.EMAIL_HOST_PASSWORD,
                    settings.DEFAULT_FROM_EMAIL,
                ]
            ):
                raise CommandError("Configure the Brevo SMTP login, key, and sender.")
            try:
                with get_connection():
                    pass
            except Exception:
                raise CommandError("Brevo SMTP check failed; verify configuration.") from None
            self.stdout.write("Brevo SMTP: connected. No email sent; delivery remains unverified.")
