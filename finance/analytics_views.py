from urllib.parse import urlsplit

from django.db import transaction
from rest_framework import serializers
from rest_framework.pagination import PageNumberPagination
from rest_framework.request import Request
from rest_framework.response import Response

from identity.models import User
from identity.views import ProtectedAPIView

from . import analytics
from .serializers import StrictSerializer, valid_timezone


class ReportInputSerializer(StrictSerializer):
    period = serializers.ChoiceField(
        choices=["this_month", "previous_month", "this_year", "custom"], default="this_month"
    )
    timezone = serializers.CharField(max_length=100)
    start = serializers.DateField(required=False, input_formats=["%Y-%m-%d"])
    end = serializers.DateField(required=False, input_formats=["%Y-%m-%d"])

    def validate_timezone(self, value: str) -> str:
        return valid_timezone(value)

    def validate(self, attrs: dict) -> dict:
        if attrs["period"] != "custom" and ("start" in attrs or "end" in attrs):
            raise serializers.ValidationError(
                {"period": ["Dates are only used for custom ranges."]}
            )
        return attrs


class BreakdownInputSerializer(ReportInputSerializer):
    page = serializers.IntegerField(min_value=1, required=False)


class AnalyticsView(ProtectedAPIView):
    @transaction.atomic
    def get(self, request: Request) -> Response:
        serializer = ReportInputSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        period = analytics.report_period(serializer.validated_data)
        User.objects.select_for_update().get(pk=request.user.pk)
        report = analytics.summary(request.user, period)
        report["breakdowns"] = {}
        for section in ("accounts", "groups", "categories", "people"):
            paginator = PageNumberPagination()
            rows = paginator.paginate_queryset(
                analytics.breakdown(request.user, section, period), request
            )
            data = paginator.get_paginated_response(
                [analytics.breakdown_row(row) for row in rows]
            ).data
            # Pagination is followed through section endpoints, never the summary URL.
            base = request.build_absolute_uri(f"/api/v1/analytics/{section}/")
            for link in ("next", "previous"):
                data[link] = f"{base}?{urlsplit(data[link]).query}" if data[link] else None
            report["breakdowns"][section] = data
        return Response(report)


class AnalyticsBreakdownView(ProtectedAPIView):
    @transaction.atomic
    def get(self, request: Request, section: str) -> Response:
        serializer = BreakdownInputSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        period = analytics.report_period(serializer.validated_data)
        User.objects.select_for_update().get(pk=request.user.pk)
        paginator = PageNumberPagination()
        rows = paginator.paginate_queryset(
            analytics.breakdown(request.user, section, period), request
        )
        return paginator.get_paginated_response([analytics.breakdown_row(row) for row in rows])
