import os
from unittest.mock import patch

from django.test import SimpleTestCase

from scripts.development import main


class SharedDevelopmentCommandTests(SimpleTestCase):
    def test_stopping_server_exits_without_an_interrupt_traceback(self) -> None:
        with (
            patch("sys.argv", ["development", "runserver", "--port", "8001"]),
            patch("scripts.development.load_local_environment"),
            patch.dict(
                os.environ,
                {
                    "PENNY_DB_PASSWORD": "synthetic-test-input",
                    "PENNY_SECRET_KEY": "synthetic-test-key",
                },
            ),
            patch("scripts.development.subprocess.run", side_effect=KeyboardInterrupt) as run,
        ):
            self.assertEqual(main(), 130)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][-3:], ["runserver", "127.0.0.1:8001", "--noreload"])

    def test_server_failure_exit_code_is_preserved(self) -> None:
        with (
            patch("sys.argv", ["development", "runserver"]),
            patch("scripts.development.load_local_environment"),
            patch.dict(
                os.environ,
                {
                    "PENNY_DB_PASSWORD": "synthetic-test-input",
                    "PENNY_SECRET_KEY": "synthetic-test-key",
                },
            ),
            patch("scripts.development.subprocess.run") as run,
        ):
            run.return_value.returncode = 1
            self.assertEqual(main(), 1)

    def test_configure_saves_password_without_running_database_commands(self) -> None:
        with (
            patch("sys.argv", ["development", "configure"]),
            patch("scripts.development.load_local_environment"),
            patch("scripts.development.getpass.getpass", return_value="synthetic-test-input"),
            patch("scripts.development.set_key") as save,
            patch("scripts.development.subprocess.run") as run,
            patch("builtins.print"),
        ):
            self.assertEqual(main(), 0)
        self.assertEqual(save.call_args.args[1:], ("PENNY_DB_PASSWORD", "synthetic-test-input"))
        self.assertEqual(save.call_args.kwargs["quote_mode"], "always")
        run.assert_not_called()

    def test_test_command_uses_penny_and_existing_credentials(self) -> None:
        with (
            patch("sys.argv", ["development", "test"]),
            patch("scripts.development.getpass.getpass", return_value="synthetic-test-input"),
            patch("scripts.development.subprocess.run") as run,
            patch("scripts.development.load_local_environment"),
            patch.dict(
                os.environ,
                {
                    "PENNY_DB_USER": "postgres",
                    "PENNY_SECRET_KEY": "synthetic-test-input",
                    "PENNY_DB_PASSWORD": "",
                },
            ),
            patch("builtins.print"),
        ):
            run.return_value.returncode = 0
            self.assertEqual(main(), 0)
        environment = run.call_args.kwargs["env"]
        self.assertEqual(environment["PENNY_DB_NAME"], "penny")
        self.assertEqual(environment["PENNY_DB_USER"], "postgres")
        self.assertEqual(environment["PENNY_DB_HOST"], "localhost")
        self.assertIn("--settings=config.settings.test", run.call_args.args[0])

    def test_configured_password_needs_no_prompt(self) -> None:
        with (
            patch("sys.argv", ["development", "check"]),
            patch("scripts.development.load_local_environment"),
            patch.dict(
                os.environ,
                {
                    "PENNY_DB_PASSWORD": "synthetic-test-input",
                    "PENNY_SECRET_KEY": "synthetic-test-key",
                },
            ),
            patch("scripts.development.getpass.getpass") as prompt,
            patch("scripts.development.subprocess.run") as run,
        ):
            run.return_value.returncode = 0
            self.assertEqual(main(), 0)
        prompt.assert_not_called()
