from decimal import ROUND_HALF_UP, Decimal, localcontext

from rest_framework.exceptions import NotFound, ValidationError

from apps.debts.models import Debt, DebtEvent, Direction, Person
from apps.finances.models import Currency, Portfolio
from apps.finances.services import core


def owned_person(portfolio: Portfolio, person_id: int, *, active: bool = True) -> Person:
    query = Person.objects.filter(owner_id=portfolio.owner_id, pk=person_id)
    if active:
        query = query.filter(archived=False)
    person = query.first()
    if person is None:
        raise NotFound("Person not found.")
    return person


def owned_debt(portfolio: Portfolio, debt_id: int) -> Debt:
    debt = Debt.objects.filter(owner_id=portfolio.owner_id, pk=debt_id, cancelled=False).first()
    if debt is None:
        raise NotFound("Debt not found.")
    owned_person(portfolio, debt.person_id)
    return debt


def change_person(portfolio: Portfolio, person_id: int | None, data: dict) -> dict:
    if person_id is None:
        person = Person.objects.create(owner_id=portfolio.owner_id, **data)
    else:
        person = owned_person(portfolio, person_id, active=False)
        if (
            data.get("archived")
            and person.debts.filter(cancelled=False, remaining_amount__gt=0).exists()
        ):
            raise core.Conflict({"code": "unsettled_person"})
        for field, value in data.items():
            setattr(person, field, value)
        person.save()
    return {"person_id": person.pk}


def account_movement(
    portfolio: Portfolio, item, data: dict, *, outgoing: bool, amount: Decimal, currency: str
) -> None:
    if data["movement"] == "record_only":
        return
    account = core.owned_account(portfolio.owner_id, data["account_id"])
    if outgoing:
        core.spend(
            portfolio,
            item,
            {
                "source_id": account.pk,
                "amount": amount,
                "currency": currency,
                **({"allocations": data["allocations"]} if "allocations" in data else {}),
            },
        )
    else:
        core.move(item, account, currency, amount)


def create_debt(portfolio: Portfolio, data: dict) -> dict:
    person = owned_person(portfolio, data["person_id"])
    item = core.operation(portfolio, "debt_create", data)
    account_movement(
        portfolio,
        item,
        data,
        outgoing=data["direction"] == Direction.RECEIVABLE,
        amount=data["amount"],
        currency=data["currency"],
    )
    debt = Debt.objects.create(
        owner_id=portfolio.owner_id,
        person=person,
        direction=data["direction"],
        currency=data["currency"],
        original_amount=data["amount"],
        remaining_amount=data["amount"],
        note=data.get("note", ""),
        due_date=data.get("due_date"),
    )
    item.details = {
        "debt_id": debt.pk,
        "movement": data["movement"],
        "currency": data["currency"],
        "amount": core.money(data["amount"]),
    }
    item.save(update_fields=["details"])
    event = DebtEvent.objects.create(debt=debt, operation=item, remaining_delta=data["amount"])
    return {"debt_id": debt.pk, "event_id": event.pk, "operation_id": item.pk}


def settle(portfolio: Portfolio, debt_id: int, kind: str, data: dict) -> dict:
    debt = owned_debt(portfolio, debt_id)
    amount = data["settled_amount"]
    if amount > debt.remaining_amount:
        raise core.Conflict(
            {"code": "over_settlement", "remaining_amount": core.money(debt.remaining_amount)}
        )
    item = core.operation(portfolio, kind, data)
    details = {
        "debt_id": debt.pk,
        "settled_amount": core.money(amount),
        "debt_currency": debt.currency,
    }
    if kind == "debt_repayment":
        payment = data["payment_amount"]
        currency = data["payment_currency"]
        if currency == debt.currency and payment != amount:
            raise ValidationError(
                {"payment_amount": "Same-currency payment must equal the settled amount."}
            )
        with localcontext() as context:
            context.prec = 60
            rate = (
                Decimal("1")
                if currency == debt.currency
                else (payment / amount if currency == Currency.IQD else amount / payment)
            )
            rate = rate.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        if rate <= 0 or rate > Decimal("999999999999.999999"):
            raise ValidationError(
                {"payment_amount": "Effective rate is outside the supported range."}
            )
        details.update(
            payment_amount=core.money(payment),
            payment_currency=currency,
            effective_rate=str(rate),
            movement=data["movement"],
        )
        account_movement(
            portfolio,
            item,
            data,
            outgoing=debt.direction == Direction.PAYABLE,
            amount=payment,
            currency=currency,
        )
    else:
        details["reason"] = data["reason"]
    item.details = details
    item.save(update_fields=["details"])
    debt.remaining_amount -= amount
    debt.save(update_fields=["remaining_amount"])
    event = DebtEvent.objects.create(debt=debt, operation=item, remaining_delta=-amount)
    return {
        "event_id": event.pk,
        "operation_id": item.pk,
        "remaining_amount": core.money(debt.remaining_amount),
    }


def reverse_event(portfolio: Portfolio, event_id: int, data: dict) -> dict:
    original = (
        DebtEvent.objects.select_related("operation", "debt")
        .filter(debt__owner_id=portfolio.owner_id, pk=event_id)
        .first()
    )
    if original is None:
        raise NotFound()
    debt = original.debt
    if original.operation.kind == "debt_reversal" or hasattr(original.operation, "reversal"):
        raise core.Conflict({"code": "already_reversed"})
    owned_person(portfolio, debt.person_id)
    if original.operation.kind == "debt_create":
        blockers = list(
            debt.events.filter(operation__reversal__isnull=True)
            .exclude(pk=original.pk)
            .exclude(operation__kind="debt_reversal")
            .order_by("id")
            .values_list("id", flat=True)[:51]
        )
        if blockers:
            raise core.Conflict(
                {"code": "dependent_events", "event_ids": blockers[:50], "more": len(blockers) > 50}
            )
    remaining = debt.remaining_amount - original.remaining_delta
    if remaining < 0 or remaining > debt.original_amount or debt.cancelled:
        raise core.Conflict({"code": "invalid_debt_history"})
    item = core.operation(
        portfolio,
        "debt_reversal",
        data,
        reverses=original.operation,
        details={"debt_id": debt.pk, "event_id": original.pk},
    )
    for entry in original.operation.entries.select_related("balance__account").order_by(
        "balance_id"
    ):
        if entry.balance.account.archived:
            raise core.Conflict({"code": "archived_account"})
        core.move(item, entry.balance.account, entry.balance.currency, -entry.delta)
    debt.remaining_amount = remaining
    debt.cancelled = original.operation.kind == "debt_create"
    debt.save(update_fields=["remaining_amount", "cancelled"])
    event = DebtEvent.objects.create(
        debt=debt, operation=item, remaining_delta=-original.remaining_delta
    )
    result = {
        "event_id": event.pk,
        "operation_id": item.pk,
        "remaining_amount": core.money(remaining),
    }
    if "replacement" in data:
        replacement = data["replacement"]
        if replacement["kind"] != original.operation.kind:
            raise ValidationError({"replacement": "Use the original debt event kind."})
        result["replacement"] = (
            create_debt(portfolio, replacement["data"])
            if original.operation.kind == "debt_create"
            else settle(portfolio, debt.pk, original.operation.kind, replacement["data"])
        )
    return result
