"""Calendar rules provide automatic repetition without jobs or writes during reads."""

from calendar import monthrange
from datetime import date
from decimal import Decimal, localcontext

from django.db import transaction
from django.db.models import OuterRef, Q, QuerySet, Subquery, Sum
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import APIException, ValidationError

from app.finance.models import (
    BudgetRule,
    BudgetSchedule,
    ExpenseCategory,
    MoneyTransaction,
    TransactionKind,
)
from app.identity.models import User


class BudgetConflict(APIException):
    status_code = 409
    default_detail = "Budgets changed. Cancel, refresh and reopen before saving."


def categories_for_month(user: User, month: date) -> QuerySet[ExpenseCategory]:
    recurring = BudgetRule.objects.filter(
        schedule__category=OuterRef("pk"), recurring=True, month__lte=month
    ).order_by("-month")
    override = BudgetRule.objects.filter(
        schedule__category=OuterRef("pk"), recurring=False, month=month
    )
    # Select rule IDs first: a null limit means removal, not fallback to an older rule.
    return (
        ExpenseCategory.objects.filter(user=user)
        .annotate(
            recurring_rule=Subquery(recurring.values("pk")[:1]),
            override_rule=Subquery(override.values("pk")[:1]),
        )
        .select_related("budgetschedule")
        .order_by("id")
    )


def budget_rows(
    categories: list[ExpenseCategory], user: User, month: date, *, end_date: date | None = None
) -> list[dict]:
    rule_ids = [
        category.override_rule or category.recurring_rule
        for category in categories
        if category.override_rule or category.recurring_rule
    ]
    rules = BudgetRule.objects.in_bulk(rule_ids)
    end = end_date or month.replace(day=monthrange(month.year, month.month)[1])
    spending = {
        (row["category_id"], row["currency"]): row["total"]
        for row in MoneyTransaction.objects.filter(
            user=user,
            category_id__in=[category.pk for category in categories],
            kind=TransactionKind.EXPENSE,
            deleted_at__isnull=True,
            date__range=(month, end),
        )
        .values("category_id", "currency")
        .annotate(total=Sum("amount"))
    }
    rows = []
    for category in categories:
        rule = rules.get(category.override_rule or category.recurring_rule)
        schedule = getattr(category, "budgetschedule", None)
        row = {
            "category": category.pk,
            "name_en": category.name_en,
            "name_ar": category.name_ar,
            "archived": category.archived,
            "month": month.isoformat()[:7],
            "version": schedule.version if schedule else 0,
            "recurring": rule.recurring if rule else False,
        }
        with localcontext() as context:
            context.prec = 60
            for currency in ("IQD", "USD"):
                limit = getattr(rule, f"limit_{currency.lower()}", None)
                spent = spending.get((category.pk, currency), Decimal("0"))
                row[currency] = {
                    "limit": f"{limit:.2f}" if limit is not None else None,
                    "spent": f"{spent:.2f}",
                    "remaining": f"{limit - spent:.2f}" if limit is not None else None,
                    "over": limit is not None and spent > limit,
                }
        rows.append(row)
    return rows


def active_categories(user: User, month: date) -> QuerySet[ExpenseCategory]:
    rules = BudgetRule.objects.filter(
        Q(limit_iqd__isnull=False) | Q(limit_usd__isnull=False)
    ).values("pk")
    return categories_for_month(user, month).filter(
        Q(override_rule__in=rules) | Q(override_rule__isnull=True, recurring_rule__in=rules)
    )


@transaction.atomic
def save_budget(user: User, category_id: int, values: dict) -> dict:
    User.objects.select_for_update().get(pk=user.pk)
    category = get_object_or_404(ExpenseCategory, user=user, pk=category_id)
    schedule = BudgetSchedule.objects.filter(category=category).first()
    if values["version"] != (schedule.version if schedule else 0):
        raise BudgetConflict()
    if category.archived and schedule is None:
        raise ValidationError({"category": ["Restore this category before creating a budget."]})
    if schedule is None:
        schedule = BudgetSchedule.objects.create(category=category)
    month = values["month"]
    recurring = values["scope"] == "future"
    if recurring:
        # A lasting change replaces later plans, while every earlier month remains intact.
        schedule.rules.filter(month__gte=month).delete()
    schedule.rules.update_or_create(
        month=month,
        recurring=recurring,
        defaults={"limit_iqd": values["limit_iqd"], "limit_usd": values["limit_usd"]},
    )
    schedule.version += 1
    schedule.save(update_fields=["version"])
    category = categories_for_month(user, month).get(pk=category.pk)
    return budget_rows([category], user, month)[0]
