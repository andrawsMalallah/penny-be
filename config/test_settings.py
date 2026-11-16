"""Isolated tests only: never load local credentials or connect to real services."""

import os
from base64 import urlsafe_b64encode

os.environ.setdefault("PENNY_SECRET_KEY", "isolated-test-settings-only-not-for-deployment")
os.environ["PENNY_SKIP_DOTENV"] = "1"
os.environ.setdefault("PENNY_EMAIL_PORT", "1025")  # Fixture only; tests send no SMTP traffic.
os.environ["PENNY_2FA_ENCRYPTION_KEY"] = urlsafe_b64encode(b"t" * 32).decode()  # Test fixture only.

from .settings import *  # noqa: E402

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
