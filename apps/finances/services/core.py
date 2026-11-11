import hashlib
import json
from collections.abc import Callable
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, localcontext
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import APIException, AuthenticationFailed, NotFound, ValidationError

from apps.accounts.models import DeviceSession, User
from apps.finances.models import (
    Account,
    AccountKind,
    Balance,
    BudgetMonth,
    Category,
    CommandRecord,
    Currency,
    LedgerEntry,
    Operation,
    Portfolio,
)

CENT = Decimal("0.01")
MAX_MONEY = Decimal("9999999999999999.99")
ZERO = Decimal("0.00")
STARTER_CATEGORIES = [
    ("Food & groceries", "الطعام والبقالة"),
    ("Transport", "النقل"),
    ("Housing", "السكن"),
    ("Utilities", "الخدمات"),
    ("Health", "الصحة"),
    ("Shopping", "التسوق"),
    ("Entertainment", "الترفيه"),
    ("Education", "التعليم"),
    ("Family", "العائلة"),
    ("Other", "أخرى"),
]


class Conflict(APIException):
    status_code = 409
    default_code = "financial_conflict"
    default_detail = "The financial state changed. Refresh and try again."


def money(value: Decimal) -> str:
    return format((value if value else ZERO).quantize(CENT), ".2f")


def month_start(value: date) -> date:
    return value.replace(day=1)


def check_date(portfolio: Portfolio, local_date: date, timezone: str) -> None:
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError, ValueError:
        raise ValidationError({"timezone": "Use an IANA timezone."}) from None
    if portfolio.active_month is None or month_start(local_date) != portfolio.active_month:
        raise Conflict({"code": "inactive_month", "detail": "Use a date in the active month."})


def execute(
    owner: User,
    key: UUID,
    action: str,
    data: dict,
    work: Callable[[Portfolio], dict],
    *,
    device_id: int | None = None,
    session_key: str | None = None,
) -> dict:
    fingerprint = hashlib.sha256(
        json.dumps(
            {"action": action, "data": data}, sort_keys=True, default=str, separators=(",", ":")
        ).encode()
    ).hexdigest()
    with transaction.atomic():
        # One documented lock order covers every financial write, including metadata.
        current_owner = User.objects.select_for_update().get(pk=owner.pk)
        if not current_owner.is_active or (
            device_id is not None
            and not DeviceSession.objects.filter(
                pk=device_id,
                user=current_owner,
                session_key=session_key,
                expires_at__gt=timezone.now(),
            ).exists()
        ):
            raise AuthenticationFailed("Session expired. Sign in again.")
        portfolio, _ = Portfolio.objects.get_or_create(owner=owner)
        previous = CommandRecord.objects.filter(owner=owner, key=key).first()
        if previous:
            if previous.generation != portfolio.generation:
                raise Conflict({"code": "pre_restore_request"})
            if previous.fingerprint != fingerprint:
                raise Conflict({"code": "idempotency_conflict"})
            return previous.result
        if "expected_revision" in data and data["expected_revision"] != portfolio.revision:
            raise Conflict({"code": "stale_preview"})
        result = work(portfolio)
        portfolio.revision += 1
        portfolio.save()
        result = json.loads(json.dumps({**result, "revision": portfolio.revision}, default=str))
        CommandRecord.objects.create(
            owner=owner,
            key=key,
            fingerprint=fingerprint,
            result=result,
            generation=portfolio.generation,
        )
        return result


def owned_account(owner_id: int, account_id: int, *, active: bool = True) -> Account:
    query = Account.objects.filter(owner_id=owner_id, pk=account_id)
    if active:
        query = query.filter(archived=False)
    account = query.first()
    if account is None:
        raise NotFound("Account not found.")
    return account


def system_account(owner_id: int, kind: AccountKind) -> Account:
    account = Account.objects.filter(owner_id=owner_id, kind=kind, archived=False).first()
    if account is None:
        raise Conflict({"code": "setup_required"})
    return account


def new_account(owner_id: int, kind: AccountKind, name: str) -> Account:
    account = Account.objects.create(owner_id=owner_id, kind=kind, name=name)
    Balance.objects.bulk_create(
        [Balance(account=account, currency=currency) for currency in Currency]
    )
    return account


