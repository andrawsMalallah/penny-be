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

    class Meta:
        ordering = ["name", "id"]
        indexes = [models.Index(fields=["user", "archived", "name", "id"])]
        constraints = [
            models.UniqueConstraint(Lower("name"), "user", name="unique_money_account_name")
        ]


class OpeningBalance(models.Model):
    """Creation-time money, separate from future income and expense activity."""

    account = models.OneToOneField(Account, on_delete=models.PROTECT, related_name="opening")
    iqd = models.DecimalField(max_digits=18, decimal_places=2)
    usd = models.DecimalField(max_digits=18, decimal_places=2)
    recorded_at = models.DateTimeField(auto_now_add=True)

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
