"""Exercise real loopback HTTP endpoints using fresh synthetic accounts only."""

import argparse
import http.cookiejar
import json
import re
import secrets
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from jsonschema import Draft4Validator, FormatChecker


def nullable_schema(value):
    if isinstance(value, list):
        return [nullable_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: nullable_schema(item) for key, item in value.items() if key != "nullable"}
    if value.get("nullable"):
        if "type" in result:
            result["type"] = [result["type"], "null"]
        else:
            result = {"anyOf": [result, {"type": "null"}]}
        if "enum" in result:
            result["enum"].append(None)
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, url):
        return None


class Client:
    def __init__(self, port: int) -> None:
        self.origin = f"http://127.0.0.1:{port}"
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), NoRedirect()
        )
        self.csrf = ""
        self.checked: set[tuple[str, str]] = set()
        self.requests = 0
        self.contract = None

    def request(
        self,
        method: str,
        path: str,
        data: dict | None = None,
        *,
        expected: int = 200,
        raw: bytes | None = None,
        content_type: str = "application/json",
        key: str | None = None,
        json_response: bool = True,
        csrf: bool = True,
    ):
        headers = {"Accept": "application/json" if json_response else "*/*"}
        if method == "POST":
            headers["Content-Type"] = content_type
            if csrf:
                headers["X-CSRFToken"] = self.csrf
            headers["Idempotency-Key"] = key or str(uuid4())
        payload = (
            raw if raw is not None else (json.dumps(data).encode() if data is not None else None)
        )
        request = urllib.request.Request(
            self.origin + path, data=payload, headers=headers, method=method
        )
        try:
            response = self.opener.open(request, timeout=20)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            body = response.read(10 * 1024 * 1024)
            status = response.status
        self.requests += 1
        self.checked.add((method, path.split("?")[0]))
        if status != expected:
            raise AssertionError(
                f"{method} {path.split('?')[0]} expected {expected}, received {status}"
            )
        result = json.loads(body) if json_response and body else body
        if self.contract and json_response:
            self.validate_response(method, path.split("?")[0], status, result)
        if isinstance(result, dict) and "csrf_token" in result:
            self.csrf = result["csrf_token"]
        return result

    def validate_response(self, method: str, path: str, status: int, result) -> None:
        for candidate, operations in self.contract["paths"].items():
            pattern = "/".join(
                "[^/]+" if part.startswith("{") else re.escape(part)
                for part in candidate.split("/")
            )
            if not re.fullmatch(pattern, path) or method.lower() not in operations:
                continue
            response = operations[method.lower()]["responses"].get(str(status), {})
            body = response.get("content", {}).get("application/json", {}).get("schema")
            if body:
                schema = {"components": self.contract["components"], **body}
                try:
                    Draft4Validator(
                        nullable_schema(schema), format_checker=FormatChecker()
                    ).validate(result)
                except Exception:
                    raise AssertionError(
                        f"Response contract mismatch: {method} {candidate} ({status}); "
                        "response withheld."
                    ) from None
            return

    def register(self) -> tuple[str, str]:
        self.request("GET", "/api/v1/auth/csrf/")
        username = "ApiCheck" + uuid4().hex[:16]
        password = "Synthetic!42" + secrets.token_urlsafe(24)
        self.request(
            "POST",
            "/api/v1/auth/register/",
            {"username": username, "password": password},
            expected=201,
        )
        return username, password


def worker() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "scripts.development", "jobs"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode:
        raise AssertionError(
            "Local financial worker failed; inspect its safe local checks separately."
        )


