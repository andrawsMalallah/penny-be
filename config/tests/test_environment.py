import os
import runpy
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from config.settings import required_port, required_setting


class RequiredEnvironmentTests(SimpleTestCase):
    def test_uses_explicit_environment_value(self) -> None:
        with patch.dict(os.environ, {"PENNY_TEST_VALUE": "configured"}):
            self.assertEqual(required_setting("PENNY_TEST_VALUE"), "configured")

    def test_rejects_missing_required_value(self) -> None:
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ImproperlyConfigured):
            required_setting("PENNY_TEST_VALUE")

    def test_rejects_empty_required_value(self) -> None:
        with patch.dict(os.environ, {"PENNY_TEST_VALUE": ""}):
            with self.assertRaises(ImproperlyConfigured):
                required_setting("PENNY_TEST_VALUE")

    def test_smtp_port_is_validated_without_echoing_value(self) -> None:
        with patch.dict(os.environ, {"PENNY_EMAIL_PORT": "2525"}):
            self.assertEqual(required_port("PENNY_EMAIL_PORT"), 2525)
        for value in ("invalid", "0", "65536"):
            with patch.dict(os.environ, {"PENNY_EMAIL_PORT": value}):
                with self.assertRaises(ImproperlyConfigured) as caught:
                    required_port("PENNY_EMAIL_PORT")
                self.assertNotIn(value, str(caught.exception))


class HostedEnvironmentTests(SimpleTestCase):
    def load_settings(
        self,
        hosted: str | None = None,
        *,
        proxy_opt_in: bool = True,
        extra_environment: dict[str, str] | None = None,
    ) -> dict:
        environment = {
            "PENNY_SKIP_DOTENV": "1",
            "PENNY_SECRET_KEY": "test-only-secret",
            "PENNY_2FA_ENCRYPTION_KEY": "dHR0dHR0dHR0dHR0dHR0dHR0dHR0dHR0dHR0dHR0dHQ=",
            "PENNY_EMAIL_TRANSPORT": "brevo_api",
            "PENNY_BREVO_API_KEY": "test-only-api-key",
            "PENNY_EMAIL_FROM": "sender@example.com",
        }
        if hosted == "render":
            environment.update(
                {
                    "RENDER_EXTERNAL_HOSTNAME": "example.onrender.com",
                }
            )
            if proxy_opt_in:
                environment["PENNY_TRUST_RENDER_PROXY"] = "1"
        elif hosted == "northflank":
            environment["NF_HOSTS"] = "app--example.code.run"
            if proxy_opt_in:
                environment["PENNY_TRUST_NORTHFLANK_PROXY"] = "1"
        environment.update(extra_environment or {})
        with patch.dict(os.environ, environment, clear=True):
            return runpy.run_path(str(Path(__file__).resolve().parents[1] / "settings.py"))

    def test_proxy_trust_requires_hosted_identity_and_explicit_opt_in(self) -> None:
        local = self.load_settings()
        render = self.load_settings("render")
        northflank = self.load_settings("northflank")
        self.assertNotIn("SECURE_PROXY_SSL_HEADER", local)
        for hosted in (render, northflank):
            self.assertEqual(hosted["SECURE_PROXY_SSL_HEADER"], ("HTTP_X_FORWARDED_PROTO", "https"))
            self.assertEqual(hosted["EMAIL_BACKEND"], "config.brevo_email.BrevoEmailBackend")
        self.assertNotIn(
            "SECURE_PROXY_SSL_HEADER", self.load_settings("render", proxy_opt_in=False)
        )
        self.assertNotIn(
            "SECURE_PROXY_SSL_HEADER", self.load_settings("northflank", proxy_opt_in=False)
        )

    def test_northflank_proxy_opt_in_requires_northflank_runtime(self) -> None:
        settings = self.load_settings(extra_environment={"PENNY_TRUST_NORTHFLANK_PROXY": "1"})
        self.assertNotIn("SECURE_PROXY_SSL_HEADER", settings)
