from django.conf import settings
from django.db import models
from django.db.models.functions import Lower


class AccountGroup(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    archived = models.BooleanField(default=False)

    class Meta:
        ordering = ["name", "id"]
        indexes = [models.Index(fields=["user", "archived", "name", "id"])]
        constraints = [
            models.UniqueConstraint(Lower("name"), "user", name="unique_account_group_name")
        ]


class Account(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    group = models.ForeignKey(AccountGroup, null=True, on_delete=models.PROTECT)
    name = models.CharField(max_length=100)
    archived = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    target_iqd = models.DecimalField(max_digits=18, decimal_places=2, null=True)
    target_usd = models.DecimalField(max_digits=18, decimal_places=2, null=True)
    target_version = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["name", "id"]
        indexes = [models.Index(fields=["user", "archived", "name", "id"])]
        constraints = [
            models.UniqueConstraint(Lower("name"), "user", name="unique_money_account_name"),
            models.CheckConstraint(
                condition=models.Q(target_iqd__isnull=True) | models.Q(target_iqd__gt=0),
                name="account_target_iqd_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(target_usd__isnull=True) | models.Q(target_usd__gt=0),
                name="account_target_usd_positive",
            ),
        ]


class OpeningBalance(models.Model):
    """Creation-time money, separate from future income and expense activity."""

    account = models.OneToOneField(Account, on_delete=models.PROTECT, related_name="opening")
    iqd = models.DecimalField(max_digits=18, decimal_places=2)
    usd = models.DecimalField(max_digits=18, decimal_places=2)
    recorded_at = models.DateTimeField(auto_now_add=True)
    effective_date = models.DateField()

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(iqd__gte=0), name="opening_iqd_nonnegative"),
            models.CheckConstraint(condition=models.Q(usd__gte=0), name="opening_usd_nonnegative"),
        ]


