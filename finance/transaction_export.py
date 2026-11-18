"""Private, current Activity exports; no persisted export jobs or financial caches."""

import csv
from io import StringIO
from tempfile import SpooledTemporaryFile
from typing import BinaryIO
from unicodedata import category

from django.db import transaction
from rest_framework.exceptions import APIException

from identity.models import User

from .ledger import transactions_for
from .models import MoneyTransaction

HEADERS = {
    "en": [
        "Date",
        "Type",
        "Account",
        "Destination account",
        "Expense category",
        "Currency",
        "Amount",
        "Exchange rate (1 USD = X IQD)",
        "Received currency",
        "Received amount",
        "Person",
        "Debt movement",
        "Note",
    ],
    "ar": [
        "التاريخ",
        "النوع",
        "الحساب",
        "الحساب المستلم",
        "فئة المصروف",
        "العملة",
        "المبلغ",
        "سعر الصرف (1 USD = X IQD)",
        "العملة المستلمة",
        "المبلغ المستلم",
        "الشخص",
        "حركة الدين",
        "الملاحظة",
    ],
}
LABELS = {
    "en": {
        "income": "Income",
        "expense": "Expense",
        "transfer": "Transfer",
        "exchange": "Exchange",
        "debt_in": "Debt money received",
        "debt_out": "Debt money paid",
        "principal": "Principal",
        "repayment": "Repayment",
    },
    "ar": {
        "income": "إيراد",
        "expense": "مصروف",
        "transfer": "تحويل",
        "exchange": "صرف عملة",
        "debt_in": "أموال دين مستلمة",
        "debt_out": "أموال دين مدفوعة",
        "principal": "أصل الدين",
        "repayment": "سداد",
    },
}


class ExportCountChanged(APIException):
    status_code = 409
    default_detail = "Activity count changed. Review the export count again."


def safe_text(value: str) -> str:
    # Quoting CSV fields alone does not stop spreadsheet formula execution. Inspect past
    # whitespace/format controls too, while retaining the original text in the output.
    probe = value
    while probe and (probe[0].isspace() or category(probe[0])[0] in {"C", "Z"}):
        probe = probe[1:]
    if probe.startswith(tuple("=+-@＝＋－＠")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


def export_row(entry: MoneyTransaction, language: str) -> list[str]:
    labels = LABELS[language]
    expense_category = ""
    if entry.category_id:
        expense_category = (
            getattr(entry.category, f"name_{language}")
            or entry.category.name_en
            or entry.category.name_ar
        )
    return [
        entry.date.isoformat(),
        labels[entry.kind],
        safe_text(entry.account.name),
        safe_text(entry.destination.name) if entry.destination_id else "",
        safe_text(expense_category),
        entry.currency,
        format(entry.amount, ".2f"),
        format(entry.exchange_rate, "f") if entry.exchange_rate is not None else "",
        ("USD" if entry.currency == "IQD" else "IQD") if entry.kind == "exchange" else "",
        format(entry.received_amount, ".2f") if entry.received_amount is not None else "",
        safe_text(entry.debt.person.name) if entry.debt_id else "",
        labels.get(entry.debt_role, ""),
        safe_text(entry.note),
    ]


@transaction.atomic
def export_transactions(user: User, values: dict) -> BinaryIO:
    # All finance mutations use this lock. Finish the snapshot before sending any bytes,
    # so slow downloads cannot hold the lock and failures cannot produce a partial CSV.
    User.objects.select_for_update().get(pk=user.pk)
    queryset = transactions_for(user, values)
    if queryset.count() != values["expected_count"]:
        raise ExportCountChanged()
    document = SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
    buffer = StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")

    def write_row(row: list[str]) -> None:
        buffer.seek(0)
        buffer.truncate()
        writer.writerow(row)
        document.write(buffer.getvalue().encode("utf-8"))

    try:
        document.write(b"\xef\xbb\xbf")
        write_row(HEADERS[values["language"]])
        rows = (
            queryset.select_related(None)
            .select_related("account", "destination", "category", "debt__person")
            .only(
                "date",
                "kind",
                "currency",
                "amount",
                "exchange_rate",
                "received_amount",
                "note",
                "debt_role",
                "account__name",
                "destination__name",
                "category__name_en",
                "category__name_ar",
                "debt__person__name",
            )
        )
        for entry in rows.iterator(chunk_size=500):
            write_row(export_row(entry, values["language"]))
        document.seek(0)
        return document
    except BaseException:
        document.close()
        raise
    finally:
        buffer.close()
