from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from .models import SignedInSession, User


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
