# Penny backend

Django REST Framework/PostgreSQL private expense tracker API. Python 3.14, cookie sessions and Brevo email via local SMTP or hosted HTTPS API.

Applications are under `app/finance` and `app/identity`, each with `api/`, `services/`, `tests/` and original migrations. Django labels and existing table/migration history remain unchanged.

Use a dedicated local PostgreSQL database and ignored `.env` based on `.env.example`. Review the target before any migration; never reset an existing database to follow setup instructions.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000 --noreload
.\.venv\Scripts\python.exe manage.py test --settings=config.test_settings
.\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run --settings=config.test_settings
.\.venv\Scripts\python.exe -m ruff check --no-cache .
.\.venv\Scripts\python.exe -m ruff format --no-cache --check .
```

Default isolated tests use SQLite and test-only settings; PostgreSQL concurrency checks are skipped there. They are supplemental to the previously executed full PostgreSQL suite. Runtime management commands remain under `app/identity/management/commands/`.

The backend production runner remains Gunicorn via `requirements-production.txt` and `gunicorn.conf.py`. Workspace context lives only in the parent AGENTS.md and MEMORY.md.

## Hosted deployment with Neon

Use the existing Dockerfile in the backend repository and keep its default Gunicorn start command; it does not run migrations. Expose its HTTP port `8000` and use `/api/v1/ready/` as the health check path. Set the backend's public HTTPS hostname in `PENNY_ALLOWED_HOSTS` without a scheme. On Northflank, set `PENNY_TRUST_NORTHFLANK_PROXY=1` only for the service behind Northflank's HTTPS ingress; the setting also requires Northflank's injected `NF_HOSTS`. On Render, use `PENNY_TRUST_RENDER_PROXY=1` instead. Leave `PENNY_LOCAL_HTTP` unset so secure cookies and HTTPS enforcement stay enabled. Verify a direct GET to the backend's `/api/v1/ready/` returns JSON `{"status":"ready"}` before connecting the frontend.

Use the dedicated Neon PostgreSQL database for this deployment. Set `PENNY_DB_HOST`, `PENNY_DB_PORT`, `PENNY_DB_NAME`, `PENNY_DB_USER`, `PENNY_DB_PASSWORD`, and `PENNY_DB_SSLMODE=require` in the backend platform's secret environment settings. Initial migrations were applied separately to the verified empty database; do not run migrations in the container's start command or reset the database. Do not put connection strings or passwords in source files, command text, logs, or chat.

Set a unique `PENNY_SECRET_KEY`, preserve a single `PENNY_2FA_ENCRYPTION_KEY` securely, and set `PENNY_EMAIL_TRANSPORT=brevo_api`, `PENNY_BREVO_API_KEY`, and a verified `PENNY_EMAIL_FROM` on the backend platform. The Brevo key is an API key, not an SMTP key. Local development may continue to use the existing SMTP settings. Set `PENNY_TRUSTED_ORIGINS` to the frontend's exact HTTPS origin. The frontend host must rewrite `/api/v1/` to this service on the same browser origin for session and CSRF behavior.

For the Render static frontend, add a `Rewrite` rule from `/api/*` to `https://BACKEND-HOST/api/*` above any catch-all route. A catch-all `/*` rule, if present, must also be a Rewrite to `/index.html`, never a Redirect. After publication, verify `/api/v1/ready/`, `/api/v1/auth/csrf/`, real verification email delivery, sign-in, and an authenticated financial write through the frontend origin. Schedule `purge_expired_codes` hourly only when a reviewed scheduler is available.
