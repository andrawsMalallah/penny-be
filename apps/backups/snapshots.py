import json
from collections import defaultdict
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import models
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from apps.debts.models import Debt, DebtEvent, Person
from apps.finances.models import (
    Account,
    Balance,
    BudgetMonth,
    Category,
    LedgerEntry,
    Operation,
    Portfolio,
)

MAX_ROWS = 20000
MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
# Whitelist financial fields. Authentication tables and owner identifiers never enter a backup.
SPECS = {
    "months": (BudgetMonth, "owner_id", ["id", "month", "closed"]),
    "accounts": (Account, "owner_id", ["id", "kind", "name", "archived"]),
    "balances": (Balance, "account__owner_id", ["id", "account_id", "currency", "amount"]),
    "categories": (Category, "owner_id", ["id", "name_en", "name_ar", "archived"]),
    "operations": (
        Operation,
        "owner_id",
        [
            "id",
            "kind",
            "local_date",
            "timezone",
            "note",
            "category_id",
            "details",
            "reverses_id",
            "created_at",
        ],
    ),
    "entries": (LedgerEntry, "operation__owner_id", ["id", "operation_id", "balance_id", "delta"]),
    "people": (Person, "owner_id", ["id", "name", "phone", "note", "archived"]),
    "debts": (
        Debt,
        "owner_id",
        [
            "id",
            "person_id",
            "direction",
            "currency",
            "original_amount",
            "remaining_amount",
            "note",
            "due_date",
            "cancelled",
        ],
    ),
    "events": (DebtEvent, "debt__owner_id", ["id", "debt_id", "operation_id", "remaining_delta"]),
}
RELATIONS = {
    "account_id": "accounts",
    "category_id": "categories",
    "reverses_id": "operations",
    "operation_id": "operations",
    "balance_id": "balances",
    "person_id": "people",
    "debt_id": "debts",
}
PREFERENCE_FIELDS = ["active_month", "exchange_rate", "language", "theme"]
OPERATION_KINDS = {
    "opening",
    "income",
    "expense",
    "transfer",
    "exchange",
    "rollover",
    "reversal",
    "debt_create",
    "debt_repayment",
    "debt_writeoff",
    "debt_reversal",
}


def snapshot(portfolio: Portfolio) -> dict:
    records = {}
    count = 0
    encoded_size = 0
    for name, (model, ownership, fields) in SPECS.items():
        rows = []
        query = (
            model.objects.filter(**{ownership: portfolio.owner_id}).order_by("pk").values(*fields)
        )
        for row in query.iterator(chunk_size=500):
            count += 1
            if count > MAX_ROWS:
                raise ValidationError("Financial export exceeds the record limit.")
            encoded_size += (
                len(
                    json.dumps(
                        row,
                        default=lambda value: (
                            value.isoformat() if hasattr(value, "isoformat") else str(value)
                        ),
                    ).encode()
                )
                + 2
            )
            if encoded_size > MAX_DOCUMENT_BYTES:
                raise ValidationError("Financial export exceeds the size limit.")
            rows.append(row)
        records[name] = rows
    document = json.loads(
        json.dumps(
            {
                "format": "penny-finances",
                "version": 1,
                "preferences": {field: getattr(portfolio, field) for field in PREFERENCE_FIELDS},
                "records": records,
            },
            default=lambda value: value.isoformat() if hasattr(value, "isoformat") else str(value),
        )
    )
    if len(json.dumps(document).encode()) > MAX_DOCUMENT_BYTES:
        raise ValidationError("Financial export exceeds the size limit.")
    return document


def invalid() -> None:
    raise ValidationError("Invalid or inconsistent financial backup.")


def scalar(field: models.Field, value):
    if value is None:
        if not field.null:
            invalid()
        return None
    if field.is_relation or field.primary_key:
        if type(value) is not int or not 0 < value < 2**63:
            invalid()
        return value
    if isinstance(field, models.JSONField):
        if not isinstance(value, dict):
            invalid()
        return value
    if isinstance(field, models.BooleanField) and type(value) is not bool:
        invalid()
    if isinstance(field, models.DecimalField):
        if not isinstance(value, str):
            invalid()
        return serializers.DecimalField(
            max_digits=field.max_digits, decimal_places=field.decimal_places
        ).run_validation(value)
    if isinstance(field, (models.CharField, models.TextField)) and not isinstance(value, str):
        invalid()
    if isinstance(field, (models.DateField, models.DateTimeField)) and not isinstance(value, str):
        invalid()
    try:
        parsed = field.to_python(value)
        field.validate(parsed, None)
        field.run_validators(parsed)
    except DjangoValidationError, TypeError, ValueError:
        invalid()
    if isinstance(field, models.DateTimeField) and timezone.is_naive(parsed):
        invalid()
    return parsed


