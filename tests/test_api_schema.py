from io import StringIO

from django.core.management import call_command
from django.test import SimpleTestCase, override_settings
from drf_spectacular.generators import SchemaGenerator
from drf_spectacular.validation import validate_schema
from rest_framework.test import APIClient


class SchemaTests(SimpleTestCase):
    def test_generated_schema_validates_without_warnings_and_documents_auth_and_writes(
        self,
    ) -> None:
        output, errors = StringIO(), StringIO()
        call_command("spectacular", validate=True, fail_on_warn=True, stdout=output, stderr=errors)
        self.assertEqual(errors.getvalue(), "")
        document = SchemaGenerator().get_schema(public=True)
        validate_schema(document)
        for path in (
            "/api/v1/auth/login/",
            "/api/v1/finances/expenses/",
            "/api/v1/debts/records/create/",
            "/api/v1/backups/restore/confirm/",
        ):
            self.assertIn(path, document["paths"])
        headers = {
            item["name"]
            for item in document["paths"]["/api/v1/finances/expenses/"]["post"]["parameters"]
        }
        self.assertTrue({"Idempotency-Key", "X-CSRFToken"} <= headers)
        self.assertNotIn(
            "Idempotency-Key",
            {
                item["name"]
                for item in document["paths"]["/api/v1/finances/exchange-preview/"]["post"][
                    "parameters"
                ]
            },
        )

    def test_interactive_docs_and_schema_are_disabled_by_default(self) -> None:
        client = APIClient()
        self.assertEqual(client.get("/api/v1/schema/").status_code, 403)
        self.assertEqual(client.get("/api/v1/docs/").status_code, 403)

    @override_settings(PENNY_API_DOCS_ENABLED=True)
    def test_local_docs_and_json_schema_are_available_when_enabled(self) -> None:
        client = APIClient()
        self.assertEqual(client.get("/api/v1/docs/").status_code, 200)
        self.assertEqual(client.get("/api/v1/schema/?format=json").status_code, 200)
