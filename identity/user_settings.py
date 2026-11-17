from django.db import transaction

from .models import User, UserSettings


def read_user_settings(user: User) -> dict:
    preferences = UserSettings.objects.filter(user=user).first()
    return {
        "language": user.language,
        "default_account": preferences.default_account_id if preferences else None,
        "default_currency": preferences.default_currency if preferences else "",
        "exchange_rate": preferences.exchange_rate if preferences else None,
    }


@transaction.atomic
def save_user_settings(user: User, values: dict) -> dict:
    # Serialize settings writes with the existing per-user lock used by security changes.
    locked_user = User.objects.select_for_update().get(pk=user.pk)
    preferences, _ = UserSettings.objects.get_or_create(user=locked_user)
    if "default_account" in values:
        from rest_framework.exceptions import ValidationError

        from finance.models import Account

        account_id = values["default_account"]
        if (
            account_id is not None
            and not Account.objects.filter(pk=account_id, user=locked_user, archived=False).exists()
        ):
            raise ValidationError(
                {"default_account": ["Choose an active account from your accounts."]}
            )
        preferences.default_account_id = account_id
    preferences.default_currency = values["default_currency"]
    preferences.exchange_rate = values["exchange_rate"]
    preferences.save(update_fields=["default_currency", "exchange_rate", "default_account"])
    locked_user.language = values["language"]
    locked_user.save(update_fields=["language"])
    return read_user_settings(locked_user)
