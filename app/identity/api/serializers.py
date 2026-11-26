import re
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from app.identity.models import SignedInSession, User


class RegistrationSerializer(serializers.Serializer):
    display_name = serializers.CharField(max_length=100)
    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(write_only=True, max_length=256, trim_whitespace=False)
    language = serializers.ChoiceField(choices=["en", "ar"])
    timezone = serializers.CharField(max_length=100)

    def validate_email(self, value: str) -> str:
        return value.strip().lower()

    def validate_timezone(self, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise serializers.ValidationError("Choose a valid device timezone.") from error
        return value

    def validate(self, attributes: dict) -> dict:
        candidate = User(email=attributes["email"], display_name=attributes["display_name"])
        try:
            validate_password(attributes["password"], candidate)
        except DjangoValidationError as error:
            raise serializers.ValidationError({"password": error.messages}) from error
        return attributes


class CodeSerializer(serializers.Serializer):
    code = serializers.RegexField(r"\A[0-9]{6}\Z", trim_whitespace=False, write_only=True)


class AccountDeletionConfirmSerializer(CodeSerializer):
    confirmation = serializers.BooleanField()

    def validate_confirmation(self, value: bool) -> bool:
        if not value:
            raise serializers.ValidationError("Confirm permanent account deletion.")
        return value


class UserSettingsSerializer(serializers.Serializer):
    default_account = serializers.IntegerField(min_value=1, allow_null=True, required=False)
    language = serializers.ChoiceField(choices=["en", "ar"])
    default_currency = serializers.ChoiceField(choices=["", "IQD", "USD"], allow_blank=True)
    exchange_rate = serializers.DecimalField(
        max_digits=18,
        decimal_places=6,
        min_value=Decimal("0.000001"),
        allow_null=True,
        coerce_to_string=True,
    )

    def to_internal_value(self, data: object) -> dict:
        if isinstance(data, dict):
            unknown = set(data) - set(self.fields)
            if unknown:
                raise serializers.ValidationError({"detail": "Unsupported settings field."})
            # Rates cross the JSON boundary as decimal strings, never floating-point values.
            rate = data.get("exchange_rate")
            if rate is not None and not isinstance(rate, str):
                raise serializers.ValidationError({"exchange_rate": "Use a decimal string."})
        return super().to_internal_value(data)


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(write_only=True, max_length=256, trim_whitespace=False)

    def validate_email(self, value: str) -> str:
        return value.strip().lower()


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)

    def validate_email(self, value: str) -> str:
        return value.strip().lower()


class PasswordResetCompleteSerializer(serializers.Serializer):
    password = serializers.CharField(write_only=True, max_length=256, trim_whitespace=False)


class CurrentUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "email", "display_name", "email_verified", "language", "timezone"]
        read_only_fields = fields


class SignedInSessionSerializer(serializers.ModelSerializer):
    is_current = serializers.SerializerMethodField()

    class Meta:
        model = SignedInSession
        fields = ["browser", "device", "signed_in_at", "last_active_at", "is_current"]
        read_only_fields = fields

    def get_is_current(self, instance: SignedInSession) -> bool:
        return instance.session_id == self.context["request"].session.session_key


class PasswordConfirmationSerializer(serializers.Serializer):
    password = serializers.CharField(write_only=True, max_length=256, trim_whitespace=False)


class FactorProofSerializer(serializers.Serializer):
    otp = serializers.CharField(write_only=True, max_length=32, trim_whitespace=True)
    recovery = serializers.BooleanField(default=False)

    def validate(self, attributes: dict) -> dict:
        code = attributes["otp"]
        if attributes["recovery"]:
            code = code.lower().replace("-", "").replace(" ", "")
        pattern = r"[a-f0-9]{16}" if attributes["recovery"] else r"[0-9]{6}"
        if re.fullmatch(pattern, code) is None:
            raise serializers.ValidationError({"otp": ["Enter a valid security code."]})
        attributes["otp"] = code
        return attributes


class FactorManagementSerializer(FactorProofSerializer):
    password = serializers.CharField(write_only=True, max_length=256, trim_whitespace=False)
