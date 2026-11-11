from django.conf import settings
from django.db import models
from django.db.models import F

from apps.finances.models import Currency, Operation


class Direction(models.TextChoices):
    RECEIVABLE = "receivable", "Owed to me"
    PAYABLE = "payable", "I owe"


class Person(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    name = models.CharField(max_length=80)
    phone = models.CharField(max_length=40, blank=True)
    note = models.CharField(max_length=1000, blank=True)
    archived = models.BooleanField(default=False)

    class Meta:
        indexes = [models.Index(fields=["owner", "archived", "id"])]


class Debt(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    person = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="debts")
    direction = models.CharField(max_length=16, choices=Direction.choices)
    currency = models.CharField(max_length=3, choices=Currency.choices)
    original_amount = models.DecimalField(max_digits=18, decimal_places=2)
    remaining_amount = models.DecimalField(max_digits=18, decimal_places=2)
    note = models.CharField(max_length=1000, blank=True)
    due_date = models.DateField(null=True)
    cancelled = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(original_amount__gt=0), name="debts_positive_principal"
            ),
            models.CheckConstraint(
                condition=models.Q(
                    remaining_amount__gte=0, remaining_amount__lte=F("original_amount")
                ),
                name="debts_remaining_bounds",
            ),
            models.CheckConstraint(
                condition=models.Q(currency__in=Currency.values), name="debts_currency"
            ),
            models.CheckConstraint(
                condition=models.Q(direction__in=Direction.values), name="debts_direction"
            ),
        ]
        indexes = [
            models.Index(fields=["owner", "person", "id"]),
            models.Index(fields=["owner", "direction", "currency"]),
        ]


class DebtEvent(models.Model):
    debt = models.ForeignKey(Debt, on_delete=models.CASCADE, related_name="events")
    operation = models.OneToOneField(Operation, on_delete=models.PROTECT, related_name="debt_event")
    remaining_delta = models.DecimalField(max_digits=18, decimal_places=2)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(remaining_delta=0), name="debts_nonzero_change"
            )
        ]
