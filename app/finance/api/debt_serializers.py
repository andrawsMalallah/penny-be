from decimal import Decimal

from rest_framework import serializers

from app.finance.api.serializers import MoneyField, StrictSerializer, valid_timezone
from app.finance.models import Currency, Debt, DebtDirection, DebtOrigin, DebtPerson


class DebtInputSerializer(StrictSerializer):
    person = serializers.IntegerField(min_value=1, allow_null=True, default=None)
    person_name = serializers.CharField(max_length=100, allow_blank=True, default="")
    direction = serializers.ChoiceField(choices=DebtDirection.choices)
    origin = serializers.ChoiceField(choices=DebtOrigin.choices)
    account = serializers.IntegerField(min_value=1, allow_null=True, default=None)
    currency = serializers.ChoiceField(choices=Currency.choices)
    amount = MoneyField()
    date = serializers.DateField(input_formats=["%Y-%m-%d"])
    note = serializers.CharField(max_length=500, allow_blank=True, default="")
    timezone = serializers.CharField(max_length=100)

    def validate_timezone(self, value: str) -> str:
        return valid_timezone(value)

    def validate(self, attrs: dict) -> dict:
        if (attrs["person"] is None) == (not attrs["person_name"]):
            raise serializers.ValidationError({"person": ["Choose a person or enter a new name."]})
        if attrs["amount"] <= 0:
            raise serializers.ValidationError({"amount": ["Enter a positive amount."]})
        if (attrs["origin"] == DebtOrigin.MOVEMENT) != (attrs["account"] is not None):
            raise serializers.ValidationError(
                {"account": ["Only new lending/borrowing needs an account."]}
            )
        return attrs


class DebtCreateSerializer(DebtInputSerializer):
    request_id = serializers.UUIDField()


class DebtUpdateSerializer(DebtInputSerializer):
    version = serializers.IntegerField(min_value=1)


class RepaymentInputSerializer(StrictSerializer):
    account = serializers.IntegerField(min_value=1)
    amount = MoneyField()
    date = serializers.DateField(input_formats=["%Y-%m-%d"])
    note = serializers.CharField(max_length=500, allow_blank=True, default="")
    timezone = serializers.CharField(max_length=100)

    def validate_timezone(self, value: str) -> str:
        return valid_timezone(value)

    def validate_amount(self, value: Decimal) -> Decimal:
        if value <= 0:
            raise serializers.ValidationError("Enter a positive amount.")
        return value


class RepaymentCreateSerializer(RepaymentInputSerializer):
    request_id = serializers.UUIDField()


class RepaymentUpdateSerializer(RepaymentInputSerializer):
    version = serializers.IntegerField(min_value=1)


class DebtFilterSerializer(StrictSerializer):
    page = serializers.IntegerField(min_value=1, required=False)
    person = serializers.IntegerField(min_value=1, required=False)
    direction = serializers.ChoiceField(choices=DebtDirection.choices, required=False)
    currency = serializers.ChoiceField(choices=Currency.choices, required=False)
    status = serializers.ChoiceField(choices=["open", "settled", "all"], default="open")


class DebtPersonFilterSerializer(StrictSerializer):
    page = serializers.IntegerField(min_value=1, required=False)
    archived = serializers.ChoiceField(choices=["false", "all"], required=False)


class DebtPersonSerializer(serializers.ModelSerializer):
    archived = serializers.BooleanField(default=False, read_only=True)

    class Meta:
        model = DebtPerson
        fields = ["id", "name", "archived"]
        read_only_fields = fields


class DebtSerializer(serializers.ModelSerializer):
    person_name = serializers.CharField(source="person.name")
    account_name = serializers.CharField(source="account.name", default="")
    account_archived = serializers.BooleanField(source="account.archived", default=False)
    opening_date = serializers.DateField(source="account.opening.effective_date", default=None)
    repaid = serializers.DecimalField(max_digits=18, decimal_places=2)
    remaining = serializers.DecimalField(max_digits=18, decimal_places=2)
    repayment_count = serializers.IntegerField()

    class Meta:
        model = Debt
        fields = [
            "id",
            "person",
            "person_name",
            "direction",
            "origin",
            "account",
            "account_name",
            "account_archived",
            "opening_date",
            "currency",
            "amount",
            "date",
            "note",
            "version",
            "repaid",
            "remaining",
            "repayment_count",
        ]
        read_only_fields = fields
