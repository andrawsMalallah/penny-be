from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.pagination import PageNumberPagination
from rest_framework.request import Request
from rest_framework.response import Response

import app.finance.services.recurring as recurring
from app.finance.api.serializers import (
    MoneyField,
    StrictSerializer,
    TransactionInputSerializer,
    valid_timezone,
)
from app.finance.models import Currency, RecurringFrequency, RecurringSchedule, RecurringState
from app.identity.api.views import ProtectedAPIView
from app.identity.models import User


class ZoneSerializer(StrictSerializer):
    timezone = serializers.CharField(max_length=100)

    def validate_timezone(self, value: str) -> str:
        return valid_timezone(value)


class ScheduleInputSerializer(ZoneSerializer):
    name = serializers.CharField(max_length=100)
    kind = serializers.ChoiceField(choices=["income", "expense"])
    account = serializers.IntegerField(min_value=1)
    category = serializers.IntegerField(min_value=1, allow_null=True)
    currency = serializers.ChoiceField(choices=Currency.choices)
    amount = MoneyField()
    note = serializers.CharField(max_length=500, allow_blank=True)
    frequency = serializers.ChoiceField(choices=RecurringFrequency.choices)
    start_date = serializers.DateField(input_formats=["%Y-%m-%d"])
    end_date = serializers.DateField(input_formats=["%Y-%m-%d"], allow_null=True)

    def validate(self, attrs: dict) -> dict:
        if attrs["amount"] <= 0:
            raise serializers.ValidationError({"amount": ["Enter a positive amount."]})
        if (attrs["kind"] == "expense") != (attrs["category"] is not None):
            raise serializers.ValidationError({"category": ["Only expenses require a category."]})
        if attrs["end_date"] and attrs["end_date"] < attrs["start_date"]:
            raise serializers.ValidationError({"end_date": ["End on or after the start date."]})
        return attrs


class ScheduleCreateSerializer(ScheduleInputSerializer):
    request_id = serializers.UUIDField()


class ScheduleUpdateSerializer(ScheduleInputSerializer):
    version = serializers.IntegerField(min_value=1)


class ScheduleStateSerializer(ZoneSerializer):
    version = serializers.IntegerField(min_value=1)
    state = serializers.ChoiceField(choices=RecurringState.choices)


class ScheduleQuerySerializer(StrictSerializer):
    page = serializers.IntegerField(min_value=1, required=False)
    state = serializers.ChoiceField(choices=[*RecurringState.values, "all"], default="all")


class PendingQuerySerializer(ZoneSerializer):
    after = serializers.DateField(input_formats=["%Y-%m-%d"], required=False)


class OccurrenceSerializer(ZoneSerializer):
    version = serializers.IntegerField(min_value=1)
    scheduled_date = serializers.DateField(input_formats=["%Y-%m-%d"])


class ConfirmSerializer(OccurrenceSerializer):
    transaction = TransactionInputSerializer()


class ScheduleSerializer(serializers.ModelSerializer):
    account_name = serializers.CharField(source="account.name")
    account_archived = serializers.BooleanField(source="account.archived")
    opening_date = serializers.DateField(source="account.opening.effective_date")
    category_name_en = serializers.CharField(source="category.name_en", default="")
    category_name_ar = serializers.CharField(source="category.name_ar", default="")
    category_archived = serializers.BooleanField(source="category.archived", default=False)

    class Meta:
        model = RecurringSchedule
        fields = [
            "id",
            "name",
            "kind",
            "account",
            "account_name",
            "account_archived",
            "opening_date",
            "category",
            "category_name_en",
            "category_name_ar",
            "category_archived",
            "currency",
            "amount",
            "note",
            "frequency",
            "start_date",
            "end_date",
            "state",
            "version",
        ]
        read_only_fields = fields


class ScheduleListView(ProtectedAPIView):
    def get(self, request: Request) -> Response:
        serializer = ScheduleQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        queryset = recurring.schedules_for(request.user)
        if serializer.validated_data["state"] != "all":
            queryset = queryset.filter(state=serializer.validated_data["state"])
        paginator = PageNumberPagination()
        rows = paginator.paginate_queryset(queryset, request)
        return paginator.get_paginated_response(ScheduleSerializer(rows, many=True).data)

    def post(self, request: Request) -> Response:
        serializer = ScheduleCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        schedule, created = recurring.save_schedule(request.user, serializer.validated_data)
        return Response(ScheduleSerializer(schedule).data, status=201 if created else 200)


class ScheduleDetailView(ProtectedAPIView):
    def get(self, request: Request, pk: int) -> Response:
        return Response(
            ScheduleSerializer(get_object_or_404(recurring.schedules_for(request.user), pk=pk)).data
        )

    def post(self, request: Request, pk: int) -> Response:
        serializer = ScheduleUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        schedule, _ = recurring.save_schedule(request.user, serializer.validated_data, pk)
        return Response(ScheduleSerializer(schedule).data)


class ScheduleStateView(ProtectedAPIView):
    def post(self, request: Request, pk: int) -> Response:
        serializer = ScheduleStateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(
            ScheduleSerializer(
                recurring.change_state(request.user, pk, serializer.validated_data)
            ).data
        )


class PendingView(ProtectedAPIView):
    @transaction.atomic
    def get(self, request: Request, pk: int) -> Response:
        serializer = PendingQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        User.objects.select_for_update().get(pk=request.user.pk)
        schedule = get_object_or_404(recurring.schedules_for(request.user), pk=pk)
        today = recurring.today_in(serializer.validated_data["timezone"])
        return Response(
            {
                "schedule": ScheduleSerializer(schedule).data,
                "today": today.isoformat(),
                **recurring.pending(schedule, today, serializer.validated_data.get("after")),
            }
        )


class ResolveView(ProtectedAPIView):
    def post(self, request: Request, pk: int, skip: bool = False) -> Response:
        serializer = (
            OccurrenceSerializer(data=request.data)
            if skip
            else ConfirmSerializer(data=request.data)
        )
        serializer.is_valid(raise_exception=True)
        outcome = recurring.resolve(request.user, pk, serializer.validated_data, skip)
        return Response(
            {
                "scheduled_date": outcome.scheduled_date.isoformat(),
                "skipped": outcome.skipped,
                "transaction": outcome.transaction_id,
            }
        )