def operation(portfolio: Portfolio, kind: str, data: dict, **kwargs) -> Operation:
    check_date(portfolio, data["local_date"], data["timezone"])
    return Operation.objects.create(
        owner_id=portfolio.owner_id,
        kind=kind,
        local_date=data["local_date"],
        timezone=data["timezone"],
        note=data.get("note", ""),
        **kwargs,
    )


def move(operation: Operation, account: Account, currency: str, delta: Decimal) -> None:
    if delta == 0:
        return
    balance = Balance.objects.get(account=account, currency=currency)
    updated = balance.amount + delta
    if updated < ZERO:
        raise Conflict(
            {
                "code": "insufficient_funds",
                "account_id": account.pk,
                "currency": currency,
                "shortfall": money(-updated),
            }
        )
    if updated > MAX_MONEY:
        raise ValidationError({"amount": "Result exceeds the supported balance."})
    balance.amount = updated
    balance.save(update_fields=["amount"])
    LedgerEntry.objects.create(operation=operation, balance=balance, delta=delta)


def allocation(portfolio: Portfolio, source_id: int, currency: str, amount: Decimal) -> list[dict]:
    source = owned_account(portfolio.owner_id, source_id)
    available = Balance.objects.get(account=source, currency=currency).amount
    first = min(available, amount)
    allocations = []
    if source.kind != AccountKind.BUDGET:
        if available < amount:
            raise Conflict(
                {
                    "code": "insufficient_funds",
                    "currency": currency,
                    "shortfall": money(amount - available),
                }
            )
        first = amount
    if first:
        allocations.append({"account_id": source.pk, "amount": money(first), "currency": currency})
    remaining = amount - first
    if remaining:
        savings = system_account(portfolio.owner_id, AccountKind.MAIN)
        savings_amount = Balance.objects.get(account=savings, currency=currency).amount
        if savings_amount < remaining:
            raise Conflict(
                {
                    "code": "insufficient_funds",
                    "currency": currency,
                    "shortfall": money(remaining - savings_amount),
                }
            )
        allocations.append(
            {"account_id": savings.pk, "amount": money(remaining), "currency": currency}
        )
    return allocations


def spend(portfolio: Portfolio, item: Operation, data: dict) -> None:
    expected = allocation(portfolio, data["source_id"], data["currency"], data["amount"])
    source = owned_account(portfolio.owner_id, data["source_id"])
    uses_savings = source.kind == AccountKind.BUDGET and any(
        row["account_id"] != source.pk for row in expected
    )
    confirmed = data.get("allocations")
    if confirmed is not None:
        confirmed = [
            {
                "account_id": row["account_id"],
                "amount": money(row["amount"]),
                "currency": data["currency"],
            }
            for row in confirmed
        ]
    if (uses_savings and confirmed is None) or (confirmed is not None and confirmed != expected):
        raise Conflict({"code": "split_confirmation_required", "allocations": expected})
    for row in expected:
        move(
            item,
            owned_account(portfolio.owner_id, row["account_id"]),
            row["currency"],
            -Decimal(row["amount"]),
        )


def initialize(portfolio: Portfolio, data: dict) -> dict:
    if portfolio.active_month is not None:
        raise Conflict({"code": "already_initialized"})
    portfolio.active_month = data["month"]
    BudgetMonth.objects.create(owner_id=portfolio.owner_id, month=data["month"])
    for kind, name, amounts in [
        (AccountKind.BUDGET, "Budget", data["budget"]),
        (AccountKind.MAIN, "Main Savings", data["main_savings"]),
    ]:
        account = new_account(portfolio.owner_id, kind, name)
        item = operation(
            portfolio, "opening", {"local_date": data["month"], "timezone": data["timezone"]}
        )
        for currency in Currency:
            move(item, account, currency, amounts[currency])
    Category.objects.bulk_create(
        [
            Category(owner_id=portfolio.owner_id, name_en=english, name_ar=arabic)
            for english, arabic in STARTER_CATEGORIES
        ]
    )
    return {"active_month": portfolio.active_month}


