"""Environment-backed configuration for a single Django API process."""

import os
from pathlib import Path

from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
# Platform environment variables take precedence over the local development file.
if os.environ.get("PENNY_SKIP_DOTENV") != "1":
    load_dotenv(BASE_DIR / ".env", override=False)


def required_setting(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ImproperlyConfigured(f"Configure {name} before starting Penny.")
    return value


def required_port(name: str) -> int:
    try:
        port = int(required_setting(name))
    except ValueError:
        raise ImproperlyConfigured(f"{name} must be a valid TCP port number.") from None
    if not 1 <= port <= 65535:
        raise ImproperlyConfigured(f"{name} must be between 1 and 65535.")
    return port


SECRET_KEY = required_setting("PENNY_SECRET_KEY")
TWO_FACTOR_ENCRYPTION_KEY = required_setting("PENNY_2FA_ENCRYPTION_KEY")
try:
    Fernet(TWO_FACTOR_ENCRYPTION_KEY.encode())
except ValueError, TypeError:
    raise ImproperlyConfigured("PENNY_2FA_ENCRYPTION_KEY must be a valid Fernet key.") from None
DEBUG = False
LOCAL_HTTP = os.environ.get("PENNY_LOCAL_HTTP") == "1"
ALLOWED_HOSTS = os.environ.get("PENNY_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
CSRF_TRUSTED_ORIGINS = [
    origin for origin in os.environ.get("PENNY_TRUSTED_ORIGINS", "").split(",") if origin
]
# Trust the original HTTPS scheme only when explicitly enabled on a recognized ingress.
render_https_proxy = os.environ.get("PENNY_TRUST_RENDER_PROXY") == "1" and os.environ.get(
    "RENDER_EXTERNAL_HOSTNAME"
)
northflank_https_proxy = os.environ.get("PENNY_TRUST_NORTHFLANK_PROXY") == "1" and os.environ.get(
    "NF_HOSTS"
)
if render_https_proxy or northflank_https_proxy:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "rest_framework",
    "app.identity.apps.IdentityConfig",
    "app.finance.apps.FinanceConfig",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "app.identity.services.sessions.SessionActivityMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("PENNY_DB_NAME", "penny_web_dev"),
        "USER": os.environ.get("PENNY_DB_USER", "penny_web_dev"),
        "PASSWORD": os.environ.get("PENNY_DB_PASSWORD", ""),
        "HOST": os.environ.get("PENNY_DB_HOST", "127.0.0.1"),
        "PORT": os.environ.get("PENNY_DB_PORT", "5432"),
        "CONN_MAX_AGE": 0,
        "OPTIONS": {"sslmode": os.environ.get("PENNY_DB_SSLMODE", "prefer")},
    }
}
AUTH_USER_MODEL = "identity.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.SessionAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["app.identity.permissions.IsVerifiedUser"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 10,
    "EXCEPTION_HANDLER": "app.identity.errors.exception_handler",
}
LANGUAGE_CODE = "en"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = not LOCAL_HTTP
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 60 * 60 * 12
CSRF_COOKIE_SECURE = not LOCAL_HTTP
CSRF_COOKIE_HTTPONLY = True
CSRF_FAILURE_VIEW = "app.identity.api.views.csrf_failure"
SECURE_SSL_REDIRECT = not LOCAL_HTTP
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
# Apply HSTS only to HTTPS deployments; local HTTP previews stay usable.
SECURE_HSTS_SECONDS = 0 if LOCAL_HTTP else 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
DATA_UPLOAD_MAX_MEMORY_SIZE = 64 * 1024
# No shared data cache; each app/page load reads the API and authoritative database.
CACHES = {"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}}
SESSION_ENGINE = "django.contrib.sessions.backends.db"
EMAIL_TRANSPORT = os.environ.get("PENNY_EMAIL_TRANSPORT", "smtp")
if EMAIL_TRANSPORT == "brevo_api":
    EMAIL_BACKEND = "config.brevo_email.BrevoEmailBackend"
elif EMAIL_TRANSPORT == "smtp":
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
else:
    raise ImproperlyConfigured("PENNY_EMAIL_TRANSPORT must be smtp or brevo_api.")
EMAIL_HOST = os.environ.get("PENNY_EMAIL_HOST", "smtp-relay.brevo.com")
EMAIL_PORT = required_port("PENNY_EMAIL_PORT") if EMAIL_TRANSPORT == "smtp" else 587
EMAIL_USE_TLS = True
EMAIL_TIMEOUT = 10
EMAIL_HOST_USER = os.environ.get("PENNY_EMAIL_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("PENNY_EMAIL_PASSWORD", "")
PENNY_BREVO_API_KEY = (
    required_setting("PENNY_BREVO_API_KEY") if EMAIL_TRANSPORT == "brevo_api" else ""
)
DEFAULT_FROM_EMAIL = (
    required_setting("PENNY_EMAIL_FROM")
    if EMAIL_TRANSPORT == "brevo_api"
    else os.environ.get("PENNY_EMAIL_FROM", "")
)
