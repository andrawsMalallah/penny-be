from datetime import date
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Count, DecimalField, F, Min, Q, QuerySet, Sum, Value
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from app.finance.models import (
    Account,
    Debt,
    DebtDirection,
    DebtOrigin,
    DebtPerson,
    MoneyTransaction,
    TransactionKind,
)
from app.finance.services.ledger import LedgerConflict, affected_accounts, check_history
from app.identity.models import User

REPAYMENTS = Q(movements__debt_role="repayment", movements__deleted_at__isnull=True)


def debts_for(user: User) -> QuerySet[Debt]:
    money = DecimalField(max_digits=30, decimal_places=2)
    return (
        Debt.objects.filter(user=user, deleted_at__isnull=True)
        .select_related("person", "account__opening")
        .annotate(
            repaid=Coalesce(
                Sum("movements__amount", filter=REPAYMENTS), Value(Decimal("0")), output_field=money
            ),
            repayment_count=Count("movements", filter=REPAYMENTS),
            first_repayment=Min("movements__date", filter=REPAYMENTS),
        )
        .annotate(remaining=F("amount") - F("repaid"))
        .order_by("person__name", "-date", "-id")
    )


def repayments_for(user: User, debt_id: int) -> QuerySet[MoneyTransaction]:
    return MoneyTransaction.objects.filter(
        user=user, debt_id=debt_id, debt_role="repayment", deleted_at__isnull=True
    ).select_related("account__opening", "category", "destination__opening", "debt__person")


def payload(values: dict, fields: tuple[str, ...]) -> dict:
    return {
        field: str(values[field]) if isinstance(values[field], (date, Decimal)) else values[field]
        for field in fields
    }


DEBT_FIELDS = (
    "person",
    "person_name",
    "direction",
    "origin",
    "account",
    "currency",
    "amount",
    "date",
    "note",
)
PAYMENT_FIELDS = ("account", "amount", "date", "note")


def validate_date(value: date, zone: str) -> None:
    if value > timezone.localdate(timezone.now(), ZoneInfo(zone)):
        raise ValidationError({"date": ["Future dates are not allowed."]})


def check_version(entry: Debt | MoneyTransaction, version: int) -> None:
    if entry.version != version:
        raise LedgerConflict()


def tombstone(user: User, entry: MoneyTransaction) -> None:
    entry.deleted_at = timezone.now()
    entry.version += 1
    entry.save(update_fields=["deleted_at", "version", "updated_at"])
    for account_id, currency in affected_accounts(entry):
        check_history(user, account_id, currency)


def sync_movement(
    user: User, debt: Debt, values: dict, role: str, entry: MoneyTransaction | None = None
) -> MoneyTransaction:
    affected = affected_accounts(entry) if entry is not None else set()
    account = (
        Account.objects.filter(user=user, pk=values["account"]).select_related("opening").first()
    )
    if account is None or (
        account.archived
        and (entry is None or entry.account_id != account.pk or entry.deleted_at is not None)
    ):
        raise ValidationError({"account": ["Choose an active account that belongs to you."]})
    if values["date"] < account.opening.effective_date:
        raise ValidationError({"date": ["Choose a date on or after the account opening date."]})
    incoming = (
        debt.direction == DebtDirection.I_OWE
        if role == "principal"
        else debt.direction == DebtDirection.OWED_TO_ME
    )
    if entry is None:
        entry = MoneyTransaction(
            user=user,
            # Debt and money-create UUID namespaces are independent. Only repayments
            # expose a money draft UUID; the debt request guards principal retries.
            request_id=uuid4() if role == "principal" else values["request_id"],
            creation_payload=values.get("creation_payload", {}),
        )
    entry.account = account
    entry.debt = debt
    entry.debt_role = role
    entry.kind = TransactionKind.DEBT_IN if incoming else TransactionKind.DEBT_OUT
    entry.currency = debt.currency
    entry.amount = values["amount"]
    entry.date = values["date"]
    entry.note = values["note"]
    entry.deleted_at = None
    if entry.pk:
        entry.version += 1
    entry.save()
    affected.update(affected_accounts(entry))
    for account_id, currency in affected:
        check_history(user, account_id, currency)
    return entry