def create_account(portfolio: Portfolio, data: dict) -> dict:
    account = new_account(portfolio.owner_id, AccountKind.NAMED, data["name"])
    item = operation(portfolio, "opening", data)
    for currency in Currency:
        move(item, account, currency, data["opening"][currency])
    return {"account_id": account.pk, "operation_id": item.pk}


def change_account(portfolio: Portfolio, account_id: int, data: dict) -> dict:
    account = owned_account(portfolio.owner_id, account_id, active=False)
    if account.kind != AccountKind.NAMED:
        raise ValidationError({"account": "System accounts cannot be renamed or archived."})
    if data.get("archived") and account.balances.exclude(amount=ZERO).exists():
        raise Conflict({"code": "nonzero_account"})
    for field in ("name", "archived"):
        if field in data:
            setattr(account, field, data[field])
    account.save()
    return {"account_id": account.pk}


def change_preferences(portfolio: Portfolio, data: dict) -> dict:
    for field, value in data.items():
        setattr(portfolio, field, value)
    return {"updated": True}


def change_category(portfolio: Portfolio, category_id: int | None, data: dict) -> dict:
    if category_id is None:
        category = Category.objects.create(owner_id=portfolio.owner_id, **data)
    else:
        category = Category.objects.filter(owner_id=portfolio.owner_id, pk=category_id).first()
        if category is None:
            raise NotFound()
        for field, value in data.items():
            setattr(category, field, value)
        category.save()
    return {"category_id": category.pk}


def transact(portfolio: Portfolio, kind: str, data: dict) -> dict:
    item = operation(portfolio, kind, data)
    details = {
        key: str(value) if isinstance(value, (date, Decimal)) else value
        for key, value in data.items()
        if key != "allocations"
    }
    if kind == "expense":
        category = Category.objects.filter(
            owner_id=portfolio.owner_id, pk=data["category_id"], archived=False
        ).first()
        if category is None:
            raise NotFound("Category not found.")
        item.category = category
        spend(portfolio, item, data)
    elif kind == "income":
        move(
            item,
            system_account(portfolio.owner_id, AccountKind.BUDGET),
            data["currency"],
            data["amount"],
        )
    elif kind == "transfer":
        if data["source_id"] == data["target_id"]:
            raise ValidationError({"target_id": "Choose a different account."})
        move(
            item,
            owned_account(portfolio.owner_id, data["source_id"]),
            data["currency"],
            -data["amount"],
        )
        move(
            item,
            owned_account(portfolio.owner_id, data["target_id"]),
            data["currency"],
            data["amount"],
        )
    elif kind == "exchange":
        account = owned_account(portfolio.owner_id, data["source_id"])
        rate, received, target_currency = exchange_values(portfolio, data)
        move(item, account, data["currency"], -data["amount"])
        move(item, account, target_currency, received)
        details.update(rate=str(rate), received=money(received), received_currency=target_currency)
    else:
        raise ValidationError({"kind": "Unsupported operation."})
    item.details = details
    item.save(update_fields=["details", "category"])
    return {"operation_id": item.pk, "details": details}


def exchange_values(portfolio: Portfolio, data: dict) -> tuple[Decimal, Decimal, str]:
    rate = data.get("rate", portfolio.exchange_rate)
    with localcontext() as context:
        context.prec = 60
        received = (
            data["amount"] / rate if data["currency"] == Currency.IQD else data["amount"] * rate
        ).quantize(CENT, rounding=ROUND_HALF_UP)
    if received <= ZERO or received > MAX_MONEY:
        raise ValidationError(
            {"amount": "Received amount must be within the supported positive range."}
        )
    return rate, received, Currency.USD if data["currency"] == Currency.IQD else Currency.IQD


def exchange_preview(portfolio: Portfolio, data: dict) -> dict:
    check_date(portfolio, data["local_date"], data["timezone"])
    account = owned_account(portfolio.owner_id, data["source_id"])
    rate, received, target = exchange_values(portfolio, data)
    balances = {row.currency: row.amount for row in account.balances.all()}
    if balances[data["currency"]] < data["amount"]:
        raise Conflict(
            {
                "code": "insufficient_funds",
                "shortfall": money(data["amount"] - balances[data["currency"]]),
            }
        )
    if balances[target] + received > MAX_MONEY:
        raise ValidationError({"amount": "Result exceeds supported balance."})
    return {
        "rate": str(rate),
        "deducted": money(data["amount"]),
        "received": money(received),
        "received_currency": target,
        "before": {currency: money(value) for currency, value in balances.items()},
        "after": {
            data["currency"]: money(balances[data["currency"]] - data["amount"]),
            target: money(balances[target] + received),
        },
    }


