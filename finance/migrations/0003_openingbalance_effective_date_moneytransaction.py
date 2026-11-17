from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def fix_opening_dates(apps, schema_editor) -> None:
    OpeningBalance = apps.get_model("finance", "OpeningBalance")
    # Freeze existing recorded openings using each owner's saved IANA timezone.
    # Subsequent timezone changes must never move this financial boundary.
    pending = []
    manager = OpeningBalance.objects.using(schema_editor.connection.alias)
    for opening in manager.select_related("account__user").iterator(chunk_size=500):
        try:
            zone = ZoneInfo(opening.account.user.timezone)
        except ZoneInfoNotFoundError, ValueError:
            zone = ZoneInfo("UTC")
        opening.effective_date = opening.recorded_at.astimezone(zone).date()
        pending.append(opening)
        if len(pending) == 500:
            manager.bulk_update(pending, ["effective_date"], batch_size=500)
            pending.clear()
    if pending:
        manager.bulk_update(pending, ["effective_date"], batch_size=500)


class Migration(migrations.Migration):
    dependencies = [
        ("finance", "0002_categorysetup_expensecategory"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.AddField(
            model_name="openingbalance", name="effective_date", field=models.DateField(null=True)
        ),
        migrations.RunPython(fix_opening_dates, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="openingbalance", name="effective_date", field=models.DateField()
        ),
        migrations.CreateModel(
            name="MoneyTransaction",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[("income", "Income"), ("expense", "Expense")], max_length=7
                    ),
                ),
                (
                    "currency",
                    models.CharField(choices=[("IQD", "IQD"), ("USD", "USD")], max_length=3),
                ),
                ("amount", models.DecimalField(decimal_places=2, max_digits=18)),
                ("date", models.DateField()),
                ("note", models.CharField(blank=True, max_length=500)),
                ("version", models.PositiveIntegerField(default=1)),
                ("request_id", models.UUIDField()),
                ("creation_payload", models.JSONField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("deleted_at", models.DateTimeField(null=True)),
                (
                    "account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="transactions",
                        to="finance.account",
                    ),
                ),
                (
                    "category",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to="finance.expensecategory",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL
                    ),
                ),
            ],
            options={
                "ordering": ["-date", "-id"],
                "indexes": [
                    models.Index(
                        fields=["user", "deleted_at", "date", "id"],
                        name="finance_mon_user_id_6bd6fd_idx",
                    ),
                    models.Index(
                        fields=["account", "currency", "deleted_at", "date"],
                        name="finance_mon_account_dec6d1_idx",
                    ),
                    models.Index(
                        fields=["user", "kind", "date"], name="finance_mon_user_id_ffc28d_idx"
                    ),
                    models.Index(
                        fields=["user", "currency", "date"], name="finance_mon_user_id_54540e_idx"
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=["user", "request_id"], name="unique_transaction_request"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(amount__gt=0), name="transaction_positive_amount"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(currency__in=["IQD", "USD"]), name="transaction_currency"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(kind__in=["income", "expense"]), name="transaction_kind"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(kind="income", category__isnull=True)
                        | models.Q(kind="expense", category__isnull=False),
                        name="transaction_category_matches_kind",
                    ),
                ],
            },
        ),
    ]
