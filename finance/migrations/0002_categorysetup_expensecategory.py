import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models.functions import Lower


class Migration(migrations.Migration):
    dependencies = [
        ("finance", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="CategorySetup",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("initialized_at", models.DateTimeField(auto_now_add=True)),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="ExpenseCategory",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("name_en", models.CharField(blank=True, max_length=100)),
                ("name_ar", models.CharField(blank=True, max_length=100)),
                ("archived", models.BooleanField(default=False)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL
                    ),
                ),
            ],
            options={
                "ordering": ["id"],
                "indexes": [
                    models.Index(
                        fields=["user", "archived", "id"], name="finance_exp_user_id_9ddfeb_idx"
                    )
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=~models.Q(name_en="", name_ar=""), name="category_has_name"
                    ),
                    models.UniqueConstraint(
                        Lower("name_en"),
                        "user",
                        condition=~models.Q(name_en=""),
                        name="unique_category_english_name",
                    ),
                    models.UniqueConstraint(
                        Lower("name_ar"),
                        "user",
                        condition=~models.Q(name_ar=""),
                        name="unique_category_arabic_name",
                    ),
                ],
            },
        ),
    ]
