from unittest.mock import MagicMock, call, patch

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from config.test_runner import ExistingPostgresRunner


class SharedDatabaseRunnerTests(SimpleTestCase):
    def connection(self) -> MagicMock:
        connection = MagicMock()
        connection.settings_dict = {
            "NAME": "penny",
            "ENGINE": "django.db.backends.postgresql",
            "HOST": "localhost",
            "PORT": "5432",
        }
        connection.cursor.return_value.__enter__.return_value.fetchone.return_value = ("penny",)
        return connection

    def test_shared_tables_are_migrated_and_cleaned_before_tests_without_database_creation(
        self,
    ) -> None:
        connection = self.connection()
        with (
            patch("config.test_runner.connections", {"default": connection}),
            patch("config.test_runner.call_command") as command,
        ):
            self.assertEqual(ExistingPostgresRunner().setup_databases(), [])
        self.assertEqual(
            command.call_args_list,
            [
                call("migrate", database="default", interactive=False, verbosity=1),
                call("flush", database="default", interactive=False, verbosity=1),
            ],
        )
        connection.creation.create_test_db.assert_not_called()
        connection.creation.destroy_test_db.assert_not_called()

    def test_actual_database_is_verified_before_migration(self) -> None:
        connection = self.connection()
        connection.cursor.return_value.__enter__.return_value.fetchone.return_value = ("other",)
        with (
            patch("config.test_runner.connections", {"default": connection}),
            patch("config.test_runner.call_command") as command,
            self.assertRaises(ImproperlyConfigured),
        ):
            ExistingPostgresRunner().setup_databases()
        command.assert_not_called()

    def test_teardown_only_closes_connections(self) -> None:
        with patch("config.test_runner.connections") as connections:
            ExistingPostgresRunner().teardown_databases([])
        connections.close_all.assert_called_once_with()