class CategorySetup(models.Model):
    """Persist initialization separately so retired starters are never recreated."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    initialized_at = models.DateTimeField(auto_now_add=True)


class ExpenseCategory(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    name_en = models.CharField(max_length=100, blank=True)
    name_ar = models.CharField(max_length=100, blank=True)
    archived = models.BooleanField(default=False)

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["user", "archived", "id"])]
        constraints = [
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
        ]


class TransactionKind(models.TextChoices):
    INCOME = "income", "Income"
    EXPENSE = "expense", "Expense"
    TRANSFER = "transfer", "Transfer"
    EXCHANGE = "exchange", "Exchange"
    DEBT_IN = "debt_in", "Debt money received"
    DEBT_OUT = "debt_out", "Debt money paid"


class Currency(models.TextChoices):
    IQD = "IQD", "IQD"
    USD = "USD", "USD"


class DebtDirection(models.TextChoices):
    OWED_TO_ME = "owed_to_me", "Owed to me"
    I_OWE = "i_owe", "I owe"


class DebtOrigin(models.TextChoices):
    EXISTING = "existing", "Existing debt"
    MOVEMENT = "movement", "New lending or borrowing"


class DebtPerson(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    name = models.CharField(max_length=100)

    class Meta:
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(Lower("name"), "user", name="unique_debt_person_name")
        ]
        indexes = [models.Index(fields=["user", "name", "id"])]


class Debt(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    person = models.ForeignKey(DebtPerson, on_delete=models.PROTECT)
    direction = models.CharField(max_length=10, choices=DebtDirection.choices)
    origin = models.CharField(max_length=8, choices=DebtOrigin.choices)
    account = models.ForeignKey(Account, on_delete=models.PROTECT, null=True)
    currency = models.CharField(max_length=3, choices=Currency.choices)
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    date = models.DateField()
    note = models.CharField(max_length=500, blank=True)
    version = models.PositiveIntegerField(default=1)
    request_id = models.UUIDField()
    creation_payload = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True)

    class Meta:
        ordering = ["person__name", "-date", "-id"]
        indexes = [
            models.Index(fields=["user", "deleted_at", "person", "date"]),
            models.Index(fields=["user", "direction", "currency", "date"]),
        ]
        constraints = [
            models.UniqueConstraint(fields=["user", "request_id"], name="unique_debt_request"),
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="debt_positive_amount"),
            models.CheckConstraint(
                condition=models.Q(currency__in=Currency.values), name="debt_currency"
            ),
            models.CheckConstraint(
                condition=models.Q(direction__in=DebtDirection.values), name="debt_direction"
            ),
            models.CheckConstraint(
                condition=models.Q(origin=DebtOrigin.EXISTING, account__isnull=True)
                | models.Q(origin=DebtOrigin.MOVEMENT, account__isnull=False),
                name="debt_origin_account",
            ),
        ]


class MoneyTransaction(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="transactions")
    destination = models.ForeignKey(
        Account, on_delete=models.PROTECT, null=True, related_name="incoming_transfers"
    )
    category = models.ForeignKey(ExpenseCategory, on_delete=models.PROTECT, null=True)
    debt = models.ForeignKey(Debt, on_delete=models.PROTECT, null=True, related_name="movements")
    debt_role = models.CharField(max_length=9, blank=True, default="")
    kind = models.CharField(max_length=8, choices=TransactionKind.choices)
    currency = models.CharField(max_length=3, choices=Currency.choices)
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    exchange_rate = models.DecimalField(max_digits=18, decimal_places=6, null=True)
    received_amount = models.DecimalField(max_digits=18, decimal_places=2, null=True)
    date = models.DateField()
    note = models.CharField(max_length=500, blank=True)
    version = models.PositiveIntegerField(default=1)
    request_id = models.UUIDField()
    creation_payload = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True)

    class Meta:
        ordering = ["-date", "-id"]
        indexes = [
            models.Index(fields=["user", "deleted_at", "date", "id"]),
            models.Index(fields=["account", "currency", "deleted_at", "date"]),
            models.Index(fields=["destination", "currency", "deleted_at", "date"]),
            models.Index(fields=["debt", "debt_role", "deleted_at", "date", "id"]),
            models.Index(fields=["user", "kind", "date"]),
            models.Index(fields=["user", "currency", "date"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "request_id"], name="unique_transaction_request"
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="transaction_positive_amount"
            ),
            models.CheckConstraint(
                condition=models.Q(currency__in=Currency.values), name="transaction_currency"
            ),
            models.CheckConstraint(
                condition=models.Q(kind__in=TransactionKind.values), name="transaction_kind"
            ),
            models.CheckConstraint(
                condition=models.Q(
                    kind__in=[
                        TransactionKind.INCOME,
                        TransactionKind.TRANSFER,
                        TransactionKind.EXCHANGE,
                        TransactionKind.DEBT_IN,
                        TransactionKind.DEBT_OUT,
                    ],
                    category__isnull=True,
                )
                | models.Q(kind=TransactionKind.EXPENSE, category__isnull=False),
                name="transaction_category_matches_kind",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(kind=TransactionKind.TRANSFER, destination__isnull=False)
                    & ~models.Q(account=models.F("destination"))
                )
                | (~models.Q(kind=TransactionKind.TRANSFER) & models.Q(destination__isnull=True)),
                name="transaction_destination_matches_kind",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    kind=TransactionKind.EXCHANGE,
                    exchange_rate__isnull=False,
                    exchange_rate__gt=0,
                    received_amount__isnull=False,
                    received_amount__gt=0,
                )
                | (
                    ~models.Q(kind=TransactionKind.EXCHANGE)
                    & models.Q(exchange_rate__isnull=True, received_amount__isnull=True)
                ),
                name="transaction_exchange_fields",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    kind__in=[TransactionKind.DEBT_IN, TransactionKind.DEBT_OUT],
                    debt__isnull=False,
                    debt_role__in=["principal", "repayment"],
                )
                | (
                    ~models.Q(kind__in=[TransactionKind.DEBT_IN, TransactionKind.DEBT_OUT])
                    & models.Q(debt__isnull=True, debt_role="")
                ),
                name="transaction_debt_fields",
            ),
            models.UniqueConstraint(
                fields=["debt"],
                condition=models.Q(debt_role="principal"),
                name="one_debt_principal_movement",
            ),
        ]
