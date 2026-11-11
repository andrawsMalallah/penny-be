"""Shared request boundaries for owned, private and idempotent financial APIs."""

from uuid import UUID

from rest_framework import generics
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import CursorPagination
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import DeviceSession
from apps.finances.models import Portfolio
from apps.finances.services import core

from . import serializers as schema


class PrivateResponse:
    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        return response


class WriteView(PrivateResponse, APIView):
    serializer_class = None
    action = ""

    def work(self, portfolio: Portfolio, data: dict, **kwargs) -> dict:
        raise NotImplementedError

    def post(self, request: Request, **kwargs) -> Response:
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            key = UUID(request.headers.get("Idempotency-Key", ""))
        except ValueError, AttributeError:
            raise ValidationError(
                {"Idempotency-Key": "Supply a UUID for every financial write."}
            ) from None
        result = core.execute(
            request.user,
            key,
            self.action,
            {**serializer.validated_data, "resource": kwargs},
            lambda portfolio: self.work(portfolio, serializer.validated_data, **kwargs),
            device_id=request.auth.pk if isinstance(request.auth, DeviceSession) else None,
            session_key=request.session.session_key,
        )
        return Response(result)


class Pagination(CursorPagination):
    ordering = "-id"


class OwnedList(PrivateResponse, generics.ListAPIView):
    pagination_class = Pagination

    def get_queryset(self):
        query = self.queryset.filter(owner=self.request.user)
        has_archive = any(field.name == "archived" for field in self.queryset.model._meta.fields)
        filters = (schema.MetadataFilters if has_archive else schema.CursorFilters)(
            data=self.request.query_params
        )
        filters.is_valid(raise_exception=True)
        if "archived" in filters.validated_data:
            query = query.filter(archived=filters.validated_data["archived"])
        return query
