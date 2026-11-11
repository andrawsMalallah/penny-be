from decimal import Decimal

from rest_framework import serializers

from apps.accounts.api.serializers import StrictSerializer
from apps.debts.models import Debt, DebtEvent, Direction, Person
from apps.finances.api.serializers import (
    AllocationSerializer,
    CursorFilters,
    DatedSerializer,
    MoneyField,
    OperationSerializer,
)
from apps.finances.models import Currency


class PersonCreateSerializer(StrictSerializer):
    name = serializers.CharField(max_length=80)
    phone = serializers.CharField(max_length=40, allow_blank=True, default="")
    note = serializers.CharField(max_length=1000, allow_blank=True, default="")


class PersonChangeSerializer(StrictSerializer):
    name = serializers.CharField(max_length=80, required=False)
    phone = serializers.CharField(max_length=40, allow_blank=True, required=False)
    note = serializers.CharField(max_length=1000, allow_blank=True, required=False)
    archived = serializers.BooleanField(required=False)


class MovementSerializer(DatedSerializer):
    movement = serializers.ChoiceField(choices=["record_only", "account"])
    account_id = serializers.IntegerField(min_value=1, required=False)
    allocations = AllocationSerializer(many=True, required=False, max_length=2)

    def validate(self, attrs):
        if attrs["movement"] == "account" and "account_id" not in attrs:
            raise serializers.ValidationError({"account_id": "Select an account."})
        if attrs["movement"] == "record_only" and ("account_id" in attrs or "allocations" in attrs):
            raise serializers.ValidationError(
                "Record-only events cannot specify account allocations."
            )
        return attrs


class DebtCreateSerializer(MovementSerializer):
    person_id = serializers.IntegerField(min_value=1)
    direction = serializers.ChoiceField(choices=Direction.values)
    currency = serializers.ChoiceField(choices=Currency.values)
    amount = MoneyField(min_value=Decimal("0.01"))
    due_date = serializers.DateField(allow_null=True, required=False)


class RepaymentSerializer(MovementSerializer):
    settled_amount = MoneyField(min_value=Decimal("0.01"))
    payment_amount = MoneyField(min_value=Decimal("0.01"))
    payment_currency = serializers.ChoiceField(choices=Currency.values)


class WriteOffSerializer(DatedSerializer):
    settled_amount = MoneyField(min_value=Decimal("0.01"))
    reason = serializers.CharField(max_length=1000)


EVENT_SERIALIZERS = {
    "debt_create": DebtCreateSerializer,
    "debt_repayment": RepaymentSerializer,
    "debt_writeoff": WriteOffSerializer,
}


class EventReplacementSerializer(StrictSerializer):
    kind = serializers.ChoiceField(choices=list(EVENT_SERIALIZERS))
    data = serializers.DictField()

    def validate(self, attrs):
        serializer = EVENT_SERIALIZERS[attrs["kind"]](data=attrs["data"])
        serializer.is_valid(raise_exception=True)
        attrs["data"] = serializer.validated_data
        return attrs


class EventReverseSerializer(DatedSerializer):
    replacement = EventReplacementSerializer(required=False)


class PersonSerializer(serializers.ModelSerializer):
    balances = serializers.SerializerMethodField()

    def get_balances(self, person: Person) -> dict[str, dict[str, str]]:
        from apps.finances.services.core import ZERO, money

        return {
            direction: {
                currency: money(getattr(person, f"{direction}_{currency.lower()}", ZERO))
                for currency in Currency.values
            }
            for direction in Direction.values
        }

    class Meta:
        model = Person
        fields = ["id", "name", "phone", "note", "archived", "balances"]


class DebtFilters(CursorFilters):
    direction = serializers.ChoiceField(choices=Direction.values, required=False)
    currency = serializers.ChoiceField(choices=Currency.values, required=False)
    status = serializers.ChoiceField(
        choices=["active", "settled", "cancelled", "all"], required=False
    )
    person_id = serializers.IntegerField(min_value=1, required=False)


class DebtSerializer(serializers.ModelSerializer):
    class Meta:
        model = Debt
        fields = [
            "id",
            "person_id",
            "direction",
            "currency",
            "original_amount",
            "remaining_amount",
            "note",
            "due_date",
            "cancelled",
        ]


class EventSerializer(serializers.ModelSerializer):
    operation = OperationSerializer()

    class Meta:
        model = DebtEvent
        fields = ["id", "debt_id", "remaining_delta", "operation"]
