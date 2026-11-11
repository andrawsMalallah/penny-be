import os
import signal
import subprocess
from contextlib import nullcontext
from io import StringIO
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError
from django.test import SimpleTestCase, override_settings

from scripts import serve


class ReadinessTests(SimpleTestCase):
    def test_readiness_checks_database_without_reading_user_data(self) -> None:
        with (
            patch("config.urls.transaction.atomic", return_value=nullcontext()),
            patch("config.urls.connection") as connection,
        ):
            response = self.client.get("/api/v1/ready/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(
            connection.cursor.return_value.__enter__.return_value.execute.call_count, 2
        )

    def test_unavailable_database_returns_safe_uncached_503(self) -> None:
        with (
            patch(
                "config.urls.transaction.atomic", side_effect=OperationalError("private details")
            ),
        ):
            response = self.client.get("/api/v1/ready/")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unavailable"})
        self.assertNotContains(response, "private", status_code=503)
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_readiness_rejects_mutations_before_accessing_database(self) -> None:
        self.assertEqual(self.client.post("/api/v1/ready/").status_code, 405)

    @override_settings(
        SECURE_SSL_REDIRECT=True,
        SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
        SESSION_COOKIE_SECURE=True,
        CSRF_COOKIE_SECURE=True,
    )
    def test_trusted_https_avoids_redirect_loop_and_keeps_csrf_cookie_secure(self) -> None:
        response = self.client.get("/api/v1/auth/csrf/", HTTP_X_FORWARDED_PROTO="https")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.cookies["csrftoken"]["secure"])
        self.assertEqual(self.client.get("/api/v1/health/").status_code, 301)
        self.assertEqual(
            self.client.get("/api/v1/health/", HTTP_X_FORWARDED_PROTO="http").status_code, 301
        )

    @override_settings(SECURE_SSL_REDIRECT=True, SECURE_PROXY_SSL_HEADER=None)
    def test_forwarded_header_does_not_enable_https_without_explicit_trust(self) -> None:
        response = self.client.get("/api/v1/health/", HTTP_X_FORWARDED_PROTO="https")
        self.assertEqual(response.status_code, 301)


class FinancialWorkerTests(SimpleTestCase):
    def test_one_shot_remains_bounded(self) -> None:
        output = StringIO()
        with (
            patch(
                "apps.backups.management.commands.process_financial_jobs.process_one",
                return_value=True,
            ) as process,
            patch("apps.backups.management.commands.process_financial_jobs.close_old_connections"),
        ):
            call_command("process_financial_jobs", limit=2, stdout=output)
        self.assertEqual(process.call_count, 2)
        self.assertIn("Processed 2 financial jobs.", output.getvalue())

    def test_watch_handles_idle_then_job_then_termination_without_next_job(self) -> None:
        from threading import Event

        stopped = Event()
        handlers = {}

        def register(signum, handler):
            previous = handlers.get(signum)
            handlers[signum] = handler
            return previous

        def idle_wait(interval):
            return stopped.is_set()

        attempts = []

        def process_job():
            attempts.append(True)
            if len(attempts) == 1:
                return False
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            return True

        with (
            patch(
                "apps.backups.management.commands.process_financial_jobs.Event",
                return_value=stopped,
            ),
            patch.object(stopped, "wait", side_effect=idle_wait),
            patch(
                "apps.backups.management.commands.process_financial_jobs.signal.signal",
                side_effect=register,
            ),
            patch(
                "apps.backups.management.commands.process_financial_jobs.process_one",
                side_effect=process_job,
            ) as process,
            patch("apps.backups.management.commands.process_financial_jobs.close_old_connections"),
            patch(
                "apps.backups.management.commands.process_financial_jobs.connections.close_all"
            ) as close,
        ):
            call_command("process_financial_jobs", watch=True, stdout=StringIO())
        self.assertEqual(process.call_count, 2)
        self.assertTrue(stopped.is_set())
        self.assertGreaterEqual(close.call_count, 2)
        self.assertIsNone(handlers[signal.SIGTERM])

    def test_database_failure_exits_without_disclosing_connection_details(self) -> None:
        with (
            patch(
                "apps.backups.management.commands.process_financial_jobs.process_one",
                side_effect=OperationalError("private connection"),
            ),
            patch("apps.backups.management.commands.process_financial_jobs.close_old_connections"),
            self.assertRaisesMessage(
                CommandError, "Financial worker lost database access."
            ) as raised,
        ):
            call_command("process_financial_jobs", stdout=StringIO())
        self.assertNotIn("private", str(raised.exception))

    def test_invalid_limits_are_rejected_before_processing(self) -> None:
        for options in ({"limit": 0}, {"limit": 101}, {"poll_interval": 0}, {"poll_interval": 61}):
            with self.subTest(options=options), self.assertRaises(CommandError):
                call_command("process_financial_jobs", **options, stdout=StringIO())