def validate(document: dict) -> tuple[dict, dict]:
    if (
        not isinstance(document, dict)
        or set(document) != {"format", "version", "preferences", "records"}
        or document["format"] != "penny-finances"
        or type(document["version"]) is not int
        or document["version"] != 1
    ):
        invalid()
    if not isinstance(document["preferences"], dict) or set(document["preferences"]) != set(
        PREFERENCE_FIELDS
    ):
        invalid()
    preferences = {
        field: scalar(Portfolio._meta.get_field(field), document["preferences"][field])
        for field in PREFERENCE_FIELDS
    }
    if (
        preferences["exchange_rate"] <= 0
        or preferences["language"] not in {"system", "en", "ar"}
        or preferences["theme"] not in {"system", "light", "dark"}
    ):
        invalid()
    records = document["records"]
    if not isinstance(records, dict) or set(records) != set(SPECS):
        invalid()
    parsed = {}
    count = 0
    for name, (model, _, fields) in SPECS.items():
        rows = records[name]
        if not isinstance(rows, list):
            invalid()
        count += len(rows)
        if count > MAX_ROWS:
            invalid()
        parsed[name] = {}
        for row in rows:
            if not isinstance(row, dict) or set(row) != set(fields):
                invalid()
            values = {field: scalar(model._meta.get_field(field), row[field]) for field in fields}
            if values["id"] in parsed[name]:
                invalid()
            parsed[name][values["id"]] = values
    for rows in parsed.values():
        for row in rows.values():
            for field, target in RELATIONS.items():
                if field in row and row[field] is not None and row[field] not in parsed[target]:
                    invalid()
    sums = defaultdict(lambda: Decimal("0"))
    for entry in parsed["entries"].values():
        if entry["delta"] == 0:
            invalid()
        sums[entry["balance_id"]] += entry["delta"]
    currencies = defaultdict(set)
    for balance in parsed["balances"].values():
        if (
            balance["amount"] < 0
            or balance["amount"] != sums[balance["id"]]
            or balance["currency"] in currencies[balance["account_id"]]
        ):
            invalid()
        if parsed["accounts"][balance["account_id"]]["archived"] and balance["amount"] != 0:
            invalid()
        currencies[balance["account_id"]].add(balance["currency"])
    if any(currencies[account_id] != {"IQD", "USD"} for account_id in parsed["accounts"]):
        invalid()
    months = {row["month"]: row["closed"] for row in parsed["months"].values()}
    if len(months) != len(parsed["months"]) or any(month.day != 1 for month in months):
        invalid()
    active = preferences["active_month"]
    if active is not None:
        if (
            active.day != 1
            or active not in months
            or months[active]
            or any(not closed for month, closed in months.items() if month != active)
        ):
            invalid()
        for kind in ("budget", "main_savings"):
            if (
                sum(
                    account["kind"] == kind and not account["archived"]
                    for account in parsed["accounts"].values()
                )
                != 1
            ):
                invalid()
    elif parsed["accounts"] or months or parsed["operations"] or parsed["debts"]:
        invalid()
    reversed_ids = set()
    movements = defaultdict(lambda: defaultdict(lambda: Decimal("0")))
    for entry in parsed["entries"].values():
        movements[entry["operation_id"]][entry["balance_id"]] += entry["delta"]
    for item in parsed["operations"].values():
        try:
            ZoneInfo(item["timezone"])
        except ZoneInfoNotFoundError, ValueError:
            invalid()
        if (
            item["kind"] not in OPERATION_KINDS
            or item["local_date"].replace(day=1) not in months
            or not isinstance(item["details"], dict)
            or len(json.dumps(item["details"])) > 8192
        ):
            invalid()
        # Reversals must point backwards and never form cycles or target the same item twice.
        original_id = item["reverses_id"]
        if original_id is not None:
            if (
                item["kind"] not in {"reversal", "debt_reversal"}
                or original_id >= item["id"]
                or original_id in reversed_ids
            ):
                invalid()
            reversed_ids.add(original_id)
            compensation = {
                balance_id: -amount for balance_id, amount in movements[original_id].items()
            }
            if dict(movements[item["id"]]) != compensation:
                invalid()
        elif item["kind"] in {"reversal", "debt_reversal"}:
            invalid()
    debt_totals = defaultdict(lambda: Decimal("0"))
    event_operations = set()
    for event in parsed["events"].values():
        if (
            event["remaining_delta"] == 0
            or event["operation_id"] in event_operations
            or not parsed["operations"][event["operation_id"]]["kind"].startswith("debt_")
        ):
            invalid()
        event_operations.add(event["operation_id"])
        debt_totals[event["debt_id"]] += event["remaining_delta"]
    for debt in parsed["debts"].values():
        if (
            not 0 <= debt["remaining_amount"] <= debt["original_amount"]
            or debt["original_amount"] <= 0
            or debt_totals[debt["id"]] != debt["remaining_amount"]
            or (debt["cancelled"] and debt["remaining_amount"] != 0)
        ):
            invalid()
        if parsed["people"][debt["person_id"]]["archived"] and debt["remaining_amount"] != 0:
            invalid()
    validate_operation_amounts(parsed)
    return preferences, parsed


