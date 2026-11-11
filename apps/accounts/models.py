from __future__ import annotations

from django.contrib.auth.models import AbstractUser, UserManager
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower
from django.views.decorators.debug import sensitive_variables
from django_otp.models import Device

from .validators import display_name_validator, username_validator


class PennyUserManager(UserManager):
    def for_username(self, username: str) -> models.QuerySet[User]:
        # Match the existing LOWER(username) unique index rather than an unindexed UPPER lookup.
        return self.alias(normalized_username=Lower("username")).filter(
            normalized_username=username.lower()
        )

    def get_by_natural_key(self, username: str) -> User:
        return self.for_username(username).get()

    async def aget_by_natural_key(self, username: str) -> User:
        return await self.for_username(username).aget()


class User(AbstractUser):
    username = models.CharField(max_length=30, unique=True, validators=[username_validator])
    display_name = models.CharField(max_length=40, blank=True, validators=[display_name_validator])
    first_name = None
    last_name = None
    objects = PennyUserManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(Lower("username"), name="accounts_username_case_insensitive"),
            models.CheckConstraint(
                condition=models.Q(username__regex=r"^[A-Za-z0-9_]{3,30}$"),
                name="accounts_username_format",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        self.display_name = self.display_name.strip()
        display_name_validator(self.display_name)

    def save(self, *args, **kwargs) -> None:
        username_validator(self.username)
        self.display_name = self.display_name.strip()
        display_name_validator(self.display_name)
        if len(self.display_name) > 40:
            raise ValidationError({"display_name": "Use at most 40 characters."})
        if self.pk is not None:
            original_username = (
                type(self).objects.filter(pk=self.pk).values_list("username", flat=True).first()
            )
            if original_username is not None and original_username != self.username:
                raise ValidationError({"username": "Usernames cannot be changed."})
        super().save(*args, **kwargs)

    def get_full_name(self) -> str:
        return self.display_name or self.username

    def get_short_name(self) -> str:
        return self.get_full_name()


class DeviceSession(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="device_sessions")
    # This is Django's existing session identifier, never a new token format or API output.
    session_key = models.CharField(max_length=40, unique=True)
    device_name = models.CharField(max_length=80)
    created_at = models.DateTimeField(auto_now_add=True)
    last_active_at = models.DateTimeField()
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        indexes = [models.Index(fields=["user", "-created_at", "-id"])]


class AuthAttemptBucket(models.Model):
    # Django's keyed hash avoids retaining raw usernames or client IP addresses here.
    key = models.CharField(max_length=64, unique=True)
    window_started_at = models.DateTimeField(db_index=True)
    attempts = models.PositiveIntegerField(default=0)


class AuthenticatorDevice(Device):
    encrypted_secret = models.TextField()
    last_step = models.BigIntegerField(default=-1)
    enrollment_session = models.ForeignKey(DeviceSession, null=True, on_delete=models.SET_NULL)
    enrollment_expires_at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user"], name="accounts_one_authenticator")]

    @sensitive_variables()
    def verify_token(self, token: str) -> bool:
        # The service holds the user lock across verification and all related state changes.
        from cryptography.fernet import Fernet
        from django.conf import settings
        from django.utils import timezone
        from django_otp.oath import TOTP

        if len(token) != 6 or not token.isascii() or not token.isdecimal():
            return False
        secret = Fernet(settings.PENNY_MFA_ENCRYPTION_KEY).decrypt(self.encrypted_secret.encode())
        verifier = TOTP(bytes.fromhex(secret.decode()))
        verifier.time = timezone.now().timestamp()
        if not verifier.verify(int(token), tolerance=1, min_t=self.last_step + 1):
            return False
        self.last_step = verifier.t()
        self.save(update_fields=["last_step"])
        return True


class RecoveryCode(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="recovery_codes")
    code_hash = models.CharField(max_length=256)


class MfaChallenge(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    session_key = models.CharField(max_length=40, unique=True)
    auth_hash = models.CharField(max_length=128)
    device_name = models.CharField(max_length=80)
    expires_at = models.DateTimeField(db_index=True)
