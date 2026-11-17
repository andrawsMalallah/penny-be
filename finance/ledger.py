from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Q, QuerySet, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError

from identity.models import User

from .models import Account, ExpenseCategory, MoneyTransaction, TransactionKind

MAX_BALANCE = Decimal("9999999999999999.99")


class LedgerConflict(APIException):
    status_code = 409
    default_detail = "This transaction changed. Refresh Activity before trying again."


def transactions_for(user: User) -> QuerySet[MoneyTransaction]:
    return MoneyTransaction.objects.filter(user=user, deleted_at__isnull=True).select_related(
        "account__opening", "category"
    )


def opening_date(recorded_at: datetime, zone: str) -> date:
    return timezone.localdate(recorded_at, ZoneInfo(zone))


def check_history(user: User, account_id: int, currency: str) -> None:
    account = Account.objects.select_related("opening").get(user=user, pk=account_id)
    balance = getattr(account.opening, currency.lower())
    days = (
        MoneyTransaction.objects.filter(
            user=user, account=account, currency=currency, deleted_at__isnull=True
        )
        .order_by()
        .values("date")
        .annotate(
            income=Sum("amount", filter=Q(kind=TransactionKind.INCOME), default=Decimal("0")),
            expense=Sum("amount", filter=Q(kind=TransactionKind.EXPENSE), default=Decimal("0")),
        )
        .order_by("date")
    )
    # Date-only entries put all income before expenses; the daily closing balance
    # is the minimum after income, so checking each day covers every later day.
    for day in days.iterator(chunk_size=500):
        balance += day["income"] - day["expense"]
        if balance < 0:
            raise ValidationError(
                {"amount": ["This change leaves insufficient funds on its date or a later date."]}
            )
        if balance > MAX_BALANCE:
            raise ValidationError(
                {"amount": ["This change exceeds the supported account balance."]}
            )


def creation_payload(values: dict) -> dict:
    return {
        field: str(values[field]) if field in {"date", "amount"} else values[field]
        for field in ("account", "category", "kind", "currency", "amount", "date", "note")
    }


@transaction.atomic
def save_transaction(
    user: User, values: dict, entry_id: int | None = None
) -> tuple[MoneyTransaction, bool]:
    User.objects.select_for_update().get(pk=user.pk)
    if entry_id is None:
        previous = MoneyTransaction.objects.filter(
            user=user, request_id=values["request_id"]
        ).first()
        if previous is not None:
            if previous.deleted_at is not None or previous.creation_payload != creation_payload(
                values
            ):
                raise LedgerConflict()
            return transactions_for(user).get(pk=previous.pk), False
        entry = MoneyTransaction(
            user=user, request_id=values["request_id"], creation_payload=creation_payload(values)
        )
        affected = set()
    else:
        entry = get_object_or_404(transactions_for(user), pk=entry_id)
        if entry.version != values["version"]:
            raise LedgerConflict()
        affected = {(entry.account_id, entry.currency)}

    account = (
        Account.objects.filter(user=user, pk=values["account"]).select_related("opening").first()
    )
    if account is None:
        raise ValidationError({"account": ["Choose one of your accounts."]})
    # Existing archived references may remain on a correction; new assignments must be active.
    if account.archived and (entry_id is None or entry.account_id != account.pk):
        raise ValidationError({"account": ["Choose an active account."]})
    if values["date"] < account.opening.effective_date:
        raise ValidationError({"date": ["Choose a date on or after the account's opening date."]})
    if values["date"] > timezone.localdate(timezone.now(), ZoneInfo(values["timezone"])):
        raise ValidationError({"date": ["Future transaction dates are not allowed."]})
    category = None
    if values["kind"] == TransactionKind.EXPENSE:
        category = ExpenseCategory.objects.filter(user=user, pk=values["category"]).first()
        if category is None or (
            category.archived and (entry_id is None or entry.category_id != category.pk)
        ):
            raise ValidationError({"category": ["Choose an active expense category."]})
    entry.account = account
    entry.category = category
    for field in ("kind", "currency", "amount", "date", "note"):
        setattr(entry, field, values[field])
    if entry_id is not None:
        entry.version += 1
    entry.save()
    affected.add((account.pk, entry.currency))
    for account_id, currency in affected:
        check_history(user, account_id, currency)
    return transactions_for(user).get(pk=entry.pk), entry_id is None


@transaction.atomic
def delete_transaction(user: User, entry_id: int, version: int) -> None:
    User.objects.select_for_update().get(pk=user.pk)
    entry = get_object_or_404(transactions_for(user), pk=entry_id)
    if entry.version != version:
        raise LedgerConflict()
    # Retain a tombstone so a retried create request can never recreate deleted money.
    entry.deleted_at = timezone.now()
    entry.version += 1
    entry.save(update_fields=["deleted_at", "version", "updated_at"])
    check_history(user, entry.account_id, entry.currency)
