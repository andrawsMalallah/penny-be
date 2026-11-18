import re
from datetime import date

from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.pagination import PageNumberPagination
from rest_framework.request import Request
from rest_framework.response import Response

from identity.models import User
from identity.views import ProtectedAPIView

from . import budgets
from .serializers import StrictSerializer, TargetMoneyField


class MonthField(serializers.CharField):
    def to_internal_value(self, data: object) -> date:
        value = super().to_internal_value(data)
        if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", value):
            raise serializers.ValidationError("Choose a valid calendar month.")
        try:
            return date.fromisoformat(f"{value}-01")
        except ValueError:
            raise serializers.ValidationError("Choose a valid calendar month.") from None


class BudgetQuerySerializer(StrictSerializer):
    month = MonthField()
    page = serializers.IntegerField(min_value=1, required=False)


class BudgetInputSerializer(StrictSerializer):
    month = MonthField()
    version = serializers.IntegerField(min_value=0)
    scope = serializers.ChoiceField(choices=["month", "future"])
    limit_iqd = TargetMoneyField(allow_null=True)
    limit_usd = TargetMoneyField(allow_null=True)

    def validate(self, attrs: dict) -> dict:
        for field in ("limit_iqd", "limit_usd"):
            if attrs[field] is not None and attrs[field] <= 0:
                raise serializers.ValidationError({field: ["Enter a positive limit or null."]})
        return attrs


class BudgetListView(ProtectedAPIView):
    @transaction.atomic
    def get(self, request: Request) -> Response:
        serializer = BudgetQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        month = serializer.validated_data["month"]
        User.objects.select_for_update().get(pk=request.user.pk)
        paginator = PageNumberPagination()
        categories = paginator.paginate_queryset(
            budgets.active_categories(request.user, month), request
        )
        return paginator.get_paginated_response(
            budgets.budget_rows(categories, request.user, month)
        )


class BudgetDetailView(ProtectedAPIView):
    @transaction.atomic
    def get(self, request: Request, pk: int) -> Response:
        serializer = BudgetQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        month = serializer.validated_data["month"]
        User.objects.select_for_update().get(pk=request.user.pk)
        category = get_object_or_404(budgets.categories_for_month(request.user, month), pk=pk)
        return Response(budgets.budget_rows([category], request.user, month)[0])

    def post(self, request: Request, pk: int) -> Response:
        serializer = BudgetInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(budgets.save_budget(request.user, pk, serializer.validated_data))
