import os
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from .settings import required_port


class EnvironmentPortTests(SimpleTestCase):
    def test_uses_explicit_environment_value(self) -> None:
        with patch.dict(os.environ, {"PENNY_EMAIL_PORT": "2525"}):
            self.assertEqual(required_port("PENNY_EMAIL_PORT"), 2525)

    def test_rejects_missing_port_instead_of_falling_back(self) -> None:
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ImproperlyConfigured):
            required_port("PENNY_EMAIL_PORT")

    def test_rejects_invalid_ports_without_echoing_value(self) -> None:
        for value in ["not-a-port", "0", "65536"]:
            with patch.dict(os.environ, {"PENNY_EMAIL_PORT": value}):
                with self.assertRaises(ImproperlyConfigured) as caught:
                    required_port("PENNY_EMAIL_PORT")
                self.assertNotIn(value, str(caught.exception))
