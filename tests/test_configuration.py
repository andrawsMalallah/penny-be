import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

from django.core.exceptions import ImproperlyConfigured
from django.core.management.utils import get_random_secret_key
from django.test import SimpleTestCase

from config.test_runner import ExistingPostgresRunner


class ConfigurationTests(SimpleTestCase):
    def test_local_mfa_key_is_read_after_environment_loading(self) -> None:
        script = """
import importlib
import os
from unittest.mock import patch
from cryptography.fernet import Fernet
key = Fernet.generate_key().decode()
def load():
    os.environ.update({
        'PENNY_SECRET_KEY': 'synthetic-django-key',
        'PENNY_MFA_ENCRYPTION_KEY': key,
        'PENNY_DB_NAME': 'penny', 'PENNY_DB_HOST': 'localhost',
        'PENNY_DB_USER': 'synthetic-user', 'PENNY_DB_PASSWORD': 'synthetic-password',
    })
with patch('config.environment.load_local_environment', side_effect=load):
    local = importlib.import_module('config.settings.local')
assert local.PENNY_MFA_ENCRYPTION_KEY == key
"""
        environment = {
            name: value for name, value in os.environ.items() if not name.startswith("PENNY_")
        }
        result = subprocess.run(
            [sys.executable, "-c", script],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def run_settings(self, module: str, variables: dict[str, str]) -> subprocess.CompletedProcess:
        environment = {
            name: value for name, value in os.environ.items() if not name.startswith("PENNY_")
        }
        environment.update(variables)
        environment.setdefault("PYTHON_DOTENV_DISABLED", "1")
        environment["DJANGO_SETTINGS_MODULE"] = module
        return subprocess.run(
            [sys.executable, "-c", "import django; django.setup()"],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_local_requires_explicit_secrets(self) -> None:
        result = self.run_settings("config.settings.local", {})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Set the PENNY_SECRET_KEY", result.stderr)

    def test_production_does_not_load_local_environment_file(self) -> None:
        result = self.run_settings("config.settings.production", {"PYTHON_DOTENV_DISABLED": "0"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Set the PENNY_SECRET_KEY", result.stderr)

    def test_test_settings_accept_shared_penny_with_existing_role(self) -> None:
        result = self.run_settings(
            "config.settings.test",
            {
                "PENNY_DB_NAME": "penny",
                "PENNY_DB_USER": "postgres",
                "PENNY_DB_PASSWORD": "synthetic-test-input",
                "PENNY_DB_HOST": "localhost",
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_test_settings_reject_other_database_and_remote_host(self) -> None:
        for name, host, expected in [
            ("other_database", "localhost", "shared local penny"),
            ("penny", "remote.example", "local PostgreSQL"),
        ]:
            with self.subTest(name=name, host=host):
                result = self.run_settings(
                    "config.settings.test",
                    {
                        "PENNY_DB_NAME": name,
                        "PENNY_DB_USER": "postgres",
                        "PENNY_DB_PASSWORD": "synthetic-test-input",
                        "PENNY_DB_HOST": host,
                    },
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr)

    def test_production_rejects_wildcard_hosts(self) -> None:
        result = self.run_settings(
            "config.settings.production",
            {
                "PENNY_SECRET_KEY": get_random_secret_key(),
                "PENNY_ALLOWED_HOSTS": "*",
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("explicit allowed hosts", result.stderr)

    def test_runner_refuses_other_database_before_connecting(self) -> None:
        connection = MagicMock()
        connection.settings_dict = {"NAME": "other_database"}
        with patch("config.test_runner.connections", {"default": connection}):
            with self.assertRaises(ImproperlyConfigured):
                ExistingPostgresRunner().setup_databases()
        connection.ensure_connection.assert_not_called()

    def test_production_proxy_trust_is_opt_in_and_tls_cannot_be_disabled(self) -> None:
        variables = {
            "PENNY_SECRET_KEY": get_random_secret_key(),
            "PENNY_ALLOWED_HOSTS": "penny.example.com",
            "PENNY_MFA_ENCRYPTION_KEY": "synthetic-key-not-used-by-this-check",
            "PENNY_DB_NAME": "synthetic",
            "PENNY_DB_USER": "synthetic",
            "PENNY_DB_PASSWORD": "synthetic",
            "PENNY_DB_HOST": "synthetic.invalid",
        }
        script = """
import os
from django.conf import settings
assert settings.SECURE_PROXY_SSL_HEADER == (
    ('HTTP_X_FORWARDED_PROTO', 'https') if os.environ.get('PENNY_TRUST_PROXY_HTTPS') == '1'
    else None
)
assert settings.SECURE_SSL_REDIRECT and settings.SESSION_COOKIE_SECURE
assert settings.CSRF_COOKIE_SECURE and not settings.DEBUG
assert settings.DATABASES['default']['OPTIONS']['channel_binding'] == 'require'
assert not settings.PENNY_API_DOCS_ENABLED
"""
        for trust in ("0", "1"):
            environment = {
                name: value for name, value in os.environ.items() if not name.startswith("PENNY_")
            }
            environment.update(variables)
            environment.update(
                {
                    "DJANGO_SETTINGS_MODULE": "config.settings.production",
                    "PENNY_TRUST_PROXY_HTTPS": trust,
                    "PENNY_DB_CHANNEL_BINDING": "require",
                }
            )
            result = subprocess.run(
                [sys.executable, "-c", script],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_settings(
            "config.settings.production", {**variables, "PENNY_DB_SSLMODE": "disable"}
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires TLS", result.stderr)

    def test_production_rejects_empty_and_nonexact_hosts(self) -> None:
        for hosts in (
            "penny.example.com,",
            ".example.com",
            "https://penny.example.com",
            "*.example.com",
        ):
            with self.subTest(hosts=hosts):
                result = self.run_settings(
                    "config.settings.production",
                    {"PENNY_SECRET_KEY": get_random_secret_key(), "PENNY_ALLOWED_HOSTS": hosts},
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("explicit allowed hosts", result.stderr)
