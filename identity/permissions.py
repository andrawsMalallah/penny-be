from __future__ import annotations

from typing import TYPE_CHECKING

from rest_framework.permissions import BasePermission

if TYPE_CHECKING:
    from rest_framework.request import Request
    from rest_framework.views import APIView


class IsVerifiedUser(BasePermission):
    message = "Verify your email before using your workspace."

    def has_permission(self, request: Request, view: APIView) -> bool:
        return bool(request.user.is_authenticated and request.user.email_verified)
