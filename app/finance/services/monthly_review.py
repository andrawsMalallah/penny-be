"""Current read-only month summaries; no closing, saved snapshots or financial writes."""

from calendar import monthrange
from datetime import date, timedelta
from decimal import localcontext
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from django.core.paginator import EmptyPage, Paginator
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError

from app.finance.services.analytics import (
    CURRENCIES,
    activity_totals,
    breakdown,
    breakdown_row,
    money,
    shift_month,
)
from app.finance.services.budgets import active_categories, budget_rows
from app.identity.models import User


def monthly_period(month: date, zone: str) -> dict:
    today = timezone.localdate(timezone.now(), ZoneInfo(zone))
    first = today.replace(day=1)
    if month > first or month == date.min:
        raise ValidationError({"month": ["Choose this month or a past month with a prior month."]})
    previous_start = shift_month(month, -1)
    incomplete = month == first
    end = today if incomplete else month.replace(day=monthrange(month.year, month.month)[1])
    # Same elapsed calendar dates clamp to the preceding month's final day.
    previous_end = shift_month(end, -1) if incomplete else month - timedelta(days=1)
    return {
        "month": month.isoformat()[:7],
        "timezone": zone,
        "today": today,
        "start": month,
        "end": end,
        "previous_start": previous_start,
        "previous_end": previous_end,
        "incomplete": incomplete,
    }


@transaction.atomic
def monthly_review(user: User, period: dict, page: int = 1) -> dict:
    User.objects.select_for_update().get(pk=user.pk)
    totals = activity_totals(user, period["start"], period["end"])
    before = activity_totals(user, period["previous_start"], period["previous_end"])
    activity = {}
    with localcontext() as precision:
        precision.prec = 60
        for currency in CURRENCIES:
            now, previous = totals[currency], before[currency]
            difference = now["income"] - now["expense"]
            previous_difference = previous["income"] - previous["expense"]
            activity[currency] = {
                "income": money(now["income"]),
                "expense": money(now["expense"]),
                "difference": money(difference),
                "previous_income": money(previous["income"]),
                "previous_expense": money(previous["expense"]),
                "previous_difference": money(previous_difference),
                "income_change": money(now["income"] - previous["income"]),
                "expense_change": money(now["expense"] - previous["expense"]),
                "difference_change": money(difference - previous_difference),
            }
    categories = {
        currency: [
            breakdown_row(row)
            for row in breakdown(user, "categories", period)
            .filter(**{f"{currency}__gt": 0})
            .order_by(f"-{currency}", "category")[:5]
        ]
        for currency in CURRENCIES
    }
    paginator = Paginator(active_categories(user, period["start"]), 10)
    try:
        budget_page = paginator.page(page)
    except EmptyPage:
        raise NotFound("Invalid page.") from None

    def page_link(number: int) -> str:
        return "/api/v1/analytics/monthly/?" + urlencode(
            {
                "month": period["month"],
                "timezone": period["timezone"],
                "page": number,
            }
        )

    return {
        "as_of": timezone.now(),
        "period": period,
        "activity": activity,
        "top_categories": categories,
        "budgets": {
            "count": paginator.count,
            "next": page_link(page + 1) if budget_page.has_next() else None,
            "previous": page_link(page - 1) if budget_page.has_previous() else None,
            "results": budget_rows(
                list(budget_page), user, period["start"], end_date=period["end"]
            ),
        },
    }
