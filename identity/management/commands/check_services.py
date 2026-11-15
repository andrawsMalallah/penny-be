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
            raise CommandError("PostgreSQL check failed; verify local configuration.") from None
        self.stdout.write("PostgreSQL: connected.")
        if not all(
            [settings.EMAIL_HOST_USER, settings.EMAIL_HOST_PASSWORD, settings.DEFAULT_FROM_EMAIL]
        ):
            raise CommandError("Fill in Brevo SMTP login/key and sender in backend/.env first.")
        try:
            with get_connection():
                pass
        except Exception:
            raise CommandError(
                "Brevo check failed; verify SMTP credentials/network access."
            ) from None
        self.stdout.write(
            "Brevo: SMTP connected. No email sent; sender delivery remains unverified."
        )
