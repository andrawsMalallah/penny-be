# Backend architecture and invariants

Penny is an Android-first personal-finance application backed by Django/DRF and PostgreSQL.
This document records the backend's implemented design and approved domain requirements.
Endpoint payloads are in [authentication](authentication.md) and [financial APIs](finances.md).
Hosting configuration belongs in [deployment](deployment.md), not business logic.

## Structure and ownership

The four domain apps are accounts, finances, debts and backups. Views validate explicit
serializers, delegate to typed services and return safe responses. List queries derive
ownership from authenticated identity, select the needed relationships and use cursor
pagination. No owner field supplied by the client authorizes access. No Django admin site
or direct balance-edit bypass is provided.

Shared financial write/read HTTP bases live in apps/finances/api/base.py. Shared operation
query loading belongs in apps/finances/queries.py, independent of endpoint classes.
Domain services own transactions; tests/financial_helpers.py supplies reusable financial
fixtures without importing another TestCase's discovered tests.

Use standard PostgreSQL transactions, locks, constraints and idempotency. Infrastructure
is configured through environment variables; no Render/Neon/provider SDK is a domain
requirement. Android support is generic across supported manufacturers, not model-specific.
Clients may read a protected offline cache but cannot perform offline financial writes or
authorize spending from cached balances. The server remains authoritative for every write.

## Authentication and secrets

Django provides database-backed sessions, CSRF, password validation/hashers and session-key
rotation. DRF session authentication additionally requires an owned, unexpired DeviceSession.
Fresh login uses username/password plus optional authenticator MFA. Sessions slide for
90 days only after successful authorized responses. Both Django and device expiry must be
valid; expiry-boundary access, CSRF failures and rejected requests cannot renew sessions.

Usernames are unique case-insensitively, immutable, 3-30 ASCII letters/numbers/underscores.
A database LOWER(username) uniqueness constraint decides concurrent registrations. Optional
display names are trimmed to 40 characters. Device names are labels, never security proof.
Passwords retain whitespace and are checked through Django's configured policy.

PostgreSQL account/IP attempt budgets are independent of Redis. Raw account/IP values
become keyed hashes before persistence. Lock sorted rate buckets in a separate committed
transaction so a failed password/MFA transaction does not erase the attempt. Then acquire
the user lock before device/session rows. Registration user/session creation is atomic;
password changes and sign-in share the user lock, preventing issuance with an old password.
Changing a password rotates/retains the current session and revokes the other sessions.

MFA uses a django-otp Device extension and maintained TOTP utilities. Fernet encrypts the
persistent secret; Django hashers store each recovery code. Codes are generated independently
through django-otp and shown once. Never retain plaintext codes or a reconstructible seed.
A previously evaluated stock headless recovery flow reconstructed codes from a seed, which
is why it was rejected rather than privately patched. Keep the maintained integration.

Enrollment is device-bound, password-protected and expires in five minutes. A valid TOTP
must confirm activation. Persist the accepted timestep to reject replay. Pending MFA login
is anonymous: a five-minute server challenge binds the cookie, user and password auth hash.
Only consumed valid TOTP/recovery proof can issue a full session. All MFA transitions use
the same user lock; proof consumption and session changes commit or roll back together.
The API guide specifies invalidation behavior for activation, regeneration and disable.

Local .env loading preserves process overrides and literal passwords. Production never
loads it. Django/Fernet keys are persistent and never generated on startup. Keep key
backups private; changing encryption keys must not silently strand authenticator secrets.
Missing or incorrect secrets cannot authorize MFA. Do not log secrets or exception details
into production responses. Use no custom authentication tokens, crypto or password hashes.

## Money and account allocation

Budget, Main Savings and named accounts each hold separate IQD/USD balances. Money is a
decimal string with two places, maximum 9,999,999,999,999,999.99. Rates are positive decimals
with at most six places, maximum 999,999,999,999.999999. Incoming binary floats are rejected.
ROUND_HALF_UP applies once to a received exchange amount; a zero received amount is invalid.
Transfers stay in one currency; exchanges stay within one account. Original rates, amounts
and signed ledger allocations are retained.

All financial mutations acquire the authenticated user's row lock inside one transaction
before touching portfolio/account/debt rows, recheck the requesting device session and
apply linked ledger entries with atomic balance updates. This deliberately favors simple
correctness for a personal-finance workload. Account balances reconcile with ledger sums.

