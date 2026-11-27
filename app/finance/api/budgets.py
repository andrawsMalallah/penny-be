from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.pagination import PageNumberPagination
from rest_framework.request import Request
from rest_framework.response import Response

import app.finance.services.budgets as budgets
from app.finance.api.serializers import MonthField, StrictSerializer, TargetMoneyField
from app.identity.api.views import ProtectedAPIView
from app.identity.models import User


class BudgetQuerySerializer(StrictSerializer):
    month = MonthField()
    page = serializers.IntegerField(min_value=1, required=False)
    category = serializers.IntegerField(min_value=1, required=False)
    category_search = serializers.CharField(
        max_length=100, required=False, allow_blank=True, trim_whitespace=True
    )


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


class BudgetDeleteSerializer(StrictSerializer):
    month = MonthField()
    version = serializers.IntegerField(min_value=0)


class BudgetListView(ProtectedAPIView):
    @transaction.atomic
    def get(self, request: Request) -> Response:
        serializer = BudgetQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        month = serializer.validated_data["month"]
        User.objects.select_for_update().get(pk=request.user.pk)
        paginator = PageNumberPagination()
        active_categories = budgets.active_categories(request.user, month)
        category_search = serializer.validated_data.get("category_search", "")
        if category_search:
            active_categories = active_categories.filter(
                Q(name_en__icontains=category_search) | Q(name_ar__icontains=category_search)
            )
        if "category" in serializer.validated_data:
            active_categories = active_categories.filter(pk=serializer.validated_data["category"])
        categories = paginator.paginate_queryset(active_categories, request)
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


class BudgetDeleteView(ProtectedAPIView):
    def post(self, request: Request, pk: int) -> Response:
        serializer = BudgetDeleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        budgets.delete_budget(request.user, pk, **serializer.validated_data)
        return Response(status=204)
