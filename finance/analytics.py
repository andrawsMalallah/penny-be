"""Read-only reports: aggregate all rows in SQL and paginate every variable-length breakdown."""

from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import DecimalField, F, OuterRef, Q, QuerySet, Subquery, Sum, Value
from django.db.models.functions import Coalesce, TruncMonth
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from identity.models import User

from .models import Debt, MoneyTransaction
from .services import accounts_for

CURRENCIES = ("IQD", "USD")
MONEY = DecimalField(max_digits=50, decimal_places=2)
ZERO = Decimal("0.00")


def money(value: Decimal | None) -> str:
    return format(value if value is not None else ZERO, ".2f")


def shift_month(value: date, offset: int) -> date:
    month_index = value.year * 12 + value.month - 1 + offset
    year, month = divmod(month_index, 12)
    return date(year, month + 1, min(value.day, monthrange(year, month + 1)[1]))


def report_period(values: dict) -> dict:
    today = timezone.localdate(timezone.now(), ZoneInfo(values["timezone"]))
    first = today.replace(day=1)
    period = values["period"]
    if period == "this_month":
        start, end = first, today
        previous_start, previous_end = shift_month(first, -1), shift_month(today, -1)
    elif period == "previous_month":
        start, end = shift_month(first, -1), first - timedelta(days=1)
        previous_start, previous_end = shift_month(start, -1), start - timedelta(days=1)
    elif period == "this_year":
        start, end = today.replace(month=1, day=1), today
        previous_start, previous_end = shift_month(start, -12), shift_month(today, -12)
    else:
        start, end = values.get("start"), values.get("end")
        if start is None or end is None:
            raise ValidationError({"start": ["Choose both dates for a custom range."]})
        if start > end or end > today:
            raise ValidationError({"end": ["Choose an end date between the start and today."]})
        try:
            previous_end = start - timedelta(days=1)
            previous_start = previous_end - (end - start)
        except OverflowError as error:
            raise ValidationError({"start": ["Choose a later start date."]}) from error
    return {
        "key": period,
        "start": start,
        "end": end,
        "previous_start": previous_start,
        "previous_end": previous_end,
        "today": today,
    }


def spending_for(user: User, period: dict) -> QuerySet[MoneyTransaction]:
    return MoneyTransaction.objects.filter(
        user=user,
        deleted_at__isnull=True,
        kind="expense",
        date__range=(period["start"], period["end"]),
    )


def outstanding_for(user: User) -> QuerySet[Debt]:
    # A scalar repayment subquery allows subsequent person/currency grouping without
    # multiplying principal amounts by the number of repayments in a join.
    repayments = (
        MoneyTransaction.objects.filter(
            debt=OuterRef("pk"), debt_role="repayment", deleted_at__isnull=True
        )
        .order_by()
        .values("debt")
        .annotate(total=Sum("amount"))
        .values("total")
    )
    return (
        Debt.objects.filter(user=user, deleted_at__isnull=True)
        .order_by()
        .annotate(
            remaining=F("amount")
            - Coalesce(Subquery(repayments, output_field=MONEY), Value(ZERO), output_field=MONEY)
        )
    )


def activity_totals(user: User, start: date, end: date) -> dict:
    rows = (
        MoneyTransaction.objects.filter(
            user=user,
            deleted_at__isnull=True,
            kind__in=["income", "expense"],
            date__range=(start, end),
        )
        .order_by()
        .values("currency", "kind")
        .annotate(total=Sum("amount", output_field=MONEY))
    )
    result = {currency: {"income": ZERO, "expense": ZERO} for currency in CURRENCIES}
    for row in rows:
        result[row["currency"]][row["kind"]] = row["total"]
    return result