Budget expenses, lending and outgoing debt repayment use Budget first, then same-currency
Main Savings only after exact allocation confirmation. Recompute consented allocations
under lock and reject stale/different splits. A boolean alone does not consent to a changed
savings deduction. Insufficient combined funds cause no partial change. Never automatically
deduct from named accounts or another currency. Directly selected account spending has no
fallback.

Every financial write requires a UUID Idempotency-Key. A canonical validated request
fingerprint binds the key to the user: exact replay returns the stored response, changed
input returns 409, failed transactions roll back both key and movements. Successful changes
advance portfolio revision for cross-device cache invalidation. Restore also changes an
internal generation so old keys cannot replay pre-restore operations or resource responses.

## Dates, corrections, reports and debt

Transaction dates are ISO Gregorian dates within the shared active month. Each operation
retains its supplied IANA timezone and authoritative server timestamp. Month starts use
the first day. Manual rollover advances only forward, closes the prior month, transfers
both Budget currency remainders to Main Savings once and posts positive salary amounts as
separate linked income operations atomically. Skipped months receive no invented salary.
Both currencies commit together; original rate/allocation history remains intact.

Corrections append compensating ledger entries and optionally a replacement atomically.
Original active-month amounts reverse exactly; insufficient funds roll back the complete
correction. Closed months remain read-only, with the approved exception that older debt
reversals post into the current active month without rewriting history. Dependent later
debt movements must be reversed first. Original operation links and correction history remain.

Debt direction/currency totals stay separate. Record-only creation, repayment and write-off
do not change account balances; moving-money debt events follow normal funding constraints.
Partial and explicit cross-currency repayments preserve both actual and settled amounts
and the effective historical rate. Debt movements never enter income/expense report totals.
Reports also exclude transfers, exchanges, rollover and opening movements.

## Backups and queued work

Snapshot/CSV generation and confirmed replacement run as queued jobs. Bounded password
ZIP encryption at download and decryption/validation at preview are explicitly approved,
so backup passwords never enter persisted jobs. Use pyzipper's standard AES-256 WinZip
AE-2 ZIP, not a custom cryptographic format. Escape spreadsheet-formula prefixes in CSV.

Snapshots whitelist the user's financial graph, excluding credentials, MFA, devices and
sessions. Validate archive structure, ownership/reference consistency, amounts, ledger
reconciliation and correction compensation. Restore requires a user-visible preview and
explicit replacement confirmation, bound to the user, requesting session, expiry and
portfolio revision. Worker execution rechecks those conditions under the user lock and
replaces only that user's financial graph atomically. Account credentials/MFA/sessions stay
unchanged. Interrupted restore jobs must not be automatically replayed. Exact caps and
expiry behavior are part of the financial API contract.

## Deferred server device verification

Other-device revocation is disabled until an approved, maintained verification protocol
and real native/server proof exist. Financial implementation was explicitly approved while
that work remains deferred; it does not relax authorization or verification requirements.
Local biometric/phone-lock unlocking protects app access only. A client success flag,
phone name/UUID or ordinary synced passkey does not prove current-phone verification.
The [device-verification assessment](device-verification.md) records the evaluated options,
unapproved candidates, acceptance rules and remaining real-device/verifier requirements.

## Verification strategy

Run ordinary behavior/ownership/privacy regressions alongside independent PostgreSQL
connection tests for locks, uniqueness, replay and atomic rollback. Observe actual locking
where relevant; timing sleeps alone are not concurrency proof. The explicitly approved
local runner shares/cleans localhost:5432/penny development tables and never creates/drops
a database. Production/deployed databases are never test targets. Unit mocks do not prove
PostgreSQL, Linux, hosted ingress or Android behavior. README records the current executed
checkpoint; API/deployment guides provide manual acceptance.

Framework references: [Django sessions](https://docs.djangoproject.com/en/6.1/topics/http/sessions/),
[Django authentication](https://docs.djangoproject.com/en/6.1/topics/auth/default/),
[DRF sessions](https://www.django-rest-framework.org/api-guide/authentication/#sessionauthentication),
[django-otp extensions](https://django-otp-official.readthedocs.io/en/stable/extend.html).
