from .unit import *

# User-approved shared local tables: test cleanup may remove development records.
DATABASES = {"default": postgres_database()}
if DATABASES["default"]["NAME"] != "penny":
    raise ImproperlyConfigured("Tests require the shared local penny database.")
if DATABASES["default"]["HOST"] not in {"localhost", "127.0.0.1", "::1"}:
    raise ImproperlyConfigured("Tests require a local PostgreSQL server.")
if str(DATABASES["default"]["PORT"]) != "5432":
    raise ImproperlyConfigured("Tests require local PostgreSQL port 5432.")
DATABASES["default"]["TEST"] = {"NAME": "penny"}
TEST_RUNNER = "config.test_runner.ExistingPostgresRunner"
