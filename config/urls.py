from django.db import DatabaseError, connection, transaction
from django.urls import include, path
from drf_spectacular.utils import extend_schema
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .schema import LocalDocumentationPermission


class HealthView(APIView):
    """Liveness only: deliberately reveals no configuration or database details."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request: Request) -> Response:
        return Response({"status": "ok"})


class ReadinessView(APIView):
    """Check database availability without disclosing configuration or stored data."""

    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(responses={200: {"type": "object"}, 503: {"type": "object"}})
    def get(self, request: Request) -> Response:
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = '2s'")
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except DatabaseError:
            return Response(
                {"status": "unavailable"}, status=503, headers={"Cache-Control": "no-store"}
            )
        return Response({"status": "ok"}, headers={"Cache-Control": "no-store"})


urlpatterns = [
    path(
        "api/v1/schema/",
        SpectacularAPIView.as_view(permission_classes=[LocalDocumentationPermission]),
        name="api-schema",
    ),
    path(
        "api/v1/docs/",
        SpectacularSwaggerView.as_view(
            url_name="api-schema", permission_classes=[LocalDocumentationPermission]
        ),
        name="api-docs",
    ),
    path("api/v1/health/", HealthView.as_view(), name="health"),
    path("api/v1/ready/", ReadinessView.as_view(), name="readiness"),
    path("api/v1/auth/", include("apps.accounts.api.urls")),
    path("api/v1/finances/", include("apps.finances.api.urls")),
    path("api/v1/debts/", include("apps.debts.api.urls")),
    path("api/v1/backups/", include("apps.backups.urls")),
]
