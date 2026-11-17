from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Q, QuerySet, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError

from identity.models import User, UserSettings

from .exchanges import MAX_MONEY, exchange_amount, received_currency
from .models import Account, ExpenseCategory, MoneyTransaction, TransactionKind

MAX_BALANCE = MAX_MONEY


class LedgerConflict(APIException):
    status_code = 409
    default_detail = "This transaction changed. Refresh Activity before trying again."


def transactions_for(user: User) -> QuerySet[MoneyTransaction]:
    return MoneyTransaction.objects.filter(user=user, deleted_at__isnull=True).select_related(
        "account__opening", "destination__opening", "category"
    )


def opening_date(recorded_at: datetime, zone: str) -> date:
    return timezone.localdate(recorded_at, ZoneInfo(zone))


def check_history(user: User, account_id: int, currency: str) -> None:
    account = Account.objects.select_related("opening").get(user=user, pk=account_id)
    balance = getattr(account.opening, currency.lower())
    days = (
        MoneyTransaction.objects.filter(
            Q(account=account) | Q(destination=account),
            user=user,
            deleted_at__isnull=True,
        )
        .filter(Q(currency=currency) | Q(kind=TransactionKind.EXCHANGE))
        .order_by()
        .values("date")
        .annotate(
            income=Sum(
                "amount",
                filter=Q(currency=currency)
                & (Q(kind=TransactionKind.INCOME) | Q(destination=account)),
                default=Decimal("0"),
            ),
            expense=Sum(
                "amount",
                filter=Q(
                    account=account,
                    currency=currency,
                    kind__in=[
                        TransactionKind.EXPENSE,
                        TransactionKind.TRANSFER,
                        TransactionKind.EXCHANGE,
                    ],
                ),
                default=Decimal("0"),
            ),
            exchanged=Sum(
                "received_amount",
                filter=Q(kind=TransactionKind.EXCHANGE) & ~Q(currency=currency),
                default=Decimal("0"),
            ),
        )
        .order_by("date")
    )
    # Date-only entries count incoming funds before outgoing funds on the same day.
    # Check every later closing balance, including incoming/outgoing transfers.
    for day in days.iterator(chunk_size=500):
        balance += day["income"] + day["exchanged"] - day["expense"]
        if balance < 0:
            raise ValidationError(
                {"amount": ["This change leaves insufficient funds on its date or a later date."]}
            )
        if balance > MAX_BALANCE:
            raise ValidationError(
                {"amount": ["This change exceeds the supported account balance."]}
            )


def creation_payload(values: dict) -> dict:
    payload = {
        field: str(values[field]) if field in {"date", "amount"} else values[field]
        for field in ("account", "category", "kind", "currency", "amount", "date", "note")
    }
    # Preserve pre-transfer request payloads so existing explicit retries still match.
    if values["kind"] == TransactionKind.TRANSFER:
        payload["destination"] = values["destination"]
    if values["kind"] == TransactionKind.EXCHANGE:
        payload["exchange_rate"] = str(values["exchange_rate"])
        payload["received_amount"] = str(values["received_amount"])
    return payload


def affected_accounts(entry: MoneyTransaction) -> set[tuple[int, str]]:
    affected = {(entry.account_id, entry.currency)}
    if entry.destination_id is not None:
        affected.add((entry.destination_id, entry.currency))
    if entry.kind == TransactionKind.EXCHANGE:
        affected.add((entry.account_id, received_currency(entry.currency)))
    return affected


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
        affected = affected_accounts(entry)

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
    rate = None
    received = None
    if values["kind"] == TransactionKind.EXCHANGE:
        # An existing exchange keeps its historical rate even after Settings is cleared.
        if (
            entry.kind != TransactionKind.EXCHANGE
            and not UserSettings.objects.filter(user=user, exchange_rate__isnull=False).exists()
        ):
            raise ValidationError({"exchange_rate": ["Save an exchange rate in Settings first."]})
        rate = values["exchange_rate"]
        received = exchange_amount(values["amount"], values["currency"], rate)
        if received != values["received_amount"]:
            raise ValidationError({"received_amount": ["Review the recalculated exchange amount."]})
    destination = None
    if values["kind"] == TransactionKind.TRANSFER:
        destination = (
            Account.objects.filter(user=user, pk=values.get("destination"))
            .select_related("opening")
            .first()
        )
        if destination is None or (
            destination.archived and (entry_id is None or entry.destination_id != destination.pk)
        ):
            raise ValidationError({"destination": ["Choose an active destination account."]})
        if destination.pk == account.pk:
            raise ValidationError({"destination": ["Choose a different account."]})
        if values["date"] < destination.opening.effective_date:
            raise ValidationError(
                {"date": ["Choose a date on or after both accounts' opening dates."]}
            )
    category = None
    if values["kind"] == TransactionKind.EXPENSE:
        category = ExpenseCategory.objects.filter(user=user, pk=values["category"]).first()
        if category is None or (
            category.archived and (entry_id is None or entry.category_id != category.pk)
        ):
            raise ValidationError({"category": ["Choose an active expense category."]})
    entry.account = account
    entry.destination = destination
    entry.exchange_rate = rate
    entry.received_amount = received
    entry.category = category
    for field in ("kind", "currency", "amount", "date", "note"):
        setattr(entry, field, values[field])
    if entry_id is not None:
        entry.version += 1
    entry.save()
    affected.update(affected_accounts(entry))
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
    for account_id, currency in affected_accounts(entry):
        check_history(user, account_id, currency)
