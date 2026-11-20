# Penny backend

Django REST Framework/PostgreSQL private expense tracker API. Python 3.14, cookie sessions and Brevo SMTP.

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
