from rest_framework import serializers

from apps.accounts.models import DeviceSession, User
from apps.accounts.validators import display_name_validator, username_validator


class StrictSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if isinstance(data, dict):
            unknown = set(data) - set(self.fields)
            if unknown:
                raise serializers.ValidationError({field: ["Unknown field."] for field in unknown})
        return super().to_internal_value(data)


class MfaCodeSerializer(StrictSerializer):
    code = serializers.CharField(max_length=64, trim_whitespace=False, write_only=True)


class MfaSetupSerializer(StrictSerializer):
    password = serializers.CharField(max_length=1024, trim_whitespace=False, write_only=True)


class MfaManagementSerializer(MfaCodeSerializer, MfaSetupSerializer):
    pass


class LoginSerializer(StrictSerializer):
    username = serializers.CharField(
        max_length=30, trim_whitespace=False, validators=[username_validator]
    )
    password = serializers.CharField(max_length=1024, trim_whitespace=False, write_only=True)
    device_name = serializers.CharField(max_length=80, default="Android device")


class RegistrationSerializer(LoginSerializer):
    display_name = serializers.CharField(
        max_length=40, allow_blank=True, default="", validators=[display_name_validator]
    )


class PasswordChangeSerializer(StrictSerializer):
    current_password = serializers.CharField(
        max_length=1024, trim_whitespace=False, write_only=True
    )
    new_password = serializers.CharField(max_length=1024, trim_whitespace=False, write_only=True)


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "username", "display_name"]
        read_only_fields = fields


class DeviceSessionSerializer(serializers.ModelSerializer):
    is_current = serializers.SerializerMethodField()

    def get_is_current(self, device: DeviceSession) -> bool:
        return device.session_key == self.context["request"].session.session_key

    class Meta:
        model = DeviceSession
        fields = ["id", "device_name", "created_at", "last_active_at", "is_current"]
        read_only_fields = fields
