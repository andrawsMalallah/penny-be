from django.db.models import Q, Sum
from rest_framework import generics
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.debts import services
from apps.debts.models import Debt, DebtEvent, Person
from apps.finances.api.base import OwnedList, PrivateResponse, WriteView
from apps.finances.models import Portfolio
from apps.finances.services.core import money

from . import serializers as schema


class PersonCreate(WriteView):
    action = "person.create"
    serializer_class = schema.PersonCreateSerializer

    def work(self, portfolio: Portfolio, data: dict) -> dict:
        return services.change_person(portfolio, None, data)


class PersonChange(WriteView):
    action = "person.change"
    serializer_class = schema.PersonChangeSerializer

    def work(self, portfolio: Portfolio, data: dict, pk: int) -> dict:
        return services.change_person(portfolio, pk, data)


class CreateDebt(WriteView):
    action = "debt.create"
    serializer_class = schema.DebtCreateSerializer

    def work(self, portfolio: Portfolio, data: dict) -> dict:
        return services.create_debt(portfolio, data)


class RepayDebt(WriteView):
    action = "debt.repay"
    serializer_class = schema.RepaymentSerializer

    def work(self, portfolio: Portfolio, data: dict, pk: int) -> dict:
        return services.settle(portfolio, pk, "debt_repayment", data)


class WriteOffDebt(WriteView):
    action = "debt.writeoff"
    serializer_class = schema.WriteOffSerializer

    def work(self, portfolio: Portfolio, data: dict, pk: int) -> dict:
        return services.settle(portfolio, pk, "debt_writeoff", data)


class ReverseEvent(WriteView):
    action = "debt.reverse"
    serializer_class = schema.EventReverseSerializer

    def work(self, portfolio: Portfolio, data: dict, pk: int) -> dict:
        return services.reverse_event(portfolio, pk, data)


class PersonList(OwnedList):
    queryset = Person.objects.all()
    serializer_class = schema.PersonSerializer

    def get_queryset(self):
        query = super().get_queryset()
        return query.annotate(
            **{
                f"{direction}_{currency.lower()}": Sum(
                    "debts__remaining_amount",
                    filter=Q(
                        debts__direction=direction, debts__currency=currency, debts__cancelled=False
                    ),
                    default=0,
                )
                for direction in ("receivable", "payable")
                for currency in ("IQD", "USD")
            }
        )


class DebtList(OwnedList):
    queryset = Debt.objects.all()
    serializer_class = schema.DebtSerializer

    def get_queryset(self):
        query = self.queryset.filter(owner=self.request.user)
        filters = schema.DebtFilters(data=self.request.query_params)
        filters.is_valid(raise_exception=True)
        for field in ("currency", "direction", "person_id"):
            if field in filters.validated_data:
                query = query.filter(**{field: filters.validated_data[field]})
        status = filters.validated_data.get("status", "all")
        if status == "active":
            query = query.filter(cancelled=False, remaining_amount__gt=0)
        elif status == "settled":
            query = query.filter(cancelled=False, remaining_amount=0)
        elif status == "cancelled":
            query = query.filter(cancelled=True)
        if "person_id" in self.kwargs:
            if not Person.objects.filter(
                owner=self.request.user, pk=self.kwargs["person_id"]
            ).exists():
                raise NotFound()
            query = query.filter(person_id=self.kwargs["person_id"])
        return query


class DebtDetail(PrivateResponse, generics.RetrieveAPIView):
    serializer_class = schema.DebtSerializer

    def get_queryset(self):
        return Debt.objects.filter(owner=self.request.user)


class EventList(OwnedList):
    serializer_class = schema.EventSerializer

    def get_queryset(self):
        if not Debt.objects.filter(owner=self.request.user, pk=self.kwargs["pk"]).exists():
            raise NotFound()
        return (
            DebtEvent.objects.filter(debt_id=self.kwargs["pk"], debt__owner=self.request.user)
            .select_related("operation__reversal")
            .prefetch_related("operation__entries__balance")
        )


class DebtSummary(PrivateResponse, APIView):
    def get(self, request: Request) -> Response:
        totals = (
            Debt.objects.filter(owner=request.user, cancelled=False)
            .values("direction", "currency")
            .annotate(remaining=Sum("remaining_amount"))
        )
        result = {
            direction: {currency: "0.00" for currency in ("IQD", "USD")}
            for direction in ("receivable", "payable")
        }
        for row in totals:
            result[row["direction"]][row["currency"]] = money(row["remaining"])
        return Response(result)
