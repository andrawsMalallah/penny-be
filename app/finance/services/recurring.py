"""Virtual due slots and durable outcomes: no scheduler and no GET initialization."""

from calendar import monthrange
from collections.abc import Iterator
from datetime import date, timedelta
from itertools import islice
from uuid import uuid4
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Q, QuerySet
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError

from app.finance.models import (
    Account,
    ExpenseCategory,
    RecurringFrequency,
    RecurringOutcome,
    RecurringSchedule,
    RecurringState,
)
from app.finance.services.ledger import save_transaction
from app.identity.models import User


class RecurringConflict(APIException):
    status_code = 409
    default_detail = "This schedule or occurrence changed. Refresh before trying again."


def today_in(zone: str) -> date:
    return timezone.localdate(timezone.now(), ZoneInfo(zone))


def schedules_for(user: User) -> QuerySet[RecurringSchedule]:
    return RecurringSchedule.objects.filter(user=user, deleted_at__isnull=True).select_related(
        "account__opening", "category"
    )


@transaction.atomic
def delete_stopped_schedule(user: User, pk: int, version: int) -> None:
    User.objects.select_for_update().get(pk=user.pk)
    schedule = get_object_or_404(schedules_for(user), pk=pk)
    check_version(schedule, version)
    if schedule.state != RecurringState.STOPPED:
        raise ValidationError({"state": ["Stop this plan before deleting it."]})
    # Retain windows and outcomes so posted entries and resolved dates remain auditable.
    schedule.deleted_at = timezone.now()
    schedule.version += 1
    schedule.save(update_fields=["deleted_at", "version"])


