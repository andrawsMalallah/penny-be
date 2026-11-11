import os
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from django.test import SimpleTestCase

from scripts import container_health


class ContainerHealthTests(SimpleTestCase):
    def probe(self, mode="ready", status=200, body=b'{"status":"ok"}'):
        response = MagicMock(status=status)
        response.read.return_value = body
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with (
            patch.dict(os.environ, {"PENNY_ALLOWED_HOSTS": "api.example.com", "PORT": "10000"}),
            patch("sys.argv", ["probe", mode]),
            patch("scripts.container_health.build_opener", return_value=opener),
        ):
            result = container_health.main()
        return result, opener

    def test_probe_checks_exact_response_over_loopback(self):
        result, opener = self.probe()
        self.assertEqual(result, 0)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:10000/api/v1/ready/")
        self.assertEqual(request.get_header("Host"), "api.example.com")

    def test_unhealthy_or_unexpected_responses_fail(self):
        for status, body in ((503, b'{"status":"unavailable"}'), (200, b"invalid")):
            with self.subTest(status=status):
                self.assertEqual(self.probe(status=status, body=body)[0], 1)

    def test_redirects_are_not_followed(self):
        self.assertIsNone(
            container_health.NoRedirects().redirect_request(
                None, None, 301, "", {}, "https://example.com"
            )
        )
        with (
            patch.dict(os.environ, {"PENNY_ALLOWED_HOSTS": "api.example.com"}),
            patch("sys.argv", ["probe", "health"]),
            patch("scripts.container_health.build_opener") as build,
        ):
            build.return_value.open.side_effect = HTTPError("", 301, "", {}, None)
            self.assertEqual(container_health.main(), 1)

    def test_invalid_mode_never_connects(self):
        result, opener = self.probe(mode="unknown")
        self.assertEqual(result, 1)
        opener.open.assert_not_called()
