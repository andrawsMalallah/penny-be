from django.db import transaction
from django.db.models import Case, CharField, DecimalField, F, Q, Sum, When
from rest_framework import generics
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.finances.models import Account, BudgetMonth, Category, LedgerEntry, Operation, Portfolio
from apps.finances.queries import operation_query
from apps.finances.services import core

from . import serializers as schema
from .base import OwnedList, PrivateResponse, WriteView


class SetupView(WriteView):
    serializer_class = schema.SetupSerializer
    action = "setup"

    def work(self, portfolio: Portfolio, data: dict) -> dict:
        return core.initialize(portfolio, data)


class AccountCreateView(WriteView):
    serializer_class = schema.AccountCreateSerializer
    action = "account.create"

    def work(self, portfolio: Portfolio, data: dict) -> dict:
        return core.create_account(portfolio, data)


class AccountChangeView(WriteView):
    serializer_class = schema.AccountChangeSerializer
    action = "account.change"

    def work(self, portfolio: Portfolio, data: dict, pk: int) -> dict:
        return core.change_account(portfolio, pk, data)


class CategoryCreateView(WriteView):
    serializer_class = schema.CategoryCreateSerializer
    action = "category.create"

    def work(self, portfolio: Portfolio, data: dict) -> dict:
        return core.change_category(portfolio, None, data)


class CategoryChangeView(CategoryCreateView):
    serializer_class = schema.CategoryChangeSerializer
    action = "category.change"

    def work(self, portfolio: Portfolio, data: dict, pk: int) -> dict:
        return core.change_category(portfolio, pk, data)


class PreferencesView(WriteView):
    serializer_class = schema.PreferencesChangeSerializer
    action = "preferences.change"

    def work(self, portfolio: Portfolio, data: dict) -> dict:
        return core.change_preferences(portfolio, data)

    def get(self, request: Request) -> Response:
        portfolio = Portfolio.objects.filter(owner=request.user).first()
        return Response(
            schema.PreferencesSerializer(portfolio or Portfolio(owner=request.user)).data
        )


class TransactionView(WriteView):
    kind = ""

    def work(self, portfolio: Portfolio, data: dict) -> dict:
        return core.transact(portfolio, self.kind, data)


class ExpenseView(TransactionView):
    serializer_class = schema.ExpenseSerializer
    action = kind = "expense"


class IncomeView(TransactionView):
    serializer_class = schema.IncomeSerializer
    action = kind = "income"


class TransferView(TransactionView):
    serializer_class = schema.TransferSerializer
    action = kind = "transfer"


class ExchangeView(TransactionView):
    serializer_class = schema.ExchangeSerializer
    action = kind = "exchange"


class RolloverView(WriteView):
    serializer_class = schema.RolloverSerializer
    action = "rollover"

    def work(self, portfolio: Portfolio, data: dict) -> dict:
        return core.rollover(portfolio, data)


class ReverseView(WriteView):
    serializer_class = schema.ReversalSerializer
    action = "reverse"

    def work(self, portfolio: Portfolio, data: dict, pk: int) -> dict:
        return core.reverse(portfolio, pk, data)


class SpendPreviewView(PrivateResponse, APIView):
    serializer_class = schema.SpendingSerializer

    def post(self, request: Request) -> Response:
        serializer = schema.SpendingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        with transaction.atomic():
            User.objects.select_for_update().get(pk=request.user.pk)
            portfolio = Portfolio.objects.filter(owner=request.user).first()
            if portfolio is None:
                raise NotFound("Financial setup required.")
            core.check_date(portfolio, data["local_date"], data["timezone"])
            return Response(
                {
                    "allocations": core.allocation(
                        portfolio, data["source_id"], data["currency"], data["amount"]
                    ),
                    "revision": portfolio.revision,
                }
            )


class ExchangePreviewView(SpendPreviewView):
    serializer_class = schema.ExchangeSerializer

    def post(self, request: Request) -> Response:
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            User.objects.select_for_update().get(pk=request.user.pk)
            portfolio = Portfolio.objects.filter(owner=request.user).first()
            if portfolio is None:
                raise NotFound("Financial setup required.")
            return Response(
                {
                    "quote": core.exchange_preview(portfolio, serializer.validated_data),
                    "revision": portfolio.revision,
                }
            )


class RolloverPreviewView(ExchangePreviewView):
    serializer_class = schema.RolloverSerializer

    def post(self, request: Request) -> Response:
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            User.objects.select_for_update().get(pk=request.user.pk)
            portfolio = Portfolio.objects.filter(owner=request.user).first()
            if portfolio is None:
                raise NotFound("Financial setup required.")
            return Response(
                {
                    "quote": core.rollover_preview(portfolio, serializer.validated_data),
                    "revision": portfolio.revision,
                }
            )


class AccountList(OwnedList):
    queryset = Account.objects.prefetch_related("balances")
    serializer_class = schema.AccountSerializer


class CategoryList(OwnedList):
    queryset = Category.objects.all()
    serializer_class = schema.CategorySerializer


class MonthList(OwnedList):
    queryset = BudgetMonth.objects.all()
    serializer_class = schema.MonthSerializer


def filtered_operations(request: Request):
    serializer = schema.ActivityFilters(data=request.query_params)
    serializer.is_valid(raise_exception=True)
    filters = serializer.validated_data
    query = Operation.objects.filter(owner=request.user)
    for field, lookup in {
        "start": "local_date__gte",
        "end": "local_date__lte",
        "kind": "kind",
        "category_id": "category_id",
        "account_id": "entries__balance__account_id",
        "currency": "entries__balance__currency",
    }.items():
        if field in filters:
            if field == "category_id":
                query = query.filter(
                    Q(category_id=filters[field]) | Q(reverses__category_id=filters[field])
                )
            elif field == "currency":
                query = query.filter(
                    Q(entries__balance__currency=filters[field])
                    | Q(details__currency=filters[field])
                    | Q(details__debt_currency=filters[field])
                    | Q(details__payment_currency=filters[field])
                )
            else:
                query = query.filter(**{lookup: filters[field]})
    return query.distinct()


class ActivityList(OwnedList):
    serializer_class = schema.OperationSerializer

    def get_queryset(self):
        return operation_query(filtered_operations(self.request))


class OperationDetail(PrivateResponse, generics.RetrieveAPIView):
    serializer_class = schema.OperationSerializer

    def get_queryset(self):
        return operation_query(Operation.objects.filter(owner=self.request.user))


class ReportsView(PrivateResponse, APIView):
    def get(self, request: Request) -> Response:
        query = filtered_operations(request)
        # One aggregate statement provides a coherent snapshot across income and expenses.
        totals = (
            LedgerEntry.objects.filter(operation__in=query)
            .filter(
                Q(operation__kind__in=["income", "expense"])
                | Q(operation__kind="reversal", operation__reverses__kind__in=["income", "expense"])
            )
            .annotate(
                report_kind=Case(
                    When(operation__kind="reversal", then=F("operation__reverses__kind")),
                    default=F("operation__kind"),
                    output_field=CharField(),
                )
            )
            .values("report_kind", "balance__currency")
            .annotate(
                total=Sum("delta", output_field=DecimalField(max_digits=30, decimal_places=2))
            )
        )
        result = {
            kind: {currency: "0.00" for currency in ("IQD", "USD")}
            for kind in ("income", "expense")
        }
        for row in totals:
            kind = row["report_kind"]
            result[kind][row["balance__currency"]] = core.money(
                row["total"] * (-1 if kind == "expense" else 1)
            )
        return Response(result)
