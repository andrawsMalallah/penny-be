# Financial, debt and backup API

The backend now implements the agreed financial features. Core authentication/MFA uses
[authentication API](authentication.md). Other-device revocation stays disabled by explicit user choice
while its verification requirement remains pending. Mobile integration and hosted
deployment are separate work; this document describes the local backend contract.

## Local use and checks

From backend/ in PowerShell:

```powershell
.\.venv\Scripts\python.exe -m scripts.development runserver --port 8001
```

Open **http://127.0.0.1:8001/api/v1/docs/** on this Windows computer for interactive
documentation, or /api/v1/schema/?format=json for the generated OpenAPI contract.
Documentation is enabled only by local settings and denied by default/production settings.
The Swagger viewer uses its package's external UI assets, so viewing it needs internet.

All resource endpoints require a fully authenticated device session. First GET
/api/v1/auth/csrf/; retain its cookie and csrf_token. Register/sign in through the auth
endpoints and use the newly returned token as X-CSRFToken on POSTs. Keep passwords,
cookies, MFA/recovery codes and backup passwords out of shared screenshots/logs.

The [backend README](../README.md#development-and-local-tests) owns setup, lint/test
commands, focused test labels and the loopback-only HTTP checker. That checker validates
responses against OpenAPI and runs queued jobs using synthetic accounts. Its records remain
until approved local test cleanup; do not run local tests while entering development data.

## Shared write contract

- Send a fresh UUID **Idempotency-Key** for each intended write. Exact retries with the
  same user/key/validated input return the original result. Changed input returns 409.
  Financial setup, preferences, metadata, debt and queued-job requests follow this rule.
  Read-only preview endpoints and password-based backup downloads do not require a key.
- All writes derive ownership from authentication. Owner IDs and unknown fields are
  rejected. Every financial write rechecks the active session under the user-row lock.
- Successful writes return a monotonically increasing **revision**. Refresh preferences
  to detect cross-device changes; invalidate financial cache when its revision changes.
  An optional expected_revision on dated transactions/rollover rejects stale previews.
  Restore advances revision and changes an internal generation: pre-restore idempotency
  keys cannot replay old resource responses or repeat old writes into restored data.
- Amounts are decimal STRINGS with up to two places, maximum 9999999999999999.99.
  Incoming JSON numbers/floats are rejected. Currency is IQD or USD. Rates are positive
  decimal strings with up to six places and maximum 999999999999.999999.
- Dated operations require local_date (YYYY-MM-DD) and an IANA timezone such as
  Asia/Baghdad. Dates must fall in the shared active month. Server created_at is separate.
  Dates/timezones are preserved when viewed from another device.
- Responses are no-store. Validation returns 400, denied authentication/CSRF 403,
  missing owned resources 404, and state/idempotency/funding conflicts 409.
- Requests never partially update balances. PostgreSQL transactions serialize on the
  user row before any financial/device-specific changes. Ledger entries store exact
  signed allocations; account balance equals their sum for that currency.

## Finances: /api/v1/finances/

| Method | Path | Purpose |
| --- | --- | --- |
| POST | setup/ | Once: active month, Budget/Main Savings opening amounts and starter categories |
| GET, POST | preferences/ | Read/update rate, language and theme; read active month/revision |
| GET | accounts/ | Paginated accounts with separate IQD/USD balances |
| POST | accounts/create/ | Named account, opening balances and dated audit operation |
| POST | accounts/{id}/change/ | Rename/archive/restore named account; archive requires both balances zero |
| GET | categories/ | Paginated bilingual categories, including historical archived entries |
| POST | categories/create/ | Create bilingual category |
| POST | categories/{id}/change/ | Rename/archive/restore; historical references remain |
| GET | months/ | Paginated month history and closed flags |
| POST | spending-preview/ | Read-only exact Budget/Main Savings allocation or shortfall |
| POST | exchange-preview/ | Read-only deducted/received amounts and before/after balances |
| POST | rollover-preview/ | Read-only remainder transfer and next budget in BOTH currencies |
| POST | expenses/ | Post expense with category and source |
| POST | income/ | Additional income into Budget |
| POST | transfers/ | Same-currency selected-account transfer |
| POST | exchanges/ | Exchange within one account, with stored original rate/amounts |
| POST | rollover/ | Close prior month, move both remainders, open later month and post salary |
| GET | activity/ | Cursor-paginated operations with stored allocations and audit links |
| GET | activity/{id}/ | Owned operation detail |
| POST | activity/{id}/reverse/ | Reverse or atomically reverse-and-replace an active-month operation |
| GET | reports/ | Separate IQD/USD income and expense totals, including compensation entries |

Lists use results/next/previous and cursor. Accounts/categories accept archived=true/false;
omit it to include history. Activity/reports accept start, end, currency, kind, account_id
and category_id. They retain decimal strings and stable ordering. A report's totals use
one SQL aggregate snapshot, including category-linked expense reversals.

Setup payload (first day for month):

```json
{
  "month": "2026-11-01",
  "timezone": "Asia/Baghdad",
  "budget": {"IQD": "1000.00", "USD": "100.00"},
  "main_savings": {"IQD": "500.00", "USD": "50.00"}
}
```

Expense fields are local_date, timezone, optional note, source_id, currency, amount and
category_id. Income uses dated fields/currency/amount. Transfers add source_id/target_id.
Exchange uses source_id/currency/amount and optional rate. Pass the preview's exact rate
and revision to prevent a later settings change from changing a confirmed exchange.

A Budget shortfall returns code=split_confirmation_required and exact allocations.
Confirm by resubmitting the intended expense with allocations containing account_id and
amount for EACH returned row, in order. Do not send the response currency inside a row.
The write recalculates under lock and rejects a stale/different split. Directly selected
savings/named accounts never fall back. Insufficient combined money changes nothing.

Rollover input is month/timezone/salary with both IQD/USD values (zero allowed). Month
must advance; skipped months get no salary. Remainder transfers are not income. Positive
salary amounts are separate income operations, atomically linked to the rollover's
response via salary_operation_ids. Both currencies commit together.

Reversal input uses current local_date/timezone/note. Optional replacement is
{"kind":"expense","data":{...complete new expense...}} (same original kind). All stored
allocations reverse exactly; insufficient reversal/replacement funds roll everything back.
Original expense/income/transfer/exchange records in closed months remain read-only.

## Debts: /api/v1/debts/

| Method | Path | Purpose |
| --- | --- | --- |
| GET, POST | people/ (GET), people/create/ (POST) | Paginated people / create name, optional phone/note |
| POST | people/{id}/change/ | Edit or archive/restore; archive requires every debt settled/written off |
| GET | people/{id}/debts/ | Owned person's paginated debt records |
| GET | records/ | All owned debts with direction/currency/status/person_id filters |
| POST | records/create/ | Create receivable/payable debt, record-only or account movement |
| GET | records/{id}/ | Original/remaining amounts, notes, due date and cancellation state |
| GET | records/{id}/events/ | Paginated event/repayment history with original financial operations |
| POST | records/{id}/repay/ | Partial/full repayment, including explicit cross-currency amounts |
| POST | records/{id}/write-off/ | Partial/full forgiveness with reason; no balance movement |
| POST | events/{id}/reverse/ | Reverse or atomically reverse-and-replace a debt event |
| GET | summary/ | Separate receivable/payable IQD/USD totals; no offsetting |

People include balances grouped by direction/currency in one aggregate query. Duplicate
names are allowed. Debt status filters are active, settled, cancelled or all. Optional
due_date is stored; reminder permission/scheduling remains device-local mobile work.

Creation requires dated fields, person_id, direction (receivable=owed to me,
payable=I owe), currency, amount and movement (record_only/account). Account movement also
requires account_id. Lending/outgoing repayment from Budget uses the same exact split
confirmation. Record-only cannot carry account_id/allocations and never changes balances.

Repayment records settled_amount in the DEBT currency and payment_amount/payment_currency
for the actual payment, plus movement/account when needed. Same-currency amounts must
match. Cross-currency actual amounts and the six-place effective rate are retained.
Write-off uses settled_amount/reason, with no money movement.

Debt events never enter income/expense totals. An older debt reversal posts on a supplied
ACTIVE-month date and leaves the closed history intact. Reversing debt creation first
requires reversing dependent events; a conflict identifies up to 50 blockers and a more
flag. Reversal of a received repayment requires its original account funds. Replacement
uses the original event kind (debt_create/debt_repayment/debt_writeoff) and complete data.

## Backups: /api/v1/backups/

| Method | Path | Purpose |
| --- | --- | --- |
| POST | jobs/ | Queue kind=backup or csv |
| GET | jobs/{uuid}/ | Owned state, failure code, expiry and snapshot revision |
| POST | jobs/{uuid}/backup/ | Ready backup download with password/password_confirmation |
| GET | jobs/{uuid}/csv/ | Ready readable CSV download |
| POST | restore/preview/ | Multipart encrypted file/password; validate and preview replacement |
| POST | restore/confirm/ | preview_id plus confirm_replace=true queues atomic replacement |

Run the worker locally in another terminal after queueing:

```powershell
.\.venv\Scripts\python.exe -m scripts.development jobs
```

This processes a bounded batch of up to 20 jobs. The continuous command is
`python manage.py process_financial_jobs --watch`. The prepared Render Free launcher
runs it alongside Gunicorn in the same service; both pause while the service sleeps.
See [deployment](deployment.md) for lifecycle and acceptance. HTTP status reads do not
execute jobs. States are queued/running/ready/failed.

Snapshot/CSV generation and database replacement run in the worker. Bounded ZIP encryption
at download and decryption/validation at preview were explicitly approved. Backup passwords
are 12–1024 characters for export, confirmed each time, and never saved. Forgotten backup
passwords cannot be recovered. Password requests are marked sensitive for exception reports.

The archive is standard AES-256 WinZip AE-2 ZIP through pyzipper, containing only
financial-data.json. The version-1 financial document whitelists preferences, months,
accounts, currency balances, categories, operations/allocations, people, debts and events.
It excludes owners' credentials, sessions, MFA secrets and device keys. Never extract
arbitrary archive paths. Validate file count/name/encryption/authentication, types, bounds,
owned references, ledger reconciliation, reversal compensation and transaction amounts.

Prototype limits: 8 MiB uploaded archive, 4 MiB financial document/CSV, 20,000 aggregate
snapshot rows, 2 outstanding and 10 unexpired jobs per user, 5 unexpired restore previews.
Jobs/results expire in one hour; previews in ten minutes. Worker maintenance clears expired
payloads and marks interrupted jobs failed; it never automatically replays a restore.
CSV cells beginning with spreadsheet formula prefixes are escaped.

Preview returns imported/current counts and a replacement warning; it changes no financial
records. Explicit confirmation is one-use, user-bound, expiring and revision-bound. Worker
execution rechecks revision and the requesting device session under the user lock, then
replaces only that user's financial graph atomically. Mid-import failure rolls back all
replacement. Account passwords, MFA and sessions remain unchanged. Old financial responses
are invalidated through portfolio generation/revision; clients must refresh their caches.

## Manual acceptance on this Windows computer

1. Start port 8001 and open /api/v1/docs/. Register a throwaway account, retain cookies and
   refresh the CSRF header after authentication changes. Set opening balances for a month.
2. Preview and post an expense exceeding Budget but within Budget+Main Savings. Confirm
   exact allocations. Try insufficient funds, a stale preview and an exact idempotent retry.
3. Transfer, exchange with a rate override, reverse/edit an expense, then preview/perform
   rollover with zero salary in one currency. Check history, reports and closed-month denial.
4. Create record-only and moving-money debts, record partial/cross-currency repayments and
   a write-off. Check separate direction totals and reversal dependency errors.
5. Queue a backup, run jobs, download privately with a confirmed backup password. Upload
   for preview, review counts and explicitly confirm replacement. Run jobs, check status,
   unchanged sign-in and restored financial data. Repeat with wrong password/corrupt input.
6. Sign in as another throwaway user and verify access to the first user's resource IDs is
   denied. Run the full regression suite before relying on changes.

The new Neon schema is initialized and the user has configured Render dashboard settings.
Hosted web/worker, proxy behavior and recovery still need acceptance; see
[deployment](deployment.md). Mobile financial integration and real Android proof remain
separate work. Local acceptance does not establish production readiness.