class ServiceSupervisorTests(SimpleTestCase):
    def test_failed_startup_check_does_not_launch_web_or_jobs(self) -> None:
        child = MagicMock(pid=12345, returncode=1)
        child.poll.return_value = 1
        with (
            patch("scripts.serve.sys.platform", "linux"),
            patch.dict(os.environ, {"PORT": "10000"}),
            patch("scripts.serve.subprocess.Popen", return_value=child) as launch,
            patch("scripts.serve.stop_children") as stop,
        ):
            self.assertEqual(serve.main(), 1)
        self.assertEqual(launch.call_count, 1)
        self.assertIn("check", launch.call_args.args[0])
        stop.assert_called_once_with([child])

    def test_child_exit_stops_both_services_and_returns_failure(self) -> None:
        checks = [MagicMock(returncode=0), MagicMock(returncode=0)]
        for check in checks:
            check.poll.return_value = 0
        web, worker = MagicMock(), MagicMock()
        web.poll.return_value = None
        worker.poll.return_value = 1
        with (
            patch("scripts.serve.sys.platform", "linux"),
            patch.dict(os.environ, {"PORT": "10000"}),
            patch("scripts.serve.subprocess.Popen", side_effect=[*checks, web, worker]) as launch,
            patch("scripts.serve.stop_children") as stop,
        ):
            self.assertEqual(serve.main(), 1)
        stop.assert_called_once_with([web, worker])
        migration_check = launch.call_args_list[1].args[0]
        self.assertEqual(migration_check[-2:], ["migrate", "--check"])
        self.assertIn("--watch", launch.call_args_list[3].args[0])

    def test_partial_launch_failure_stops_already_running_web_process(self) -> None:
        checks = [MagicMock(returncode=0), MagicMock(returncode=0)]
        for check in checks:
            check.poll.return_value = 0
        web = MagicMock()
        with (
            patch("scripts.serve.sys.platform", "linux"),
            patch.dict(os.environ, {"PORT": "10000"}),
            patch("scripts.serve.subprocess.Popen", side_effect=[*checks, web, OSError()]),
            patch("scripts.serve.stop_children") as stop,
        ):
            self.assertEqual(serve.main(), 1)
        stop.assert_called_once_with([web])

    def test_shutdown_kills_stuck_process_group_after_grace_period(self) -> None:
        child = MagicMock(pid=12345)
        child.wait.side_effect = [subprocess.TimeoutExpired("worker", 0), 0]
        with patch("scripts.serve.os.killpg", create=True) as kill:
            # Windows lacks SIGKILL, so supply the POSIX value for this isolated check.
            with patch("scripts.serve.signal.SIGKILL", 9, create=True):
                serve.stop_children([child], grace_seconds=0)
        self.assertEqual(kill.call_args_list[0].args, (12345, signal.SIGTERM))
        self.assertEqual(kill.call_args_list[1].args, (12345, 9))