def rollover_preview(portfolio: Portfolio, data: dict) -> dict:
    if portfolio.active_month is None or data["month"] <= portfolio.active_month:
        raise Conflict({"code": "month_not_forward"})
    budget = system_account(portfolio.owner_id, AccountKind.BUDGET)
    savings = system_account(portfolio.owner_id, AccountKind.MAIN)
    budget_amounts = {row.currency: row.amount for row in budget.balances.all()}
    savings_amounts = {row.currency: row.amount for row in savings.balances.all()}
    result = {}
    for currency in Currency:
        combined = budget_amounts[currency] + savings_amounts[currency]
        if combined > MAX_MONEY:
            raise ValidationError({"month": "Savings would exceed the supported balance."})
        result[currency] = {
            "to_main_savings": money(budget_amounts[currency]),
            "main_savings_after": money(combined),
            "budget_after": money(data["salary"][currency]),
        }
    return result


def rollover(portfolio: Portfolio, data: dict) -> dict:
    rollover_preview(portfolio, data)
    if portfolio.active_month is None or data["month"] <= portfolio.active_month:
        raise Conflict({"code": "month_not_forward"})
    old_month = portfolio.active_month
    portfolio.active_month = data["month"]
    BudgetMonth.objects.filter(owner_id=portfolio.owner_id, month=old_month).update(closed=True)
    BudgetMonth.objects.create(owner_id=portfolio.owner_id, month=data["month"])
    item = operation(
        portfolio,
        "rollover",
        {"local_date": data["month"], "timezone": data["timezone"]},
        details={
            "previous_month": str(old_month),
            "salary": {currency: money(data["salary"][currency]) for currency in Currency},
        },
    )
    budget = system_account(portfolio.owner_id, AccountKind.BUDGET)
    savings = system_account(portfolio.owner_id, AccountKind.MAIN)
    salary_operations = []
    for currency in Currency:
        remaining = Balance.objects.get(account=budget, currency=currency).amount
        move(item, budget, currency, -remaining)
        move(item, savings, currency, remaining)
        if data["salary"][currency] > ZERO:
            salary_operations.append(
                transact(
                    portfolio,
                    "income",
                    {
                        "local_date": data["month"],
                        "timezone": data["timezone"],
                        "note": "Salary",
                        "currency": currency,
                        "amount": data["salary"][currency],
                    },
                )["operation_id"]
            )
    return {
        "operation_id": item.pk,
        "salary_operation_ids": salary_operations,
        "active_month": portfolio.active_month,
    }


def reverse(portfolio: Portfolio, operation_id: int, data: dict) -> dict:
    original = Operation.objects.filter(owner_id=portfolio.owner_id, pk=operation_id).first()
    if original is None:
        raise NotFound()
    if original.kind not in {"expense", "income", "transfer", "exchange"}:
        raise ValidationError({"operation": "This operation requires its own correction flow."})
    if month_start(original.local_date) != portfolio.active_month:
        raise Conflict({"code": "closed_month"})
    if hasattr(original, "reversal"):
        raise Conflict({"code": "already_reversed"})
    item = operation(portfolio, "reversal", data, reverses=original)
    # Restore the exact stored allocations; historical rates are never recalculated.
    for entry in original.entries.select_related("balance__account").order_by("balance_id"):
        account = entry.balance.account
        if account.archived:
            raise Conflict({"code": "archived_account"})
        move(item, account, entry.balance.currency, -entry.delta)
    result = {"operation_id": item.pk}
    if "replacement" in data:
        replacement = data["replacement"]
        if replacement["kind"] != original.kind:
            raise ValidationError({"replacement": "Replacement must have the same operation kind."})
        result["replacement"] = transact(portfolio, original.kind, replacement["data"])
    return result