def validate_operation_amounts(records: dict) -> None:
    from apps.finances.api.serializers import TRANSACTION_SERIALIZERS, MoneyField, RateField
    from apps.finances.services.core import exchange_values

    movements = defaultdict(dict)
    for entry in records["entries"].values():
        if entry["balance_id"] in movements[entry["operation_id"]]:
            invalid()
        movements[entry["operation_id"]][entry["balance_id"]] = entry["delta"]
    lookup = {
        (row["account_id"], row["currency"]): row["id"] for row in records["balances"].values()
    }
    events = {row["operation_id"]: row for row in records["events"].values()}
    for item in records["operations"].values():
        kind, details, entries = item["kind"], item["details"], movements[item["id"]]
        if kind in TRANSACTION_SERIALIZERS:
            raw = {
                key: value
                for key, value in details.items()
                if kind != "exchange" or key not in {"received", "received_currency"}
            }
            serializer = TRANSACTION_SERIALIZERS[kind](data=raw)
            serializer.is_valid(raise_exception=True)
            data = serializer.validated_data
            if data["local_date"] != item["local_date"] or data["timezone"] != item["timezone"]:
                invalid()
            try:
                if kind == "income":
                    budget = next(
                        row["id"] for row in records["accounts"].values() if row["kind"] == "budget"
                    )
                    expected = {lookup[(budget, data["currency"])]: data["amount"]}
                elif kind == "transfer":
                    if data["source_id"] == data["target_id"]:
                        invalid()
                    expected = {
                        lookup[(data["source_id"], data["currency"])]: -data["amount"],
                        lookup[(data["target_id"], data["currency"])]: data["amount"],
                    }
                elif kind == "exchange":
                    if "rate" not in data:
                        invalid()
                    _, received, target = exchange_values(Portfolio(), data)
                    if (
                        details.get("received") != format(received, ".2f")
                        or details.get("received_currency") != target
                    ):
                        invalid()
                    expected = {
                        lookup[(data["source_id"], data["currency"])]: -data["amount"],
                        lookup[(data["source_id"], target)]: received,
                    }
                else:
                    if (
                        item["category_id"] != data["category_id"]
                        or not entries
                        or sum(entries.values()) != -data["amount"]
                    ):
                        invalid()
                    source = records["accounts"][data["source_id"]]
                    for balance_id, delta in entries.items():
                        balance = records["balances"][balance_id]
                        account = records["accounts"][balance["account_id"]]
                        if (
                            delta >= 0
                            or balance["currency"] != data["currency"]
                            or (
                                account["id"] != source["id"]
                                and not (
                                    source["kind"] == "budget" and account["kind"] == "main_savings"
                                )
                            )
                        ):
                            invalid()
                    continue
                if entries != expected:
                    invalid()
            except KeyError, StopIteration:
                invalid()
        elif kind.startswith("debt_"):
            if item["id"] not in events:
                invalid()
            event = events[item["id"]]
            debt = records["debts"][event["debt_id"]]
            if details.get("debt_id") != debt["id"]:
                invalid()
            if kind == "debt_reversal":
                continue
            amount = MoneyField(min_value=Decimal("0.01")).run_validation(
                details.get("amount" if kind == "debt_create" else "settled_amount")
            )
            if kind == "debt_create":
                if (
                    event["remaining_delta"] != amount
                    or amount != debt["original_amount"]
                    or details.get("currency") != debt["currency"]
                ):
                    invalid()
                currency, outgoing = debt["currency"], debt["direction"] == "receivable"
            else:
                if (
                    -event["remaining_delta"] != amount
                    or details.get("debt_currency") != debt["currency"]
                ):
                    invalid()
                if kind == "debt_writeoff":
                    if (
                        entries
                        or not isinstance(details.get("reason"), str)
                        or not details["reason"].strip()
                    ):
                        invalid()
                    continue
                amount = MoneyField(min_value=Decimal("0.01")).run_validation(
                    details.get("payment_amount")
                )
                RateField().run_validation(details.get("effective_rate"))
                currency, outgoing = details.get("payment_currency"), debt["direction"] == "payable"
            if details.get("movement") == "record_only":
                if entries:
                    invalid()
            elif details.get("movement") == "account":
                if (
                    not entries
                    or sum(entries.values()) != amount * (-1 if outgoing else 1)
                    or any(
                        records["balances"][balance_id]["currency"] != currency
                        or (delta >= 0 if outgoing else delta <= 0)
                        for balance_id, delta in entries.items()
                    )
                ):
                    invalid()
            else:
                invalid()


