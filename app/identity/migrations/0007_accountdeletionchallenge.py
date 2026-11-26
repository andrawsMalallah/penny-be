import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("identity", "0006_usersettings_default_account"),
    ]

    operations = [
        migrations.CreateModel(
            name="AccountDeletionChallenge",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("generation", models.UUIDField(default=uuid.uuid4)),
                ("code_hash", models.CharField(blank=True, max_length=128)),
                ("expires_at", models.DateTimeField(db_index=True, null=True)),
                ("failed_attempts", models.PositiveSmallIntegerField(default=0)),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL
                    ),
                ),
            ],
        ),
    ]
