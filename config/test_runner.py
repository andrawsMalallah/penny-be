from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import connections
from django.test.runner import DiscoverRunner


class ExistingPostgresRunner(DiscoverRunner):
    """Use the approved local penny tables without creating or dropping a database."""

    def setup_databases(self, **kwargs) -> list:
        connection = connections["default"]
        database = connection.settings_dict
        if (
            database["NAME"] != "penny"
            or database["ENGINE"] != "django.db.backends.postgresql"
            or database["HOST"] not in {"localhost", "127.0.0.1", "::1"}
            or str(database["PORT"]) != "5432"
            or self.parallel > 1
        ):
            raise ImproperlyConfigured(
                "Tests require shared local penny tables without parallelism."
            )
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database()")
            if cursor.fetchone() != ("penny",):
                raise ImproperlyConfigured("Refusing tests outside penny.")
        call_command("migrate", database="default", interactive=False, verbosity=self.verbosity)
        # The shared database can contain development accounts before the first TestCase.
        # Start from deterministic tables, using the user-approved local test cleanup.
        call_command("flush", database="default", interactive=False, verbosity=self.verbosity)
        # Bypass Django's database creation machinery entirely. TestCase rolls back;
        # TransactionTestCase may flush these shared tables, as approved by the user.
        return []

    def teardown_databases(self, old_config, **kwargs) -> None:
        connections.close_all()