def dates_from(anchor: date, frequency: str, begins: date, ends: date) -> Iterator[date]:
    """Jump to the requested interval without iterating earlier years or drifting dates."""
    if frequency == RecurringFrequency.WEEKLY:
        ordinal = max(0, (begins - anchor).days + 6) // 7
        while True:
            try:
                candidate = anchor + timedelta(weeks=ordinal)
            except OverflowError:
                return
            if candidate > ends:
                return
            yield candidate
            ordinal += 1
    else:
        step = 12 if frequency == RecurringFrequency.YEARLY else 1
        elapsed = (begins.year - anchor.year) * 12 + begins.month - anchor.month
        ordinal = max(0, elapsed // step)
        while True:
            absolute = anchor.year * 12 + anchor.month - 1 + ordinal * step
            year, month_index = divmod(absolute, 12)
            if year > 9999:
                return
            candidate = date(
                year, month_index + 1, min(anchor.day, monthrange(year, month_index + 1)[1])
            )
            if candidate > ends:
                return
            if candidate >= begins:
                yield candidate
            ordinal += 1


def pending(schedule: RecurringSchedule, today: date, after: date | None = None) -> dict:
    if after == date.max:
        return {"results": [], "next_cursor": None}
    lower = after + timedelta(days=1) if after else date.min
    windows = schedule.windows.filter(begins__lte=today).filter(
        Q(ends__isnull=True) | Q(ends__gte=lower)
    )
    # Bound both timing-history and date scans, including ancient backlogs.
    sampled_windows = list(windows[:101])

    def candidates() -> Iterator[date]:
        for window in sampled_windows[:100]:
            yield from dates_from(
                window.anchor,
                window.frequency,
                max(lower, window.begins),
                min(today, window.ends or today),
            )

    sampled = list(islice(candidates(), 101))
    settled = set(
        RecurringOutcome.objects.filter(
            schedule=schedule, scheduled_date__in=sampled[:100]
        ).values_list("scheduled_date", flat=True)
    )
    available = [day for day in sampled[:100] if day not in settled]
    result = available[:10]
    more = len(available) > 10 or len(sampled) > 100 or len(sampled_windows) > 100
    cursor = result[-1] if len(available) > 10 else (sampled[99] if len(sampled) > 100 else None)
    if more and cursor is None:
        # Empty historical timing windows still advance the bounded scan.
        cursor = min(today, sampled_windows[99].ends or today)
        if cursor <= (after or date.min):
            more = False
    return {
        "results": [day.isoformat() for day in result],
        "next_cursor": cursor.isoformat() if more and cursor else None,
    }


def check_version(schedule: RecurringSchedule, version: int) -> None:
    if schedule.version != version:
        raise RecurringConflict()


def close_future(schedule: RecurringSchedule, today: date) -> None:
    schedule.windows.filter(begins__gt=today).delete()
    schedule.windows.filter(Q(ends__isnull=True) | Q(ends__gt=today)).update(ends=today)


def open_window(schedule: RecurringSchedule, begins: date) -> None:
    if schedule.end_date is None or begins <= schedule.end_date:
        schedule.windows.create(
            frequency=schedule.frequency,
            anchor=schedule.start_date,
            begins=begins,
            ends=schedule.end_date,
        )


@transaction.atomic
def save_schedule(
    user: User, values: dict, pk: int | None = None
) -> tuple[RecurringSchedule, bool]:
    User.objects.select_for_update().get(pk=user.pk)
    fields = (
        "name",
        "kind",
        "account",
        "category",
        "currency",
        "amount",
        "note",
        "frequency",
        "start_date",
        "end_date",
    )
    payload = {
        field: str(values[field])
        if field in {"amount", "start_date", "end_date"}
        else values[field]
        for field in fields
    }
    if pk is None:
        previous = RecurringSchedule.objects.filter(
            user=user, request_id=values["request_id"]
        ).first()
        if previous:
            if previous.creation_payload != payload or previous.state == RecurringState.STOPPED:
                raise RecurringConflict()
            return schedules_for(user).get(pk=previous.pk), False
        schedule = RecurringSchedule(
            user=user, request_id=values["request_id"], creation_payload=payload
        )
    else:
        schedule = get_object_or_404(schedules_for(user), pk=pk)
        check_version(schedule, values["version"])
        if schedule.state == RecurringState.STOPPED:
            raise ValidationError({"state": ["Stopped schedules cannot be edited."]})
    account = get_object_or_404(
        Account.objects.filter(user=user).select_related("opening"), pk=values["account"]
    )
    if account.archived and (pk is None or schedule.account_id != account.pk):
        raise ValidationError({"account": ["Choose an active account."]})
    category = None
    if values["category"] is not None:
        category = get_object_or_404(ExpenseCategory, user=user, pk=values["category"])
        if category.archived and (pk is None or schedule.category_id != category.pk):
            raise ValidationError({"category": ["Choose an active category."]})
    timing_changed = pk is not None and any(
        getattr(schedule, field) != values[field]
        for field in ("frequency", "start_date", "end_date")
    )
    if (pk is None or values["start_date"] != schedule.start_date) and values[
        "start_date"
    ] < account.opening.effective_date:
        raise ValidationError({"start_date": ["Start on or after the account opening date."]})
    # A timezone change near midnight must not rewind a previous timing decision.
    today = max(today_in(values["timezone"]), schedule.timing_boundary or date.min)
    schedule.timing_boundary = today
    if timing_changed:
        close_future(schedule, today)
    for field in fields:
        setattr(
            schedule,
            field,
            account if field == "account" else category if field == "category" else values[field],
        )
    if pk is not None:
        schedule.version += 1
    schedule.save()
    if pk is None:
        open_window(schedule, schedule.start_date)
    elif timing_changed and schedule.state == RecurringState.ACTIVE and today < date.max:
        open_window(schedule, max(schedule.start_date, today + timedelta(days=1)))
    return schedules_for(user).get(pk=schedule.pk), pk is None


@transaction.atomic
def change_state(user: User, pk: int, values: dict) -> RecurringSchedule:
    User.objects.select_for_update().get(pk=user.pk)
    schedule = get_object_or_404(schedules_for(user), pk=pk)
    check_version(schedule, values["version"])
    target = values["state"]
    allowed = {
        RecurringState.ACTIVE: {RecurringState.PAUSED, RecurringState.STOPPED},
        RecurringState.PAUSED: {RecurringState.ACTIVE, RecurringState.STOPPED},
        RecurringState.STOPPED: set(),
    }
    if target not in allowed[schedule.state]:
        raise ValidationError({"state": ["Choose a valid state change."]})
    today = max(today_in(values["timezone"]), schedule.timing_boundary or date.min)
    schedule.timing_boundary = today
    if schedule.state == RecurringState.ACTIVE:
        close_future(schedule, today)
    schedule.state = target
    if target == RecurringState.ACTIVE and today < date.max:
        open_window(schedule, max(schedule.start_date, today + timedelta(days=1)))
    schedule.version += 1
    schedule.save(update_fields=["state", "version", "timing_boundary"])
    return schedule


@transaction.atomic
def resolve(user: User, pk: int, values: dict, skip: bool = False) -> RecurringOutcome:
    User.objects.select_for_update().get(pk=user.pk)
    schedule = get_object_or_404(schedules_for(user), pk=pk)
    check_version(schedule, values["version"])
    scheduled = values["scheduled_date"]
    today = today_in(values["timezone"])
    if scheduled > today or not any(
        next(dates_from(window.anchor, window.frequency, scheduled, scheduled), None) == scheduled
        for window in schedule.windows.filter(begins__lte=scheduled).filter(
            Q(ends__isnull=True) | Q(ends__gte=scheduled)
        )[:1]
    ):
        raise ValidationError({"scheduled_date": ["Choose a due occurrence of this schedule."]})
    if RecurringOutcome.objects.filter(schedule=schedule, scheduled_date=scheduled).exists():
        raise RecurringConflict()
    entry = None
    if not skip:
        transaction_values = values["transaction"]
        if transaction_values["kind"] not in {"income", "expense"}:
            raise ValidationError({"kind": ["Only income and expenses can recur."]})
        entry, _ = save_transaction(user, {**transaction_values, "request_id": uuid4()})
    outcome = RecurringOutcome.objects.create(
        schedule=schedule, scheduled_date=scheduled, skipped=skip, transaction=entry
    )
    schedule.version += 1
    schedule.save(update_fields=["version"])
    return outcome
