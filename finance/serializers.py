from decimal import Decimal

from rest_framework import serializers

from .models import Account, AccountGroup, ExpenseCategory, OpeningBalance


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


class AccountInputSerializer(GroupInputSerializer):
    group = serializers.IntegerField(min_value=1, allow_null=True)


class AccountCreateSerializer(AccountInputSerializer):
    opening_iqd = MoneyField()
    opening_usd = MoneyField()


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
        fields = ["iqd", "usd", "recorded_at"]
        read_only_fields = fields


class AccountSerializer(serializers.ModelSerializer):
    group_name = serializers.CharField(source="group.name", default=None)
    balance_iqd = serializers.DecimalField(source="opening.iqd", max_digits=18, decimal_places=2)
    balance_usd = serializers.DecimalField(source="opening.usd", max_digits=18, decimal_places=2)
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
            "opening",
        ]
        read_only_fields = fields
