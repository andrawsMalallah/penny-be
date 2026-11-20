import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("identity", "0004_twofactorlogin_twofactorsettings"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="UserSettings",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "default_currency",
                    models.CharField(
                        blank=True, choices=[("IQD", "IQD"), ("USD", "USD")], max_length=3
                    ),
                ),
                (
                    "exchange_rate",
                    models.DecimalField(blank=True, decimal_places=6, max_digits=18, null=True),
                ),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            ("exchange_rate__isnull", True),
                            ("exchange_rate__gt", 0),
                            _connector="OR",
                        ),
                        name="positive_settings_exchange_rate",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("default_currency__in", ["", "IQD", "USD"])),
                        name="supported_settings_currency",
                    ),
                ],
            },
        ),
    ]
