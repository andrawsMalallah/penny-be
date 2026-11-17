from django.db import transaction
from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import ValidationError

from identity.models import User, UserSettings

from .models import Account, AccountGroup, OpeningBalance


def accounts_for(user: User) -> QuerySet[Account]:
    return Account.objects.filter(user=user).select_related("group", "opening")


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
    unique_name(Account, user, values["name"], account_id)
    account.name = values["name"]
    account.group = available_group(user, values["group"])
    account.save()
    if account_id is None:
        OpeningBalance.objects.create(
            account=account, iqd=values["opening_iqd"], usd=values["opening_usd"]
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