def replace(portfolio: Portfolio, document: dict) -> None:
    preferences, records = validate(document)
    owner_id = portfolio.owner_id
    # Only called by the worker after an owned, current, explicitly confirmed preview.
    DebtEvent.objects.filter(debt__owner_id=owner_id).delete()
    Debt.objects.filter(owner_id=owner_id).delete()
    Person.objects.filter(owner_id=owner_id).delete()
    LedgerEntry.objects.filter(operation__owner_id=owner_id).delete()
    Operation.objects.filter(owner_id=owner_id).update(reverses=None)
    Operation.objects.filter(owner_id=owner_id).delete()
    Account.objects.filter(owner_id=owner_id).delete()
    Category.objects.filter(owner_id=owner_id).delete()
    BudgetMonth.objects.filter(owner_id=owner_id).delete()
    mapping = {name: {} for name in SPECS}
    # Dependencies precede their users; operation self-references and details are linked later.
    order = [
        "months",
        "accounts",
        "balances",
        "categories",
        "operations",
        "entries",
        "people",
        "debts",
        "events",
    ]
    for name in order:
        model, ownership, _ = SPECS[name]
        for old_id, row in sorted(records[name].items()):
            values = {
                field: value
                for field, value in row.items()
                if field not in {"id", "reverses_id", "created_at"}
            }
            for field, target in RELATIONS.items():
                if field in values and values[field] is not None:
                    values[field] = mapping[target][values[field]]
            if ownership == "owner_id":
                values["owner_id"] = owner_id
            instance = model.objects.create(**values)
            mapping[name][old_id] = instance.pk
            if "created_at" in row:
                model.objects.filter(pk=instance.pk).update(created_at=row["created_at"])
    for old_id, row in records["operations"].items():
        details = row["details"].copy()
        for field, target in {
            "source_id": "accounts",
            "target_id": "accounts",
            "category_id": "categories",
            "debt_id": "debts",
            "event_id": "events",
        }.items():
            if field in details:
                if details[field] not in mapping[target]:
                    invalid()
                details[field] = mapping[target][details[field]]
        Operation.objects.filter(pk=mapping["operations"][old_id]).update(
            reverses_id=mapping["operations"].get(row["reverses_id"]), details=details
        )
    for field, value in preferences.items():
        setattr(portfolio, field, value)
    portfolio.generation = uuid4()
    portfolio.revision += 1
    portfolio.save()