@transaction.atomic
def save_debt(user: User, values: dict, debt_id: int | None = None) -> tuple[Debt, bool]:
    User.objects.select_for_update().get(pk=user.pk)
    if debt_id is None:
        previous = Debt.objects.filter(user=user, request_id=values["request_id"]).first()
        if previous is not None:
            if previous.deleted_at is not None or previous.creation_payload != payload(
                values, DEBT_FIELDS
            ):
                raise LedgerConflict()
            return debts_for(user).get(pk=previous.pk), False
        debt = Debt(
            user=user,
            request_id=values["request_id"],
            creation_payload=payload(values, DEBT_FIELDS),
        )
    else:
        debt = get_object_or_404(debts_for(user), pk=debt_id)
        check_version(debt, values["version"])
        if values["amount"] < debt.repaid:
            raise ValidationError(
                {
                    "amount": [
                        "Remove or correct repayments before reducing the debt below their total."
                    ]
                }
            )
        if debt.repayment_count and (
            values["currency"] != debt.currency or values["direction"] != debt.direction
        ):
            raise ValidationError(
                {"currency": ["Remove repayments before changing the debt currency or direction."]}
            )
        if debt.first_repayment and values["date"] > debt.first_repayment:
            raise ValidationError({"date": ["The debt date cannot follow its repayments."]})
    validate_date(values["date"], values["timezone"])
    if values["person"] is not None:
        person = get_object_or_404(DebtPerson, user=user, pk=values["person"])
    else:
        person = DebtPerson.objects.filter(user=user, name__iexact=values["person_name"]).first()
        if person is None:
            person = DebtPerson.objects.create(user=user, name=values["person_name"])
    debt.person = person
    for field in ("direction", "origin", "currency", "amount", "date", "note"):
        setattr(debt, field, values[field])
    debt.account_id = values["account"]
    # Validate ownership before assigning a foreign account, even when the DB FK exists.
    if (
        debt.account_id is not None
        and not Account.objects.filter(user=user, pk=debt.account_id).exists()
    ):
        raise ValidationError({"account": ["Choose one of your accounts."]})
    if debt_id is not None:
        debt.version += 1
    debt.save()
    principal = MoneyTransaction.objects.filter(user=user, debt=debt, debt_role="principal").first()
    if debt.origin == DebtOrigin.MOVEMENT:
        sync_movement(user, debt, values, "principal", principal)
    elif principal is not None and principal.deleted_at is None:
        tombstone(user, principal)
    return debts_for(user).get(pk=debt.pk), debt_id is None


@transaction.atomic
def delete_debt(user: User, debt_id: int, version: int) -> None:
    User.objects.select_for_update().get(pk=user.pk)
    debt = get_object_or_404(debts_for(user), pk=debt_id)
    check_version(debt, version)
    if debt.repayment_count:
        raise ValidationError({"amount": ["Delete the repayments before deleting this debt."]})
    principal = MoneyTransaction.objects.filter(
        user=user, debt=debt, debt_role="principal", deleted_at__isnull=True
    ).first()
    if principal is not None:
        tombstone(user, principal)
    debt.deleted_at = timezone.now()
    debt.version += 1
    debt.save(update_fields=["deleted_at", "version", "updated_at"])


@transaction.atomic
def save_repayment(
    user: User, debt_id: int, values: dict, entry_id: int | None = None
) -> tuple[MoneyTransaction, bool]:
    User.objects.select_for_update().get(pk=user.pk)
    debt = get_object_or_404(debts_for(user), pk=debt_id)
    if entry_id is None:
        previous = MoneyTransaction.objects.filter(
            user=user, request_id=values["request_id"]
        ).first()
        original = {"debt": debt_id, **payload(values, PAYMENT_FIELDS)}
        if previous is not None:
            if (
                previous.deleted_at is not None
                or previous.debt_id != debt_id
                or previous.debt_role != "repayment"
                or previous.creation_payload != original
            ):
                raise LedgerConflict()
            return repayments_for(user, debt_id).get(pk=previous.pk), False
        values = {**values, "creation_payload": original}
        entry = None
        available = debt.remaining
    else:
        entry = get_object_or_404(repayments_for(user, debt_id), pk=entry_id)
        check_version(entry, values["version"])
        available = debt.remaining + entry.amount
    if values["amount"] > available:
        raise ValidationError({"amount": ["Repayment cannot exceed the remaining debt."]})
    validate_date(values["date"], values["timezone"])
    if values["date"] < debt.date:
        raise ValidationError({"date": ["Repayment cannot predate the debt."]})
    entry = sync_movement(user, debt, values, "repayment", entry)
    debt.version += 1
    debt.save(update_fields=["version", "updated_at"])
    return repayments_for(user, debt_id).get(pk=entry.pk), entry_id is None


@transaction.atomic
def delete_repayment(user: User, debt_id: int, entry_id: int, version: int) -> None:
    User.objects.select_for_update().get(pk=user.pk)
    debt = get_object_or_404(debts_for(user), pk=debt_id)
    entry = get_object_or_404(repayments_for(user, debt_id), pk=entry_id)
    check_version(entry, version)
    tombstone(user, entry)
    debt.version += 1
    debt.save(update_fields=["version", "updated_at"])
