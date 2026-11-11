# Penny backend

Django/DRF API for Penny: username/password sessions, optional authenticator MFA,
exact IQD/USD accounts and ledger, monthly rollover, debts, encrypted financial backups,
CSV export and confirmed restore. Python 3.14, Django 6.1 and PostgreSQL; exact dependency
versions are pinned in requirements.txt. No admin site is exposed.

## Documentation

- [Architecture and invariants](docs/architecture.md)
- [Authentication API and manual checks](docs/authentication.md)
- [Financial, debt and backup API](docs/finances.md)
- [Render Free deployment and hosted acceptance](docs/deployment.md)
- [Northflank Docker deployment](docs/northflank.md)
- [Deferred device-verification research](docs/device-verification.md)
- [Contributor instructions](AGENTS.md)

These backend instructions are self-contained. Application/domain code uses standard
Django/PostgreSQL APIs; hosting, database and mobile-build providers remain replaceable.

## Structure

| Path | Purpose |
| --- | --- |
| apps/accounts/ | User/session authentication, MFA, attempt budgets and auth maintenance |
| apps/finances/ | Accounts, ledger, operations, rollover, corrections and reports |
| apps/debts/ | People, debts, repayment/write-off and debt-event reversals |
| apps/backups/ | Financial snapshots, encrypted ZIP/CSV and confirmed restore jobs |
| config/ | Settings, routing, OpenAPI, WSGI/ASGI and shared-local test runner |
| scripts/development.py | Private local configuration, setup, tests, server and one-shot jobs |
| scripts/check_financial_api.py | Loopback-only real HTTP contract/financial acceptance |
| scripts/serve.py | Linux web/financial-worker supervisor |
| tests/ | Cross-cutting configuration/deployment/schema tests and shared financial test helpers |
| docs/ | Current contracts, architecture, deployment and deferred research |

## Windows setup

Run commands from the backend checkout root. Use the explicit virtual-environment Python;
activation is optional. This workspace already has its environment installed.

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python314\python.exe" -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Existing local configuration is in a gitignored `.env`. Never print, commit, overwrite or
regenerate its credentials/keys. Local dotenv loading preserves process overrides and literal
password text; production never loads this file. For a fresh checkout, configure persistent
PENNY_SECRET_KEY and PENNY_MFA_ENCRYPTION_KEY privately before using local commands.
The required settings are described below; `.env` creation/changes need explicit approval.

The existing local PostgreSQL database is `penny` on localhost:5432. Its role defaults to
postgres; set PENNY_DB_USER in the process environment if different. To save its existing
password through a hidden prompt, use the explicitly approved configuration command:

```powershell
.\.venv\Scripts\python.exe -m scripts.development configure
```

Other local commands prompt privately if no password is available and do not save it.

## Development and local tests

Development and PostgreSQL tests deliberately share the SAME local penny tables. Tests
clear development records before the suite and during transaction-test cleanup. Do not run
these tests while entering local data. The runner rejects remote hosts, other database
names/ports and parallel execution; it never creates or drops a database/schema/role.
Never point the test runner or development helpers at Neon or another deployed database.

```powershell
.\.venv\Scripts\python.exe -m scripts.development setup
.\.venv\Scripts\python.exe -m scripts.development test
.\.venv\Scripts\python.exe -m scripts.development runserver --port 8001
```

`setup` shows/applies LOCAL migrations. `runserver` binds 127.0.0.1 only; default port is
8000. `test --label apps.finances` runs focused tests; repeat --label for additional modules.

With the local server running, open http://127.0.0.1:8001/api/v1/docs/ or obtain the generated
OpenAPI JSON at `/api/v1/schema/?format=json`. Docs are local-only and use external Swagger
assets. `/api/v1/health/` is database-independent liveness; `/api/v1/ready/` checks PostgreSQL.
Both return `{"status":"ok"}` when healthy; `/` intentionally returns 404.

