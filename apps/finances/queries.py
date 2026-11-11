"""Shared financial read queries used by API responses and background exports."""

from django.db.models import Prefetch

from apps.finances.models import LedgerEntry


def operation_query(query):
    return query.select_related("reversal").prefetch_related(
        Prefetch("entries", queryset=LedgerEntry.objects.select_related("balance").order_by("id"))
    )
