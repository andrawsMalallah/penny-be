from django.db import connection
from django.http import HttpRequest, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET


@require_GET
@never_cache
def ready(request: HttpRequest) -> JsonResponse:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        # Readiness must never disclose connection credentials or database errors.
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ready"})
