from uuid import uuid4

from django.shortcuts import get_object_or_404
from rest_framework.pagination import PageNumberPagination
from rest_framework.request import Request
from rest_framework.response import Response

from identity.views import ProtectedAPIView

from .ledger import delete_transaction, save_transaction, transactions_for
from .serializers import (
    TransactionCreateSerializer,
    TransactionDeleteSerializer,
    TransactionFilterSerializer,
    TransactionSerializer,
    TransactionUpdateSerializer,
)


class TransactionRequestIdView(ProtectedAPIView):
    def get(self, request: Request) -> Response:
        return Response({"request_id": str(uuid4())})


class TransactionListView(ProtectedAPIView):
    def get(self, request: Request) -> Response:
        filters = TransactionFilterSerializer(data=request.query_params)
        filters.is_valid(raise_exception=True)
        values = filters.validated_data
        queryset = transactions_for(request.user)
        for field in ("account", "kind", "currency"):
            if field in values:
                queryset = queryset.filter(**{field: values[field]})
        if "start" in values:
            queryset = queryset.filter(date__gte=values["start"])
        if "end" in values:
            queryset = queryset.filter(date__lte=values["end"])
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(queryset, request)
        return paginator.get_paginated_response(TransactionSerializer(page, many=True).data)

    def post(self, request: Request) -> Response:
        serializer = TransactionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        entry, created = save_transaction(request.user, serializer.validated_data)
        return Response(TransactionSerializer(entry).data, status=201 if created else 200)


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
