from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.request import Request
from rest_framework.response import Response

from identity.views import ProtectedAPIView

from . import categories, services
from .models import AccountGroup
from .serializers import (
    AccountCreateSerializer,
    AccountInputSerializer,
    AccountSerializer,
    ArchiveSerializer,
    CategoryInputSerializer,
    CategorySerializer,
    GroupInputSerializer,
    GroupSerializer,
    StrictSerializer,
)


def paginated(
    queryset: QuerySet,
    serializer_class: type[AccountSerializer] | type[GroupSerializer] | type[CategorySerializer],
    request: Request,
) -> Response:
    archived = request.query_params.get("archived", "false")
    if archived not in ["true", "false", "all"]:
        raise ValidationError({"archived": ["Choose true, false or all."]})
    paginator = PageNumberPagination()
    if archived != "all":
        queryset = queryset.filter(archived=archived == "true")
    page = paginator.paginate_queryset(queryset, request)
    return paginator.get_paginated_response(serializer_class(page, many=True).data)


class CategoryInitializeView(ProtectedAPIView):
    def post(self, request: Request) -> Response:
        serializer = StrictSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        categories.initialize_categories(request.user)
        return Response(status=204)


class CategoryListView(ProtectedAPIView):
    def get(self, request: Request) -> Response:
        return paginated(categories.categories_for(request.user), CategorySerializer, request)

    def post(self, request: Request) -> Response:
        serializer = CategoryInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(
            CategorySerializer(
                categories.save_category(request.user, serializer.validated_data)
            ).data,
            status=201,
        )


class CategoryDetailView(ProtectedAPIView):
    def get(self, request: Request, pk: int) -> Response:
        return Response(
            CategorySerializer(
                get_object_or_404(categories.categories_for(request.user), pk=pk)
            ).data
        )

    def post(self, request: Request, pk: int) -> Response:
        serializer = CategoryInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(
            CategorySerializer(
                categories.save_category(request.user, serializer.validated_data, pk)
            ).data
        )


class CategoryArchiveView(ProtectedAPIView):
    def post(self, request: Request, pk: int) -> Response:
        serializer = ArchiveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(
            CategorySerializer(
                categories.archive_category(request.user, pk, serializer.validated_data["archived"])
            ).data
        )


class GroupListView(ProtectedAPIView):
    def get(self, request: Request) -> Response:
        return paginated(AccountGroup.objects.filter(user=request.user), GroupSerializer, request)

    def post(self, request: Request) -> Response:
        serializer = GroupInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(
            GroupSerializer(services.save_group(request.user, serializer.validated_data)).data,
            status=201,
        )


class GroupDetailView(ProtectedAPIView):
    def get(self, request: Request, pk: int) -> Response:
        return Response(
            GroupSerializer(get_object_or_404(AccountGroup, user=request.user, pk=pk)).data
        )

    def post(self, request: Request, pk: int) -> Response:
        serializer = GroupInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(
            GroupSerializer(services.save_group(request.user, serializer.validated_data, pk)).data
        )


class GroupArchiveView(ProtectedAPIView):
    def post(self, request: Request, pk: int) -> Response:
        serializer = ArchiveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(
            GroupSerializer(
                services.archive_group(request.user, pk, serializer.validated_data["archived"])
            ).data
        )


class AccountListView(ProtectedAPIView):
    def get(self, request: Request) -> Response:
        return paginated(services.accounts_for(request.user), AccountSerializer, request)

    def post(self, request: Request) -> Response:
        serializer = AccountCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(
            AccountSerializer(services.save_account(request.user, serializer.validated_data)).data,
            status=201,
        )


class AccountDetailView(ProtectedAPIView):
    def get(self, request: Request, pk: int) -> Response:
        return Response(
            AccountSerializer(get_object_or_404(services.accounts_for(request.user), pk=pk)).data
        )

    def post(self, request: Request, pk: int) -> Response:
        serializer = AccountInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(
            AccountSerializer(
                services.save_account(request.user, serializer.validated_data, pk)
            ).data
        )


class AccountArchiveView(ProtectedAPIView):
    def post(self, request: Request, pk: int) -> Response:
        serializer = ArchiveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(
            AccountSerializer(
                services.archive_account(request.user, pk, serializer.validated_data["archived"])
            ).data
        )
