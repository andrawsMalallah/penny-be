from .base import *

SECRET_KEY = required_environment("PENNY_SECRET_KEY")
ALLOWED_HOSTS = [host.strip() for host in required_environment("PENNY_ALLOWED_HOSTS").split(",")]
if any(
    not host or "*" in host or "://" in host or "/" in host or host.startswith(".")
    for host in ALLOWED_HOSTS
):
    raise ImproperlyConfigured("Production requires explicit allowed hosts.")
DATABASES = {"default": postgres_database()}
if DATABASES["default"]["OPTIONS"]["sslmode"] not in {"require", "verify-ca", "verify-full"}:
    raise ImproperlyConfigured("Production PostgreSQL requires TLS.")
PENNY_MFA_ENCRYPTION_KEY = required_environment("PENNY_MFA_ENCRYPTION_KEY")
# Enable only behind an ingress that controls this header and blocks direct public access.
if os.environ.get("PENNY_TRUST_PROXY_HTTPS", "0") == "1":
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
DATABASES["default"]["OPTIONS"]["connect_timeout"] = 5
DATABASES["default"]["OPTIONS"]["channel_binding"] = os.environ.get(
    "PENNY_DB_CHANNEL_BINDING", "prefer"
)
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31536000