@transaction.atomic
def summary(user: User, period: dict) -> dict:
    # Finance writes take this same lock. Keep all sections of one report coherent
    # without persisting a cache or changing any financial row.
    User.objects.select_for_update().get(pk=user.pk)
    with localcontext() as precision:
        precision.prec = 60
        balances = accounts_for(user).aggregate(
            **{
                currency: Sum(f"balance_{currency.lower()}", output_field=MONEY)
                for currency in CURRENCIES
            }
        )
        debts = {
            (row["currency"], row["direction"]): row["total"]
            for row in outstanding_for(user)
            .values("currency", "direction")
            .annotate(total=Sum("remaining", output_field=MONEY))
        }
        current = {}
        for currency in CURRENCIES:
            held = balances[currency] or ZERO
            owed_to_me, i_owe = (
                debts.get((currency, direction), ZERO) for direction in ("owed_to_me", "i_owe")
            )
            current[currency] = {
                "held": money(held),
                "owed_to_me": money(owed_to_me),
                "i_owe": money(i_owe),
                "net": money(held + owed_to_me - i_owe),
            }
        totals = activity_totals(user, period["start"], period["end"])
        previous = activity_totals(user, period["previous_start"], period["previous_end"])
        activity = {}
        for currency in CURRENCIES:
            now, before = totals[currency], previous[currency]
            activity[currency] = {
                "income": money(now["income"]),
                "expense": money(now["expense"]),
                "difference": money(now["income"] - now["expense"]),
                "previous_income": money(before["income"]),
                "previous_expense": money(before["expense"]),
                "income_change": money(now["income"] - before["income"]),
                "expense_change": money(now["expense"] - before["expense"]),
            }
        first = period["today"].replace(day=1)
        months = [shift_month(first, offset) for offset in range(-5, 1)]
        trend_rows = (
            MoneyTransaction.objects.filter(
                user=user,
                deleted_at__isnull=True,
                kind__in=["income", "expense"],
                date__range=(months[0], period["today"]),
            )
            .order_by()
            .annotate(month=TruncMonth("date"))
            .values("month", "currency", "kind")
            .annotate(total=Sum("amount", output_field=MONEY))
        )
        trends = {
            month: {
                "month": month.strftime("%Y-%m"),
                **{currency: {"income": "0.00", "expense": "0.00"} for currency in CURRENCIES},
            }
            for month in months
        }
        for row in trend_rows:
            trends[row["month"]][row["currency"]][row["kind"]] = money(row["total"])
        return {
            "as_of": timezone.now(),
            "period": period,
            "current": current,
            "activity": activity,
            "trends": list(trends.values()),
        }


def breakdown(user: User, section: str, period: dict | None = None) -> QuerySet:
    if section == "accounts":
        return (
            accounts_for(user)
            .values(
                "id",
                "name",
                "archived",
                "group__name",
                "target_iqd",
                "target_usd",
                "balance_iqd",
                "balance_usd",
            )
            .order_by("name", "id")
        )
    if section == "groups":
        return (
            accounts_for(user)
            .order_by()
            .values("group", "group__name")
            .annotate(
                **{
                    currency: Sum(f"balance_{currency.lower()}", output_field=MONEY)
                    for currency in CURRENCIES
                }
            )
            .order_by("group__name", "group")
        )
    if section == "categories":
        return (
            spending_for(user, period)
            .order_by()
            .values("category", "category__name_en", "category__name_ar", "category__archived")
            .annotate(
                **{
                    currency: Coalesce(
                        Sum("amount", filter=Q(currency=currency), output_field=MONEY),
                        Value(ZERO),
                        output_field=MONEY,
                    )
                    for currency in CURRENCIES
                }
            )
            .order_by("category")
        )
    return (
        outstanding_for(user)
        .filter(remaining__gt=0)
        .values("person", "person__name")
        .annotate(
            **{
                f"{direction}_{currency}": Coalesce(
                    Sum(
                        "remaining",
                        filter=Q(direction=direction, currency=currency),
                        output_field=MONEY,
                    ),
                    Value(ZERO),
                    output_field=MONEY,
                )
                for direction in ("owed_to_me", "i_owe")
                for currency in CURRENCIES
            }
        )
        .order_by("person__name", "person")
    )


def breakdown_row(row: dict) -> dict:
    return {
        key.replace("__", "_"): money(value) if isinstance(value, Decimal) else value
        for key, value in row.items()
    }
