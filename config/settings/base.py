import os

from django.core.exceptions import ImproperlyConfigured


def required_environment(name: str) -> str:
    value = os.environ.get(name, "")
    if not value.strip():
        raise ImproperlyConfigured(f"Set the {name} environment variable.")
    return value


DEBUG = False
ALLOWED_HOSTS: list[str] = []
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "rest_framework",
    "django_otp",
    "apps.accounts.apps.AccountsConfig",
    "apps.finances.apps.FinancesConfig",
    "apps.debts.apps.DebtsConfig",
    "apps.backups.apps.BackupsConfig",
    "drf_spectacular",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.accounts.middleware.SessionActivityMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": ["django.template.context_processors.request"]},
    }
]
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"
AUTH_USER_MODEL = "accounts.User"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"
LANGUAGE_CODE = "en"
USE_I18N = True
LANGUAGES = [("en", "English"), ("ar", "Arabic")]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
    {"NAME": "apps.accounts.validators.PasswordCompositionValidator"},
]
REST_FRAMEWORK = {
    "DEFAULT_SCHEMA_CLASS": "config.schema.ApiSchema",
    "DEFAULT_AUTHENTICATION_CLASSES": ["apps.accounts.authentication.DeviceSessionAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.CursorPagination",
    "PAGE_SIZE": 50,
    "COERCE_DECIMAL_TO_STRING": True,
}
PENNY_API_DOCS_ENABLED = False
SPECTACULAR_SETTINGS = {
    "TITLE": "Penny API",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": r"/api/v1",
    "POSTPROCESSING_HOOKS": [
        "config.schema.request_headers",
    ],
}
DATA_UPLOAD_MAX_MEMORY_SIZE = 9 * 1024 * 1024
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_AGE = 90 * 24 * 60 * 60
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_FAILURE_VIEW = "apps.accounts.api.views.csrf_failure"
# Renew explicitly after successful authorized requests, not after rejected requests.
SESSION_SAVE_EVERY_REQUEST = False
AUTH_LOGIN_ACCOUNT_LIMIT = 5
AUTH_LOGIN_IP_LIMIT = 50
AUTH_LOGIN_WINDOW_SECONDS = 15 * 60
AUTH_REGISTRATION_IP_LIMIT = 10
AUTH_REGISTRATION_WINDOW_SECONDS = 60 * 60
# Loaded lazily by MFA; unit health checks do not require deployment secrets.
PENNY_MFA_ENCRYPTION_KEY = os.environ.get("PENNY_MFA_ENCRYPTION_KEY", "")


def postgres_database() -> dict:
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": required_environment("PENNY_DB_NAME"),
        "USER": required_environment("PENNY_DB_USER"),
        "PASSWORD": required_environment("PENNY_DB_PASSWORD"),
        "HOST": required_environment("PENNY_DB_HOST"),
        "PORT": os.environ.get("PENNY_DB_PORT", "5432"),
        "CONN_MAX_AGE": 0,
        "OPTIONS": {"sslmode": os.environ.get("PENNY_DB_SSLMODE", "require")},
    }
