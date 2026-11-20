from decimal import Decimal

from django.db import transaction
from django.db.models import (
    Case,
    DecimalField,
    F,
    OuterRef,
    Q,
    QuerySet,
    Subquery,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError

from app.finance.models import Account, AccountGroup, MoneyTransaction, OpeningBalance
from app.finance.services.ledger import opening_date
from app.identity.models import User, UserSettings


class AccountTargetConflict(APIException):
    status_code = 409
    default_detail = "These targets changed. Cancel and refresh Accounts before editing again."


def accounts_for(user: User) -> QuerySet[Account]:
    money_field = DecimalField(max_digits=30, decimal_places=2)
    annotations = {}
    for currency in ("IQD", "USD"):
        movements = (
            MoneyTransaction.objects.filter(
                Q(account=OuterRef("pk")) | Q(destination=OuterRef("pk")),
                user=user,
                deleted_at__isnull=True,
            )
            .filter(Q(currency=currency) | Q(kind="exchange"))
            .order_by()
            # Group the correlated aggregate once for this user, across both sides.
            # Grouping by source account would split incoming transfers into rows.
            .annotate(owner_id=Value(user.pk))
            .values("owner_id")
            .annotate(
                total=Sum(
                    Case(
                        When(Q(kind="exchange") & ~Q(currency=currency), then=F("received_amount")),
                        When(kind__in=["income", "debt_in"], then=F("amount")),
                        When(destination=OuterRef("pk"), then=F("amount")),
                        default=-F("amount"),
                        output_field=money_field,
                    )
                )
            )
            .values("total")
        )
        annotations[f"balance_{currency.lower()}"] = F(f"opening__{currency.lower()}") + Coalesce(
            Subquery(movements, output_field=money_field),
            Value(Decimal("0")),
            output_field=money_field,
        )
    return (
        Account.objects.filter(user=user).select_related("group", "opening").annotate(**annotations)
    )


def available_group(user: User, group_id: int | None) -> AccountGroup | None:
    if group_id is None:
        return None
    group = AccountGroup.objects.filter(user=user, pk=group_id, archived=False).first()
    if group is None:
        raise ValidationError({"group": ["Choose an active group from your accounts."]})
    return group


def unique_name(
    model: type[Account] | type[AccountGroup], user: User, name: str, pk: int | None
) -> None:
    if model.objects.filter(user=user, name__iexact=name).exclude(pk=pk).exists():
        raise ValidationError({"name": ["This name is already used, including archived items."]})


@transaction.atomic
def save_group(user: User, values: dict, group_id: int | None = None) -> AccountGroup:
    User.objects.select_for_update().get(pk=user.pk)
    group = (
        get_object_or_404(AccountGroup, user=user, pk=group_id)
        if group_id
        else AccountGroup(user=user)
    )
    unique_name(AccountGroup, user, values["name"], group_id)
    group.name = values["name"]
    group.save()
    return group


@transaction.atomic
def save_account(user: User, values: dict, account_id: int | None = None) -> Account:
    # Finance/settings writes share the user lock so archive/default changes cannot race.
    User.objects.select_for_update().get(pk=user.pk)
    account = (
        get_object_or_404(Account, user=user, pk=account_id) if account_id else Account(user=user)
    )
    if account.archived:
        raise ValidationError({"name": ["Restore this account before editing it."]})
    target_fields = {"target_iqd", "target_usd"} & values.keys()
    # Older metadata-only requests preserve targets. Target changes require the read version.
    if account_id and target_fields:
        if "target_version" not in values:
            raise ValidationError(
                {"target_version": ["Refresh the account before editing targets."]}
            )
        if values["target_version"] != account.target_version:
            raise AccountTargetConflict()
        account.target_version += 1
    for field in target_fields:
        setattr(account, field, values[field])
    unique_name(Account, user, values["name"], account_id)
    account.name = values["name"]
    account.group = available_group(user, values["group"])
    account.save()
    if account_id is None:
        OpeningBalance.objects.create(
            account=account,
            iqd=values["opening_iqd"],
            usd=values["opening_usd"],
            effective_date=opening_date(timezone.now(), values.get("timezone", user.timezone)),
        )
    return accounts_for(user).get(pk=account.pk)


@transaction.atomic
def archive_account(user: User, account_id: int, archived: bool) -> Account:
    User.objects.select_for_update().get(pk=user.pk)
    account = get_object_or_404(accounts_for(user), pk=account_id)
    if not archived:
        available_group(user, account.group_id)
    account.archived = archived
    account.save(update_fields=["archived"])
    if archived:
        UserSettings.objects.filter(user=user, default_account=account).update(default_account=None)
    return account


@transaction.atomic
def archive_group(user: User, group_id: int, archived: bool) -> AccountGroup:
    User.objects.select_for_update().get(pk=user.pk)
    group = get_object_or_404(AccountGroup, pk=group_id, user=user)
    if archived and Account.objects.filter(group=group, archived=False).exists():
        raise ValidationError({"group": ["Move or archive this group's active accounts first."]})
    group.archived = archived
    group.save(update_fields=["archived"])
    return group
