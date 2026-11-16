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
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["display_name", "language"]
    objects = UserManager()

    class Meta:
        constraints = [models.UniqueConstraint(Lower("email"), name="unique_user_email_lower")]


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
