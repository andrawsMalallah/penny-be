from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from rest_framework import serializers

from .models import (
    Account,
    AccountGroup,
    Currency,
    ExpenseCategory,
    MoneyTransaction,
    OpeningBalance,
    TransactionKind,
)


class StrictSerializer(serializers.Serializer):
    def to_internal_value(self, data: object) -> dict:
        if isinstance(data, dict) and set(data) - set(self.fields):
            raise serializers.ValidationError({"detail": "Unsupported field."})
        return super().to_internal_value(data)


class GroupInputSerializer(StrictSerializer):
    name = serializers.CharField(max_length=100)


class CategoryInputSerializer(StrictSerializer):
    name_en = serializers.CharField(max_length=100, allow_blank=True, default="")
    name_ar = serializers.CharField(max_length=100, allow_blank=True, default="")

    def validate(self, attrs: dict) -> dict:
        if not attrs["name_en"] and not attrs["name_ar"]:
            raise serializers.ValidationError({"name_en": ["Enter at least one category name."]})
        return attrs


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ExpenseCategory
        fields = ["id", "name_en", "name_ar", "archived"]
        read_only_fields = fields


class MoneyField(serializers.DecimalField):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(max_digits=18, decimal_places=2, min_value=Decimal("0"), **kwargs)

    def to_internal_value(self, data: object) -> Decimal:
        if not isinstance(data, str):
            raise serializers.ValidationError("Use a decimal string with up to two decimal places.")
        return super().to_internal_value(data)


class TargetMoneyField(MoneyField):
    def validate_empty_values(self, data: object) -> tuple[bool, object]:
        # Removal is explicit null; do not let DecimalField silently coerce blank strings.
        if isinstance(data, str) and not data.strip():
            self.fail("invalid")
        return super().validate_empty_values(data)


class AccountInputSerializer(GroupInputSerializer):
    group = serializers.IntegerField(min_value=1, allow_null=True)
    target_iqd = TargetMoneyField(allow_null=True, required=False)
    target_usd = TargetMoneyField(allow_null=True, required=False)
    target_version = serializers.IntegerField(min_value=1, required=False)

    def validate(self, attrs: dict) -> dict:
        for field in ("target_iqd", "target_usd"):
            if attrs.get(field) is not None and attrs[field] <= 0:
                raise serializers.ValidationError(
                    {field: ["Enter a positive target or leave it empty."]}
                )
        return attrs


class AccountCreateSerializer(AccountInputSerializer):
    opening_iqd = MoneyField()
    opening_usd = MoneyField()
    timezone = serializers.CharField(max_length=100, required=False)

    def validate_timezone(self, value: str) -> str:
        return valid_timezone(value)


class ArchiveSerializer(StrictSerializer):
    archived = serializers.BooleanField()


class GroupSerializer(serializers.ModelSerializer):
    class Meta:
        model = AccountGroup
        fields = ["id", "name", "archived"]
        read_only_fields = fields


class OpeningSerializer(serializers.ModelSerializer):
    class Meta:
        model = OpeningBalance
        fields = ["iqd", "usd", "recorded_at", "effective_date"]
        read_only_fields = fields


class AccountSerializer(serializers.ModelSerializer):
    group_name = serializers.CharField(source="group.name", default=None)
    balance_iqd = serializers.DecimalField(max_digits=18, decimal_places=2)
    balance_usd = serializers.DecimalField(max_digits=18, decimal_places=2)
    opening = OpeningSerializer()

    class Meta:
        model = Account
        fields = [
            "id",
            "name",
            "group",
            "group_name",
            "archived",
            "created_at",
            "balance_iqd",
            "balance_usd",
            "target_iqd",
            "target_usd",
            "target_version",
            "opening",
        ]
        read_only_fields = fields


def valid_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise serializers.ValidationError("Choose a valid device timezone.") from error
    return value


