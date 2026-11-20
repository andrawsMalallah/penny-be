from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from app.finance.api.serializers import MonthField, StrictSerializer, valid_timezone
from app.finance.services.monthly_review import monthly_period, monthly_review
from app.identity.api.views import ProtectedAPIView


class MonthlyReviewInputSerializer(StrictSerializer):
    month = MonthField()
    timezone = serializers.CharField(max_length=100)
    page = serializers.IntegerField(min_value=1, default=1)

    def validate_timezone(self, value: str) -> str:
        return valid_timezone(value)


class MonthlyReviewView(ProtectedAPIView):
    def get(self, request: Request) -> Response:
        serializer = MonthlyReviewInputSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        period = monthly_period(values["month"], values["timezone"])
        return Response(monthly_review(request.user, period, values["page"]))
