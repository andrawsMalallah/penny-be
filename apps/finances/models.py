from decimal import Decimal
from uuid import uuid4

from django.conf import settings
from django.db import models


class Currency(models.TextChoices):
    IQD = "IQD"
    USD = "USD"


class AccountKind(models.TextChoices):
    BUDGET = "budget"
    MAIN = "main_savings"
    NAMED = "named"


class Portfolio(models.Model):
    owner = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    active_month = models.DateField(null=True)
    revision = models.PositiveBigIntegerField(default=0)
    generation = models.UUIDField(default=uuid4)
    exchange_rate = models.DecimalField(max_digits=18, decimal_places=6, default=Decimal("1"))
    language = models.CharField(max_length=8, default="system")
    theme = models.CharField(max_length=8, default="system")

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(exchange_rate__gt=0), name="finance_positive_rate"
            )
        ]


class BudgetMonth(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    month = models.DateField()
    closed = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["owner", "month"], name="finance_owner_month")
        ]


class Account(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    kind = models.CharField(max_length=16, choices=AccountKind.choices)
    name = models.CharField(max_length=80)
    archived = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "kind"],
                condition=~models.Q(kind="named"),
                name="finance_system_account",
            ),
            models.CheckConstraint(
                condition=models.Q(kind__in=AccountKind.values), name="finance_account_kind"
            ),
        ]
        indexes = [models.Index(fields=["owner", "archived", "id"])]


class Balance(models.Model):
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="balances")
    currency = models.CharField(max_length=3, choices=Currency.choices)
    amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account", "currency"], name="finance_account_currency"
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gte=0), name="finance_nonnegative_balance"
            ),
            models.CheckConstraint(
                condition=models.Q(currency__in=Currency.values), name="finance_balance_currency"
            ),
        ]


class Category(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    name_en = models.CharField(max_length=80)
    name_ar = models.CharField(max_length=80)
    archived = models.BooleanField(default=False)

    class Meta:
        indexes = [models.Index(fields=["owner", "archived", "id"])]


class Operation(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    kind = models.CharField(max_length=24)
    local_date = models.DateField()
    timezone = models.CharField(max_length=64)
    note = models.CharField(max_length=1000, blank=True)
    category = models.ForeignKey(Category, null=True, on_delete=models.PROTECT)
    # Store the approved inputs and historical amounts, not a recalculatable current rate.
    details = models.JSONField(default=dict)
    reverses = models.OneToOneField(
        "self", null=True, on_delete=models.PROTECT, related_name="reversal"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["owner", "-created_at", "-id"]),
            models.Index(fields=["owner", "local_date", "kind"]),
        ]


class LedgerEntry(models.Model):
    operation = models.ForeignKey(Operation, on_delete=models.CASCADE, related_name="entries")
    balance = models.ForeignKey(Balance, on_delete=models.PROTECT, related_name="entries")
    delta = models.DecimalField(max_digits=18, decimal_places=2)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=~models.Q(delta=0), name="finance_nonzero_entry")
        ]


class CommandRecord(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    key = models.UUIDField()
    fingerprint = models.CharField(max_length=64)
    generation = models.UUIDField(default=uuid4)
    result = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["owner", "key"], name="finance_idempotency_key")
        ]
