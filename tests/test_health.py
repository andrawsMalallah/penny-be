from django.test import SimpleTestCase
from rest_framework.permissions import IsAuthenticated
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView


class HealthTests(SimpleTestCase):
    def test_public_liveness_without_database_or_secrets(self) -> None:
        response = self.client.get("/api/v1/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertEqual(response["Content-Type"], "application/json")

    def test_health_rejects_mutations(self) -> None:
        self.assertEqual(self.client.post("/api/v1/health/").status_code, 405)

    def test_unknown_urls_do_not_expose_debug_details(self) -> None:
        response = self.client.get("/missing/")
        self.assertEqual(response.status_code, 404)
        self.assertNotContains(response, "Traceback", status_code=404)

    def test_default_api_policy_denies_anonymous_access(self) -> None:
        self.assertEqual(APIView.permission_classes, [IsAuthenticated])
        response = APIView.as_view()(APIRequestFactory().get("/"))
        self.assertEqual(response.status_code, 403)
