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

## Render deployment with Neon

Use the existing Dockerfile in the backend repository as a Render Web Service on the `main` branch. Set the service language to Docker, `PORT` to `8000`, and the health check path to `/api/v1/ready/`. Keep the Dockerfile's default start command; it does not run migrations. Use the Render HTTPS hostname in `PENNY_ALLOWED_HOSTS` without a scheme. Set `PENNY_TRUST_RENDER_PROXY=1` only for this service behind Render's ingress, and leave `PENNY_LOCAL_HTTP` unset so secure cookies and HTTPS enforcement stay enabled.

Create a separate, empty Neon PostgreSQL database for this deployment. Set `PENNY_DB_HOST`, `PENNY_DB_PORT`, `PENNY_DB_NAME`, `PENNY_DB_USER`, `PENNY_DB_PASSWORD`, and `PENNY_DB_SSLMODE=require` in Render's secret environment settings. Use the direct Neon connection for the one-time migration after verifying its database identity. Do not put connection strings or passwords in source files, command text, logs, or chat. Never point the migration at an existing database without a separate review.

Set a unique `PENNY_SECRET_KEY`, preserve a single `PENNY_2FA_ENCRYPTION_KEY` securely, and set `PENNY_EMAIL_TRANSPORT=brevo_api`, `PENNY_BREVO_API_KEY`, and a verified `PENNY_EMAIL_FROM` in Render. The Brevo key is an API key, not an SMTP key. Local development may continue to use the existing SMTP settings. Add the frontend's exact HTTPS origin to `PENNY_TRUSTED_ORIGINS` when it is known. The frontend host must forward `/api/v1/` to this service on the same browser origin for session and CSRF behavior.

On a free Render web service, database migration must be a separate, reviewed action against the new Neon database; free services do not provide a pre-deploy command or shell access. After publication and migration, verify `/api/v1/ready/`, `/api/v1/auth/csrf/`, real verification email delivery, sign-in, and an authenticated financial write through the frontend proxy. Schedule `purge_expired_codes` hourly only when a reviewed scheduler is available.