class TransactionInputSerializer(StrictSerializer):
    exchange_rate = serializers.DecimalField(
        max_digits=18,
        decimal_places=6,
        min_value=Decimal("0.000001"),
        allow_null=True,
        default=None,
    )
    received_amount = MoneyField(allow_null=True, default=None)
    account = serializers.IntegerField(min_value=1)
    destination = serializers.IntegerField(min_value=1, allow_null=True, default=None)
    category = serializers.IntegerField(min_value=1, allow_null=True, default=None)
    kind = serializers.ChoiceField(choices=TransactionKind.choices)
    currency = serializers.ChoiceField(choices=Currency.choices)
    amount = MoneyField()
    date = serializers.DateField(input_formats=["%Y-%m-%d"])
    note = serializers.CharField(max_length=500, allow_blank=True, default="")
    timezone = serializers.CharField(max_length=100)

    def to_internal_value(self, data: object) -> dict:
        if (
            isinstance(data, dict)
            and data.get("exchange_rate") is not None
            and not isinstance(data["exchange_rate"], str)
        ):
            raise serializers.ValidationError({"exchange_rate": ["Use a decimal string."]})
        return super().to_internal_value(data)

    def validate_timezone(self, value: str) -> str:
        return valid_timezone(value)

    def validate(self, attrs: dict) -> dict:
        if attrs["amount"] <= 0:
            raise serializers.ValidationError({"amount": ["Enter an amount greater than zero."]})
        if (attrs["kind"] == TransactionKind.EXPENSE) != (attrs["category"] is not None):
            raise serializers.ValidationError({"category": ["Only expenses require a category."]})
        if (attrs["kind"] == TransactionKind.TRANSFER) != (attrs["destination"] is not None):
            raise serializers.ValidationError(
                {"destination": ["Only transfers need a destination."]}
            )
        if attrs["destination"] == attrs["account"]:
            raise serializers.ValidationError({"destination": ["Choose a different account."]})
        if attrs["kind"] == TransactionKind.EXCHANGE:
            if attrs["exchange_rate"] is None:
                raise serializers.ValidationError({"exchange_rate": ["Enter the exchange rate."]})
            if attrs["received_amount"] is None:
                raise serializers.ValidationError(
                    {"received_amount": ["Review the received amount first."]}
                )
        elif attrs["exchange_rate"] is not None or attrs["received_amount"] is not None:
            raise serializers.ValidationError(
                {"exchange_rate": ["Only exchanges have a rate and received amount."]}
            )
        return attrs


class TransactionCreateSerializer(TransactionInputSerializer):
    request_id = serializers.UUIDField()


class TransactionUpdateSerializer(TransactionInputSerializer):
    version = serializers.IntegerField(min_value=1)


class TransactionDeleteSerializer(StrictSerializer):
    version = serializers.IntegerField(min_value=1)


class TransactionFilterSerializer(StrictSerializer):
    page = serializers.IntegerField(min_value=1, required=False)
    account = serializers.IntegerField(min_value=1, required=False)
    category = serializers.IntegerField(min_value=1, required=False)
    kind = serializers.ChoiceField(choices=TransactionKind.choices, required=False)
    currency = serializers.ChoiceField(choices=Currency.choices, required=False)
    start = serializers.DateField(input_formats=["%Y-%m-%d"], required=False)
    end = serializers.DateField(input_formats=["%Y-%m-%d"], required=False)

    def validate(self, attrs: dict) -> dict:
        if attrs.get("start") and attrs.get("end") and attrs["start"] > attrs["end"]:
            raise serializers.ValidationError(
                {"end": ["Choose an end date on or after the start date."]}
            )
        return attrs


class TransactionSerializer(serializers.ModelSerializer):
    debt_person_name = serializers.CharField(source="debt.person.name", default="")
    destination_name = serializers.CharField(source="destination.name", default="")
    destination_archived = serializers.BooleanField(source="destination.archived", default=False)
    destination_opening_date = serializers.DateField(
        source="destination.opening.effective_date", default=None
    )
    account_name = serializers.CharField(source="account.name")
    account_archived = serializers.BooleanField(source="account.archived")
    category_name_en = serializers.CharField(source="category.name_en", default="")
    category_name_ar = serializers.CharField(source="category.name_ar", default="")
    category_archived = serializers.BooleanField(source="category.archived", default=False)
    opening_date = serializers.DateField(source="account.opening.effective_date")

    class Meta:
        model = MoneyTransaction
        fields = [
            "id",
            "debt",
            "debt_role",
            "debt_person_name",
            "account",
            "account_name",
            "account_archived",
            "destination",
            "destination_name",
            "destination_archived",
            "destination_opening_date",
            "category",
            "category_name_en",
            "category_name_ar",
            "category_archived",
            "kind",
            "currency",
            "amount",
            "exchange_rate",
            "received_amount",
            "date",
            "note",
            "version",
            "opening_date",
        ]
        read_only_fields = fields
