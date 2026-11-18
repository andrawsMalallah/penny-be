from uuid import uuid4

from django.http import FileResponse
from django.shortcuts import get_object_or_404
from rest_framework.pagination import PageNumberPagination
from rest_framework.request import Request
from rest_framework.response import Response

from identity.views import ProtectedAPIView

from .ledger import delete_transaction, save_transaction, transactions_for
from .serializers import (
    ActivityFilterSerializer,
    TransactionCreateSerializer,
    TransactionDeleteSerializer,
    TransactionExportSerializer,
    TransactionFilterSerializer,
    TransactionSerializer,
    TransactionUpdateSerializer,
)
from .transaction_export import export_transactions


class TransactionRequestIdView(ProtectedAPIView):
    def get(self, request: Request) -> Response:
        return Response({"request_id": str(uuid4())})


class TransactionListView(ProtectedAPIView):
    def get(self, request: Request) -> Response:
        filters = TransactionFilterSerializer(data=request.query_params)
        filters.is_valid(raise_exception=True)
        queryset = transactions_for(request.user, filters.validated_data)
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(queryset, request)
        return paginator.get_paginated_response(TransactionSerializer(page, many=True).data)

    def post(self, request: Request) -> Response:
        serializer = TransactionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        entry, created = save_transaction(request.user, serializer.validated_data)
        return Response(TransactionSerializer(entry).data, status=201 if created else 200)


class TransactionExportCountView(ProtectedAPIView):
    def get(self, request: Request) -> Response:
        serializer = ActivityFilterSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        return Response(
            {"count": transactions_for(request.user, serializer.validated_data).count()}
        )


class TransactionExportView(ProtectedAPIView):
    def get(self, request: Request) -> FileResponse:
        serializer = TransactionExportSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        document = export_transactions(request.user, values)
        return FileResponse(
            document,
            as_attachment=True,
            filename=f"penny-activity-{values['language']}.csv",
            content_type="text/csv; charset=utf-8",
        )


class TransactionDetailView(ProtectedAPIView):
    def get(self, request: Request, pk: int) -> Response:
        return Response(
            TransactionSerializer(get_object_or_404(transactions_for(request.user), pk=pk)).data
        )

    def post(self, request: Request, pk: int) -> Response:
        serializer = TransactionUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        entry, _ = save_transaction(request.user, serializer.validated_data, pk)
        return Response(TransactionSerializer(entry).data)


class TransactionDeleteView(ProtectedAPIView):
    def post(self, request: Request, pk: int) -> Response:
        serializer = TransactionDeleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        delete_transaction(request.user, pk, serializer.validated_data["version"])
        return Response(status=204)
