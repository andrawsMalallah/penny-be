from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from rest_framework import serializers

from apps.accounts.api.serializers import StrictSerializer
from apps.finances.models import (
    Account,
    Balance,
    BudgetMonth,
    Category,
    Currency,
    LedgerEntry,
    Operation,
    Portfolio,
)


class MoneyField(serializers.DecimalField):
    def __init__(self, **kwargs):
        super().__init__(max_digits=18, decimal_places=2, **kwargs)

    def to_internal_value(self, data):
        if not isinstance(data, str):
            raise serializers.ValidationError("Use a decimal string, not a JSON number.")
        return super().to_internal_value(data)


class RateField(MoneyField):
    def __init__(self, **kwargs):
        serializers.DecimalField.__init__(
            self, max_digits=18, decimal_places=6, min_value=Decimal("0.000001"), **kwargs
        )


class AmountsSerializer(StrictSerializer):
    IQD = MoneyField(min_value=Decimal("0"))
    USD = MoneyField(min_value=Decimal("0"))


class TimezoneField(serializers.CharField):
    def __init__(self, **kwargs):
        super().__init__(max_length=64, **kwargs)

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError, ValueError:
            raise serializers.ValidationError("Use an IANA timezone.") from None
        return value


class MonthField(serializers.DateField):
    def to_internal_value(self, value):
        parsed = super().to_internal_value(value)
        if parsed.day != 1:
            raise serializers.ValidationError("Use the first day of the month.")
        return parsed


class SetupSerializer(StrictSerializer):
    month = MonthField()
    timezone = TimezoneField()
    budget = AmountsSerializer()
    main_savings = AmountsSerializer()


class DatedSerializer(StrictSerializer):
    expected_revision = serializers.IntegerField(min_value=0, required=False)
    local_date = serializers.DateField()
    timezone = TimezoneField()
    note = serializers.CharField(max_length=1000, allow_blank=True, default="")


class AccountCreateSerializer(DatedSerializer):
    name = serializers.CharField(max_length=80)
    opening = AmountsSerializer()


class AccountChangeSerializer(StrictSerializer):
    name = serializers.CharField(max_length=80, required=False)
    archived = serializers.BooleanField(required=False)


class CategoryCreateSerializer(StrictSerializer):
    name_en = serializers.CharField(max_length=80)
    name_ar = serializers.CharField(max_length=80)


class CategoryChangeSerializer(CategoryCreateSerializer):
    name_en = serializers.CharField(max_length=80, required=False)
    name_ar = serializers.CharField(max_length=80, required=False)
    archived = serializers.BooleanField(required=False)


class PreferencesChangeSerializer(StrictSerializer):
    exchange_rate = RateField(required=False)
    language = serializers.ChoiceField(choices=["system", "en", "ar"], required=False)
    theme = serializers.ChoiceField(choices=["system", "light", "dark"], required=False)


class AllocationSerializer(StrictSerializer):
    account_id = serializers.IntegerField(min_value=1)
    amount = MoneyField(min_value=Decimal("0.01"))


class SpendingSerializer(DatedSerializer):
    source_id = serializers.IntegerField(min_value=1)
    currency = serializers.ChoiceField(choices=Currency.values)
    amount = MoneyField(min_value=Decimal("0.01"))
    allocations = AllocationSerializer(many=True, required=False, max_length=2)


class ExpenseSerializer(SpendingSerializer):
    category_id = serializers.IntegerField(min_value=1)


class IncomeSerializer(DatedSerializer):
    currency = serializers.ChoiceField(choices=Currency.values)
    amount = MoneyField(min_value=Decimal("0.01"))


class TransferSerializer(IncomeSerializer):
    source_id = serializers.IntegerField(min_value=1)
    target_id = serializers.IntegerField(min_value=1)


class ExchangeSerializer(IncomeSerializer):
    source_id = serializers.IntegerField(min_value=1)
    rate = RateField(required=False)


class RolloverSerializer(StrictSerializer):
    expected_revision = serializers.IntegerField(min_value=0, required=False)
    month = MonthField()
    timezone = TimezoneField()
    salary = AmountsSerializer()


TRANSACTION_SERIALIZERS = {
    "expense": ExpenseSerializer,
    "income": IncomeSerializer,
    "transfer": TransferSerializer,
    "exchange": ExchangeSerializer,
}


class ReplacementSerializer(StrictSerializer):
    kind = serializers.ChoiceField(choices=list(TRANSACTION_SERIALIZERS))
    data = serializers.DictField()

    def validate(self, attrs):
        nested = TRANSACTION_SERIALIZERS[attrs["kind"]](data=attrs["data"])
        nested.is_valid(raise_exception=True)
        attrs["data"] = nested.validated_data
        return attrs


class ReversalSerializer(DatedSerializer):
    replacement = ReplacementSerializer(required=False)


class BalanceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Balance
        fields = ["currency", "amount"]


class AccountSerializer(serializers.ModelSerializer):
    balances = BalanceSerializer(many=True, read_only=True)

    class Meta:
        model = Account
        fields = ["id", "kind", "name", "archived", "balances"]


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ["id", "name_en", "name_ar", "archived"]


class PreferencesSerializer(serializers.ModelSerializer):
    class Meta:
        model = Portfolio
        fields = ["active_month", "revision", "exchange_rate", "language", "theme"]


class MonthSerializer(serializers.ModelSerializer):
    class Meta:
        model = BudgetMonth
        fields = ["id", "month", "closed"]


class EntrySerializer(serializers.ModelSerializer):
    account_id = serializers.IntegerField(source="balance.account_id")
    currency = serializers.CharField(source="balance.currency")

    class Meta:
        model = LedgerEntry
        fields = ["account_id", "currency", "delta"]


class OperationSerializer(serializers.ModelSerializer):
    entries = EntrySerializer(many=True)
    reversal_id = serializers.SerializerMethodField()

    def get_reversal_id(self, item: Operation) -> int | None:
        return item.reversal.pk if hasattr(item, "reversal") else None

    class Meta:
        model = Operation
        fields = [
            "id",
            "kind",
            "local_date",
            "timezone",
            "note",
            "category_id",
            "details",
            "created_at",
            "reverses_id",
            "reversal_id",
            "entries",
        ]


class ActivityFilters(StrictSerializer):
    cursor = serializers.CharField(required=False, max_length=2048)
    start = serializers.DateField(required=False)
    end = serializers.DateField(required=False)
    currency = serializers.ChoiceField(choices=Currency.values, required=False)
    kind = serializers.CharField(max_length=24, required=False)
    category_id = serializers.IntegerField(min_value=1, required=False)
    account_id = serializers.IntegerField(min_value=1, required=False)

    def validate(self, attrs):
        if "start" in attrs and "end" in attrs and attrs["start"] > attrs["end"]:
            raise serializers.ValidationError("Start must not follow end.")
        return attrs


class CursorFilters(StrictSerializer):
    cursor = serializers.CharField(required=False, max_length=2048)


class MetadataFilters(CursorFilters):
    archived = serializers.BooleanField(required=False)