def run(port: int) -> None:
    client = Client(port)
    _, password = client.register()
    today = datetime.now(ZoneInfo("Asia/Baghdad")).date()
    month = today.replace(day=1)
    dated = {"local_date": today.isoformat(), "timezone": "Asia/Baghdad"}

    def write(path: str, data: dict, **kwargs):
        return client.request("POST", "/api/v1/finances/" + path + "/", data, **kwargs)

    def debt_write(path: str, data: dict, **kwargs):
        return client.request("POST", "/api/v1/debts/" + path + "/", data, **kwargs)

    client.request("GET", "/api/v1/health/")
    client.contract = client.request("GET", "/api/v1/schema/?format=json")
    client.request("GET", "/api/v1/docs/", json_response=False)
    write(
        "setup",
        {
            "month": month.isoformat(),
            "timezone": dated["timezone"],
            "budget": {"IQD": "1000.00", "USD": "100.00"},
            "main_savings": {"IQD": "500.00", "USD": "50.00"},
        },
    )
    accounts = client.request("GET", "/api/v1/finances/accounts/")["results"]
    budget = next(row["id"] for row in accounts if row["kind"] == "budget")
    named = write(
        "accounts/create",
        {**dated, "name": "Synthetic savings", "opening": {"IQD": "0.00", "USD": "0.00"}},
    )["account_id"]
    write(f"accounts/{named}/change", {"name": "Updated synthetic savings"})
    category = write("categories/create", {"name_en": "Synthetic", "name_ar": "تجريبي"})[
        "category_id"
    ]
    write(f"categories/{category}/change", {"name_en": "Updated synthetic"})
    write("preferences", {"exchange_rate": "1500.000000", "language": "ar", "theme": "dark"})
    client.request("GET", "/api/v1/finances/preferences/")
    client.request("GET", "/api/v1/finances/categories/")
    expense = {
        **dated,
        "amount": "10.00",
        "currency": "IQD",
        "source_id": budget,
        "category_id": category,
    }
    key = str(uuid4())
    created = write("expenses", expense, key=key)
    assert write("expenses", expense, key=key) == created
    write("expenses", {**expense, "amount": "11.00"}, key=key, expected=409)
    write("expenses", {**expense, "amount": 1.23}, expected=400)
    write("income", {**dated, "amount": "1.00", "currency": "IQD"}, csrf=False, expected=403)
    write("expenses", {**expense, "amount": "2000.00"}, expected=409)
    preview = write(
        "spending-preview", {**dated, "source_id": budget, "currency": "IQD", "amount": "1100.00"}
    )
    write(
        "expenses",
        {
            **expense,
            "amount": "1100.00",
            "allocations": [
                {"account_id": item["account_id"], "amount": item["amount"]}
                for item in preview["allocations"]
            ],
        },
    )
    write(f"activity/{created['operation_id']}/reverse", dated)
    write("income", {**dated, "amount": "100.00", "currency": "IQD"})
    write(
        "transfers",
        {**dated, "source_id": budget, "target_id": named, "currency": "IQD", "amount": "10.00"},
    )
    exchange = {
        **dated,
        "source_id": budget,
        "currency": "USD",
        "amount": "1.00",
        "rate": "1500.000000",
    }
    quote = write("exchange-preview", exchange)
    write("exchanges", {**exchange, "expected_revision": quote["revision"]})
    person = debt_write("people/create", {"name": "Synthetic Person"})["person_id"]
    debt_write(f"people/{person}/change", {"phone": "synthetic"})
    debt = debt_write(
        "records/create",
        {
            **dated,
            "person_id": person,
            "direction": "receivable",
            "currency": "IQD",
            "amount": "20.00",
            "movement": "record_only",
        },
    )
    repayment = debt_write(
        f"records/{debt['debt_id']}/repay",
        {
            **dated,
            "settled_amount": "5.00",
            "payment_amount": "5.00",
            "payment_currency": "IQD",
            "movement": "record_only",
        },
    )
    debt_write(f"events/{repayment['event_id']}/reverse", dated)
    debt_write(
        f"records/{debt['debt_id']}/write-off",
        {**dated, "settled_amount": "20.00", "reason": "Synthetic forgiveness"},
    )
    for path in [
        "people/",
        f"people/{person}/debts/",
        "records/",
        f"records/{debt['debt_id']}/",
        f"records/{debt['debt_id']}/events/",
        "summary/",
    ]:
        client.request("GET", "/api/v1/debts/" + path)
    for path in ["activity/", f"activity/{created['operation_id']}/", "reports/", "months/"]:
        client.request("GET", "/api/v1/finances/" + path)
    other = Client(port)
    other.register()
    other.request("GET", f"/api/v1/finances/activity/{created['operation_id']}/", expected=404)
    other.request("GET", f"/api/v1/debts/records/{debt['debt_id']}/", expected=404)
    next_month = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
    rollover = {
        "month": next_month.isoformat(),
        "timezone": dated["timezone"],
        "salary": {"IQD": "100.00", "USD": "0.00"},
    }
    quote = write("rollover-preview", rollover)
    write("rollover", {**rollover, "expected_revision": quote["revision"]})
    backup_password = "Synthetic backup " + secrets.token_urlsafe(24)
    backup_job = client.request("POST", "/api/v1/backups/jobs/", {"kind": "backup"})["job_id"]
    csv_job = client.request("POST", "/api/v1/backups/jobs/", {"kind": "csv"})["job_id"]
    worker()
    assert client.request("GET", f"/api/v1/backups/jobs/{backup_job}/")["state"] == "ready"
    archive = client.request(
        "POST",
        f"/api/v1/backups/jobs/{backup_job}/backup/",
        {"password": backup_password, "password_confirmation": backup_password},
        json_response=False,
    )
    client.request("GET", f"/api/v1/backups/jobs/{csv_job}/csv/", json_response=False)
    boundary = "Penny" + uuid4().hex
    body = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="password"\r\n\r\n'
            f"{backup_password}\r\n--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="penny-check.zip"\r\n'
            "Content-Type: application/zip\r\n\r\n"
        ).encode()
        + archive
        + f"\r\n--{boundary}--\r\n".encode()
    )
    preview = client.request(
        "POST",
        "/api/v1/backups/restore/preview/",
        raw=body,
        content_type="multipart/form-data; boundary=" + boundary,
    )
    restored = client.request(
        "POST",
        "/api/v1/backups/restore/confirm/",
        {"preview_id": preview["preview_id"], "confirm_replace": True},
    )["job_id"]
    worker()
    assert client.request("GET", f"/api/v1/backups/jobs/{restored}/")["state"] == "ready"
    client.request("GET", "/api/v1/auth/me/")
    client.request("GET", "/api/v1/auth/devices/")
    client.request("GET", "/api/v1/auth/mfa/")
    client.request(
        "POST",
        "/api/v1/auth/password/",
        {"current_password": password, "new_password": "Changed!42" + secrets.token_urlsafe(24)},
    )
    client.request("POST", "/api/v1/auth/logout/", {}, expected=204)
    client.request("GET", "/api/v1/finances/accounts/", expected=403)
    print(
        f"Passed {client.requests + other.requests} real HTTP checks across "
        f"{len(client.checked | other.checked)} method/path combinations. "
        "Synthetic local accounts retained; no secrets saved."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8001)
    arguments = parser.parse_args()
    if not 1 <= arguments.port <= 65535:
        parser.error("Use a valid loopback port.")
    run(arguments.port)


if __name__ == "__main__":
    main()
