import runpy
from pathlib import Path
from unittest.mock import patch

from django.contrib.sessions.models import Session
from django.test import SimpleTestCase, TestCase


class DeploymentEndpointTests(TestCase):
    def test_readiness_checks_database_without_creating_finance_or_session_records(self) -> None:
        response = self.client.get("/api/v1/ready/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ready"})
        self.assertIn("no-store", response["Cache-Control"])
        self.assertFalse(Session.objects.exists())

    def test_readiness_failure_is_generic_and_get_only(self) -> None:
        with patch("config.health.connection.cursor", side_effect=RuntimeError("private fixture")):
            response = self.client.get("/api/v1/ready/")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unavailable"})
        self.assertNotContains(response, "private fixture", status_code=503)
        self.assertEqual(self.client.post("/api/v1/ready/").status_code, 405)

    def test_json_size_limit_is_enforced_before_authentication_processing(self) -> None:
        response = self.client.post(
            "/api/v1/auth/login/",
            data='{"email":"' + "x" * (64 * 1024) + '"}',
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(b"x" * 100, response.content)


class ProductionRunnerTests(SimpleTestCase):
    def configuration(self, variables: dict) -> dict:
        with patch.dict("os.environ", variables, clear=True):
            return runpy.run_path(str(Path(__file__).resolve().parents[2] / "gunicorn.conf.py"))

    def test_default_runner_is_bounded_and_does_not_trust_remote_forwarders(self) -> None:
        config = self.configuration({})
        self.assertEqual(config["bind"], "0.0.0.0:8000")
        self.assertEqual((config["workers"], config["threads"]), (1, 2))
        self.assertEqual(set(config["forwarded_allow_ips"].split(",")), {"127.0.0.1", "::1"})
        self.assertIsNone(config["accesslog"])

    def test_runner_accepts_explicit_port_and_exact_proxy_peers(self) -> None:
        config = self.configuration({"PORT": "8080", "PENNY_TRUSTED_PROXY_IPS": "10.0.0.5"})
        self.assertEqual(config["bind"], "0.0.0.0:8080")
        self.assertIn("10.0.0.5", config["forwarded_allow_ips"])
        for variables in (
            {"PORT": "bad"},
            {"PENNY_WORKERS": "100"},
            {"PENNY_TRUSTED_PROXY_IPS": "*"},
        ):
            with self.assertRaises(ValueError):
                self.configuration(variables)
