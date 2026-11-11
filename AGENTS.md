# Backend working instructions

Read README.md, docs/architecture.md and the relevant API/deployment guide before work.
These files contain the backend's requirements and commands without relying on a parent
checkout. The user's global instructions continue to apply.

## Scope and safety

- Keep code portable: standard Django/DRF/PostgreSQL, environment-configured infrastructure,
  no provider SDK or domain assumptions about Render/Neon/build providers.
- No Git writes, packages, .env edits, CI, or deployed-database changes without the required
  explicit approval. Propose CI steps/triggers before writing it. Never log credentials,
  cookies, MFA/recovery secrets or financial datasets. Keep configured persistent keys.
- No destructive filesystem/database actions without confirmation. Preserve migration
  files: the new Neon schema is initialized, so existing migration history is deployed.
- Use the simplest maintainable solution and existing patterns. Keep views thin, inputs
  strict, ownership explicit, services typed and money exact. No financial admin bypass.
- Keep documentation current rather than appending milestone histories and file inventories.
  README owns setup/current verification; API docs own contracts; architecture owns invariants;
  deployment owns operations. Deferred proposals must be clearly distinguished from approval.

## Data and authentication invariants

- Derive ownership from authenticated identity. Lock the user before financial/device rows;
  rate buckets lock in sorted scope order in a separate transaction. Never issue sessions
  or perform MFA transitions outside the user-locked service path.
- All financial writes recheck the requesting session under lock and use exact Decimal
  ledger allocations, UUID idempotency and portfolio revision. Restore advances generation
  so old write keys cannot replay pre-restore responses or operations.
- Keep two-decimal IQD/USD separate. Preserve allocations/rates/history and exact savings
  confirmation. No cached-balance spending authorization or automatic named-account/
  cross-currency fallback. Closed-month corrections follow docs/architecture.md.
- Use Django sessions/hashers, django-otp and cryptography; never invent cryptographic
  formats. MFA secrets are encrypted, recovery codes hash-only and pending MFA anonymous.
  Never call Device.verify_token outside the user-locked replay/consumption transition.
- Other-device revocation stays disabled pending approved, proven server verification.
  Financial work was explicitly approved despite that sequencing gate; a local biometric
  callback never substitutes for server proof. See docs/device-verification.md.
- Keep backup limits, expiry, explicit replacement preview/confirmation and requesting-
  session/revision binding. Never persist backup passwords or automatically replay restore.

## Runtime and tests

- Dockerfile runs the existing web/worker supervisor as UID 10001. Build from the backend
  root; .dockerignore allows only runtime sources. Northflank deployment and CMD health
  probes are documented in docs/northflank.md. Container build/start never apply migrations.

- GitHub Actions uses an isolated PostgreSQL 17 service on localhost:5432/penny with
  disposable CI-only credentials/keys. The local guarded runner is reused unchanged;
  no production secrets or Neon connection are available to CI. Backend checks must
  pass before merging/deploying once the user enables the documented dashboard rules.

- Use the explicit .venv Python and README commands. Settings: local for development,
  production for deployment, unit for database-free checks, test for shared local PostgreSQL.
- Development/tests share local penny on localhost:5432. Test cleanup may clear development
  records, as approved. No separate databases/schemas/roles, remote test target or parallel
  test execution; never create/drop penny. Do not test against Neon/deployed data.
- Add meaningful regressions and execute affected checks. PostgreSQL tests prove real
  constraints/concurrency; unit mocks do not. Report local, Linux, hosted and Android proof
  separately. Keep security/financial/concurrency regressions and required package files.
- Shared financial HTTP bases live in apps/finances/api/base.py; operation loading lives in
  apps/finances/queries.py; shared test fixtures/helpers in tests/financial_helpers.py.
  Reuse these rather than importing concrete endpoint or TestCase classes across domains.
- Render Free starts `python -m scripts.serve`: one web server and one financial worker
  share a sleeping free service. No paid worker/cron, keep-alive workaround or automatic
  production migration. Proxy HTTPS trust is explicit and requires a controlled ingress.
- Python 3.14/Django 6.1 pins are in requirements.txt. Keep approved django-otp,
  cryptography, pyzipper and drf-spectacular choices. Docs/schema remain denied in production.
- Local dotenv loading preserves process overrides/literal password text. Production never
  reads local .env or regenerates Django/Fernet keys. Keep secret configuration private.

Finish implementation tasks with changed files, executed checks and precise manual steps.
Keep this file updated when conventions change; update a session log only when authorized.
