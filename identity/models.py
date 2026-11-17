import uuid

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.db.models.functions import Lower


class UserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, email: str, password: str | None = None, **extra_fields: object) -> User:
        if not email:
            raise ValueError("An email address is required.")
        user = self.model(email=email.strip().lower(), **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email: str, password: str, **extra_fields: object) -> User:
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if not extra_fields["is_staff"] or not extra_fields["is_superuser"]:
            raise ValueError("Superusers must have staff and superuser permissions.")
        return self.create_user(email, password, **extra_fields)


class User(AbstractUser):
    username = None
    email = models.EmailField(unique=True)
    display_name = models.CharField(max_length=100)
    email_verified = models.BooleanField(default=False)
    language = models.CharField(max_length=2, choices=[("en", "English"), ("ar", "Arabic")])
    timezone = models.CharField(max_length=100, default="UTC")
    session_generation = models.PositiveBigIntegerField(default=0)
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["display_name", "language"]
    objects = UserManager()

    class Meta:
        constraints = [models.UniqueConstraint(Lower("email"), name="unique_user_email_lower")]


class UserSettings(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    default_account = models.ForeignKey(
        "finance.Account", null=True, blank=True, on_delete=models.PROTECT
    )
    default_currency = models.CharField(
        max_length=3, choices=[("IQD", "IQD"), ("USD", "USD")], blank=True
    )
    exchange_rate = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(exchange_rate__isnull=True) | models.Q(exchange_rate__gt=0),
                name="positive_settings_exchange_rate",
            ),
            models.CheckConstraint(
                condition=models.Q(default_currency__in=["", "IQD", "USD"]),
                name="supported_settings_currency",
            ),
        ]


class Registration(models.Model):
    """A draft, not a user: an email cannot be claimed before verification."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(db_index=True)
    display_name = models.CharField(max_length=100)
    password_hash = models.CharField(max_length=128)
    language = models.CharField(max_length=2)
    timezone = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)


class EmailVerification(models.Model):
    """One current challenge per recipient, shared safely by all API processes."""

    email = models.EmailField(unique=True)
    registration = models.ForeignKey(Registration, null=True, on_delete=models.SET_NULL)
    generation = models.UUIDField(default=uuid.uuid4)
    code_hash = models.CharField(max_length=128, blank=True)
    expires_at = models.DateTimeField(null=True, db_index=True)
    failed_attempts = models.PositiveSmallIntegerField(default=0)


class CodeRequestLimit(models.Model):
    """Small rolling-window state; persisted security state, not a data cache."""

    identity_hash = models.CharField(max_length=64, unique=True)
    request_times = models.JSONField(default=list)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)


class PasswordReset(models.Model):
    """Separate from registration; one reset generation can be consumed only once."""

    user = models.OneToOneField(User, on_delete=models.CASCADE)
    generation = models.UUIDField(default=uuid.uuid4, unique=True)
    code_hash = models.CharField(max_length=128, blank=True)
    expires_at = models.DateTimeField(null=True, db_index=True)
    failed_attempts = models.PositiveSmallIntegerField(default=0)
    verified = models.BooleanField(default=False)


class SignedInSession(models.Model):
    """Indexed metadata only; Django owns session keys, signing and expiration."""

    session = models.OneToOneField("sessions.Session", primary_key=True, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    generation = models.PositiveBigIntegerField()
    browser = models.CharField(max_length=30)
    device = models.CharField(max_length=30)
    signed_in_at = models.DateTimeField(null=True)
    last_active_at = models.DateTimeField()

    class Meta:
        indexes = [
            models.Index(
                fields=["user", "-last_active_at", "session"], name="user_session_activity"
            )
        ]


class TwoFactorSettings(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    enabled = models.BooleanField(default=False)
    secret_ciphertext = models.TextField(blank=True)
    last_timecode = models.BigIntegerField(default=-1)
    pending_ciphertext = models.TextField(blank=True)
    pending_expires_at = models.DateTimeField(null=True, db_index=True)
    pending_session_digest = models.CharField(max_length=64, blank=True)
    recovery_hashes = models.JSONField(default=list)


class TwoFactorLogin(models.Model):
    """A password-proven challenge, not an authenticated session."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    password_auth_hash = models.CharField(max_length=64)
    session_generation = models.PositiveBigIntegerField()
    expires_at = models.DateTimeField(db_index=True)
    failed_attempts = models.PositiveSmallIntegerField(default=0)
    consumed = models.BooleanField(default=False)
