from django.shortcuts import get_object_or_404
from rest_framework.pagination import PageNumberPagination
from rest_framework.request import Request
from rest_framework.response import Response

from app.finance.api.debt_serializers import (
    DebtCreateSerializer,
    DebtFilterSerializer,
    DebtPersonFilterSerializer,
    DebtPersonSerializer,
    DebtSerializer,
    DebtUpdateSerializer,
    RepaymentCreateSerializer,
    RepaymentUpdateSerializer,
)
from app.finance.api.serializers import TransactionDeleteSerializer, TransactionSerializer
from app.finance.models import DebtPerson
from app.finance.services.debts import (
    debts_for,
    delete_debt,
    delete_repayment,
    repayments_for,
    save_debt,
    save_repayment,
)
from app.identity.api.views import ProtectedAPIView


def paginated(queryset, serializer, request: Request) -> Response:
    paginator = PageNumberPagination()
    page = paginator.paginate_queryset(queryset, request)
    return paginator.get_paginated_response(serializer(page, many=True).data)


class DebtPersonListView(ProtectedAPIView):
    def get(self, request: Request) -> Response:
        filters = DebtPersonFilterSerializer(data=request.query_params)
        filters.is_valid(raise_exception=True)
        return paginated(
            DebtPerson.objects.filter(user=request.user), DebtPersonSerializer, request
        )


class DebtPersonDetailView(ProtectedAPIView):
    def get(self, request: Request, pk: int) -> Response:
        return Response(
            DebtPersonSerializer(get_object_or_404(DebtPerson, user=request.user, pk=pk)).data
        )


class DebtListView(ProtectedAPIView):
    def get(self, request: Request) -> Response:
        filters = DebtFilterSerializer(data=request.query_params)
        filters.is_valid(raise_exception=True)
        values = filters.validated_data
        queryset = debts_for(request.user)
        for field in ("person", "direction", "currency"):
            if field in values:
                queryset = queryset.filter(**{field: values[field]})
        if values["status"] == "open":
            queryset = queryset.filter(remaining__gt=0)
        elif values["status"] == "settled":
            queryset = queryset.filter(remaining=0)
        return paginated(queryset, DebtSerializer, request)

    def post(self, request: Request) -> Response:
        serializer = DebtCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        debt, created = save_debt(request.user, serializer.validated_data)
        return Response(DebtSerializer(debt).data, status=201 if created else 200)


class DebtDetailView(ProtectedAPIView):
    def get(self, request: Request, pk: int) -> Response:
        return Response(DebtSerializer(get_object_or_404(debts_for(request.user), pk=pk)).data)

    def post(self, request: Request, pk: int) -> Response:
        serializer = DebtUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        debt, _ = save_debt(request.user, serializer.validated_data, pk)
        return Response(DebtSerializer(debt).data)


class DebtDeleteView(ProtectedAPIView):
    def post(self, request: Request, pk: int) -> Response:
        serializer = TransactionDeleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        delete_debt(request.user, pk, serializer.validated_data["version"])
        return Response(status=204)


class RepaymentListView(ProtectedAPIView):
    def get(self, request: Request, pk: int) -> Response:
        filters = DebtPersonFilterSerializer(data=request.query_params)
        filters.is_valid(raise_exception=True)
        get_object_or_404(debts_for(request.user), pk=pk)
        return paginated(repayments_for(request.user, pk), TransactionSerializer, request)

    def post(self, request: Request, pk: int) -> Response:
        serializer = RepaymentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        entry, created = save_repayment(request.user, pk, serializer.validated_data)
        return Response(TransactionSerializer(entry).data, status=201 if created else 200)


class RepaymentDetailView(ProtectedAPIView):
    def get(self, request: Request, pk: int, entry_id: int) -> Response:
        get_object_or_404(debts_for(request.user), pk=pk)
        return Response(
            TransactionSerializer(
                get_object_or_404(repayments_for(request.user, pk), pk=entry_id)
            ).data
        )

    def post(self, request: Request, pk: int, entry_id: int) -> Response:
        serializer = RepaymentUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        entry, _ = save_repayment(request.user, pk, serializer.validated_data, entry_id)
        return Response(TransactionSerializer(entry).data)


class RepaymentDeleteView(ProtectedAPIView):
    def post(self, request: Request, pk: int, entry_id: int) -> Response:
        serializer = TransactionDeleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        delete_repayment(request.user, pk, entry_id, serializer.validated_data["version"])
        return Response(status=204)
