# Penny API

Django REST Framework API for Penny's web expense tracker. Current features are registration, email-code verification, email/password sign-in, session logout, and bounded expired-code maintenance. Password reset, optional authenticator 2FA, account setup, and financial features are planned separately.

## Development setup

Requires Python 3.14 and PostgreSQL. From this repository:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Fill in `.env` locally. It is ignored by Git. Process environment variables take precedence over the file.

| Variable | Purpose |
| --- | --- |
| `PENNY_SECRET_KEY` | Unique, stable Django secret |
| `PENNY_LOCAL_HTTP` | `1` for local HTTP; omit or set `0` for HTTPS deployments |
| `PENNY_ALLOWED_HOSTS` | Comma-separated allowed hostnames/IPs |
| `PENNY_TRUSTED_ORIGINS` | Comma-separated trusted browser origins |
| `PENNY_DB_HOST`, `PENNY_DB_PORT` | PostgreSQL endpoint |
| `PENNY_DB_NAME`, `PENNY_DB_USER`, `PENNY_DB_PASSWORD` | Development database credentials |
| `PENNY_DB_SSLMODE` | PostgreSQL TLS policy |
| `PENNY_EMAIL_HOST` | Brevo SMTP relay host |
| `PENNY_EMAIL_PORT` | Required SMTP STARTTLS port, normally 587 or 2525; no code fallback |
| `PENNY_EMAIL_USER` | Brevo SMTP login |
| `PENNY_EMAIL_PASSWORD` | SMTP key, not API key/account password |
| `PENNY_EMAIL_FROM` | Exact sender address verified in Brevo |

Create a dedicated development database using your PostgreSQL tooling. Review/apply its schema explicitly:

```powershell
.\.venv\Scripts\python.exe manage.py migrate --plan
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py check_services
.\scripts\run-dev.ps1
```

The service checker runs a read-only DB probe and authenticates SMTP without sending email. SMTP login does not establish sender authorization or inbox delivery. [Brevo SMTP configuration](https://developers.brevo.com/docs/smtp-integration).

## Restart and phone preview

```powershell
.\scripts\run-dev.ps1 -Restart
# For a phone on the same Wi-Fi, replace the placeholder with this PC's IPv4:
.\scripts\run-dev.ps1 -Restart -PhoneAddress 'YOUR_PC_IPV4'
```

The helper stops only a listener identified as this checkout's Python process, checks port availability, and loads configuration in the actual new server process. It displays the process ID, SMTP port and sender-presence flag, never credentials. It performs no migrations, database resets or email sends. Phone host/CSRF allowances affect that terminal session only.

Start the frontend with `npm run dev:phone`; open `http://YOUR_PC_IPV4:5173` on the phone. The API remains bound to localhost and Vite proxies `/api/`. Keep both terminal windows open. The development server is not a production server.

## API

Routes begin with `/api/v1/`. Auth responses are private/no-store. For writes, first fetch `auth/csrf/`, keep the masked token in memory, and send it in `X-CSRFToken` with cookies, including before login.

| Endpoint | Method | Behavior |
| --- | --- | --- |
| `health/` | GET | Process liveness, not dependency readiness |
| `auth/csrf/` | GET | CSRF initialization/masked token |
| `auth/register/` | POST | Draft registration and direct verification email |
| `auth/resend-code/` | POST | Invalidate old challenge and send replacement |
| `auth/verify-email/` | POST | Single-use code consumption and verified-user session |
| `auth/login/` | POST | Active verified-user email/password sign-in |
| `auth/me/` | GET | Current user's safe profile |
| `auth/logout/` | POST | End session |
| `auth/password-reset/` | POST | Request an email recovery code (`email`) |
| `auth/password-reset/verify/` | POST | Verify the code in the requesting browser (`code`) |
| `auth/password-reset/complete/` | POST | Consume verified recovery and set a new password (`password`) |

Registration fields: `display_name`, `email`, `password`, `language` (`en`/`ar`), `timezone` (IANA name). Password minimum: 12 plus Django validators. Login fields: `email`, `password`. Verification uses six ASCII digits in `code`. Email identities are normalized; existing accounts cannot be overwritten by registration.

Django handles password validation/authentication and session rotation. Unverified/inactive/unknown/wrong credentials get a generic error. Login limits are 10/email and 30/direct peer IP per sliding 15 minutes. Email-code requests allow three/email per sliding hour and 20/direct peer IP for abuse control. Proxy-aware IP handling remains a production configuration decision.

Verification hashes/expiry/attempts, rate counters and sessions live in PostgreSQL; no data cache or Redis. Row locks coordinate updates. Codes expire after ten minutes and five wrong attempts. User creation and consumption commit together; superseded sends cannot reactivate old challenges.

Email is intentionally synchronous with a ten-second timeout and no background worker/retries. Failed sends consume allowance and leave a challenge inactive. A replacement invalidates the previous code even if sending fails. Successful SMTP submission is not proof of inbox delivery.

Password reset is separate from registration. Request responses are identical for eligible, unknown, inactive and unverified addresses, including SMTP failure; this response does not confirm delivery. Recovery has separate limits of three requests/email/hour and 20/direct peer IP/hour. Codes are hash-only, expire after ten minutes and allow five wrong attempts. Verification rotates the browser session key and binds recovery proof to that session; completing recovery must happen before the original code expiry. Resending invalidates both the old code and any verified proof. Password validation includes requiring a different password. Completion consumes recovery atomically, flushes the requesting session and changes Django's authentication hash so every earlier authenticated session is denied on its next request. It does not sign in automatically. The frontend refreshes CSRF before subsequent sign-in. No device-list/logout-all screen is implemented yet.

## Hourly cleanup

```powershell
.\.venv\Scripts\python.exe manage.py purge_expired_codes --dry-run
# Mutates the intended database; use only when authorized:
.\.venv\Scripts\python.exe manage.py purge_expired_codes
```

Clears expired hashes while retaining coordination rows, active codes, attempt counts, rate limits, drafts and user data. Batches are 500 rows, maximum 20 batches by default, bounded by `--batch-size`/`--max-batches`. Expiry is rechecked during updates. Cleanup does not extend code validity; ten-minute expiry is enforced regardless of scheduling.

For Northflank deployment, use a short scheduled Job with the same backend image, application working directory, command `python manage.py purge_expired_codes`, schedule `0 * * * *`, Forbid concurrency, 60-second deadline, no automatic retries and no run-on-source-change. Supply DB configuration and the Django secret; SMTP credentials/public ports are unnecessary. First inspect with dry-run, then activate after deployment approval. No hosted schedule is included or active in this repository. [Northflank scheduled jobs](https://northflank.com/docs/v1/application/run/run-an-image-once-or-on-a-schedule).

## Checks

```powershell
.\.venv\Scripts\python.exe manage.py test --settings=config.test_settings
.\.venv\Scripts\python.exe manage.py check --settings=config.test_settings
.\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run --settings=config.test_settings
.\.venv\Scripts\python.exe -m ruff check --no-cache .
.\.venv\Scripts\python.exe -m ruff format --no-cache --check .
.\.venv\Scripts\python.exe -m pip_audit -r requirements.txt
```

Isolated tests skip real `.env`, use SQLite/in-memory mail, and include a test-only SMTP-port fixture. They do not prove PostgreSQL concurrency or email delivery. No CI is configured; checks are local commands.

Production serving/container packaging, ingress/CSRF/TLS configuration, stale-draft/session retention, recovery and deployment acceptance remain pending. Configure secrets through platform environment settings, not the local file. Never reset/migrate a production database without separate authorization.
