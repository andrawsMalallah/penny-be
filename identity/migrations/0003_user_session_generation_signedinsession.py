import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("identity", "0002_passwordreset"),
        ("sessions", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="session_generation",
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.CreateModel(
            name="SignedInSession",
            fields=[
                (
                    "session",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        primary_key=True,
                        serialize=False,
                        to="sessions.session",
                    ),
                ),
                ("generation", models.PositiveBigIntegerField()),
                ("browser", models.CharField(max_length=30)),
                ("device", models.CharField(max_length=30)),
                ("signed_in_at", models.DateTimeField(null=True)),
                ("last_active_at", models.DateTimeField()),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["user", "-last_active_at", "session"],
                        name="user_session_activity",
                    )
                ]
            },
        ),
    ]