Use another terminal for a bounded queue batch or loopback HTTP acceptance:

```powershell
.\.venv\Scripts\python.exe -m scripts.development jobs
.\.venv\Scripts\python.exe -m scripts.check_financial_api --port 8001
```

The HTTP checker creates synthetic local accounts, checks responses against OpenAPI and
runs local jobs. Its records remain until the next approved local test cleanup. It cannot
be configured to target a remote host. Manual scenarios are in the two API guides.

Checks that do not connect to a database:

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe manage.py check --settings=config.settings.unit
.\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run --settings=config.settings.unit
.\.venv\Scripts\python.exe manage.py test apps.accounts.tests.test_unit tests --settings=config.settings.unit
.\.venv\Scripts\python.exe -m pip check
```

Unit settings use a dummy backend; these checks do not prove PostgreSQL behavior.

## Configuration

Local settings need PENNY_SECRET_KEY, PENNY_MFA_ENCRYPTION_KEY and PENNY_DB_NAME,
PENNY_DB_USER, PENNY_DB_PASSWORD, PENNY_DB_HOST. PENNY_DB_PORT defaults to 5432;
PENNY_DB_SSLMODE defaults to require. The local helper pins localhost:5432/penny and
uses SSL disable only for this local PostgreSQL connection. PENNY_LOCAL_DEBUG=1 is
local-only; debug output is otherwise off. Keys are persistent, never generated at startup.

Production WSGI/ASGI defaults to config.settings.production. Configure secrets in the
host's environment, explicit PENNY_ALLOWED_HOSTS and TLS to PostgreSQL. Render Free uses
`python -m scripts.serve`: one Gunicorn web server and one financial worker in the same
service. Both pause when the service sleeps. Startup/build do not apply migrations.
See the deployment guide for all variables, HTTPS trust, health checks and acceptance.

## Verification and remaining work

GitHub Actions runs **Backend CI / Backend checks** on pushes/pull requests to main and
manual dispatch. It checks pinned dependencies, Ruff lint/format, Django/migration drift,
the complete suite against an isolated PostgreSQL 17 service, module compilation and an
actual Linux Gunicorn HTTP liveness smoke check. CI uses disposable keys/credentials and
never connects to Neon. See [deployment enforcement](docs/deployment.md#ci-and-deployment-enforcement)
for required GitHub/Render dashboard settings; a workflow alone does not block merges
or deployment.

Before initial publication on 2026-11-11, the full guarded LOCAL PostgreSQL suite passed
**123 tests**, including two regressions for quiet Ctrl+C shutdown and unchanged failure
exit codes. Ruff lint/format, Django system/migration-drift and pip checks passed.
Cleanup preserved all application regression tests, removed one obsolete dependency probe,
and left migration hashes and the generated OpenAPI schema unchanged (54 paths).
The separate database-free suite previously passed 43 tests before the two shutdown tests
were added; the full 123-test run includes the current database-free coverage.

Earlier real HTTP acceptance passed 63 checks across 51 method/path combinations, plus
separate authentication/MFA checks; those HTTP scenarios were not rerun for cleanup.
The earlier PyPI version-feed snapshot reported no matching active advisory for 25 pins,
including Gunicorn 27.0.0. Dependencies were unchanged by cleanup; that snapshot is not
an exhaustive security assessment.

The new Neon database was explicitly initialized on 2026-11-10: **23 migrations applied,
none pending**. Render dashboard settings have been configured by the user. Actual Linux
Gunicorn startup, hosted HTTPS/authentication, queue processing, sleep/resume, resource use
and database recovery remain unverified. Local tests do not establish production readiness.

Other-device revocation stays disabled until secure server verification is approved and
proven. Financial implementation proceeded under an explicit sequencing exception; its
security requirement remains intact. Mobile financial integration and APK/device proof
remain pending. GitHub Actions configuration is prepared; hosted CI results and initial
publication must be verified separately from the local results above.
