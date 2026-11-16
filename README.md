# Penny API

Django REST Framework API for Penny's web expense tracker. Authentication includes registration, email-code verification, sign-in, password recovery, session management, optional authenticator 2FA and bounded security-state maintenance. Account setup and financial features are planned separately.

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
| `PENNY_2FA_ENCRYPTION_KEY` | Required stable Fernet key for authenticator secrets; generated separately from Django's secret |
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
| `auth/sessions/` | GET | Current user's active session metadata; 30 per page (`?page=2`) |
| `auth/logout-all/` | POST | Revoke every existing session, including this browser; CSRF required |
| `auth/2fa/` | GET | Current user's enabled status and remaining recovery-code count |
| `auth/2fa/login/` | GET / POST | Pending password-proven challenge status / complete second-factor sign-in |
| `auth/2fa/login/cancel/` | POST | Flush the requesting browser's sign-in challenge |
| `auth/2fa/setup/` | POST | Confirm password and return a new setup key/QR code |
| `auth/2fa/setup/cancel/` | POST | Clear pending setup belonging to this browser |
| `auth/2fa/enable/` | POST | Verify an authenticator code, enable 2FA and show recovery codes once |
| `auth/2fa/disable/` | POST | Disable with password plus authenticator/recovery proof |
| `auth/2fa/recovery-codes/` | POST | Replace all recovery codes with password plus authenticator/recovery proof |
| `auth/password-reset/` | POST | Request an email recovery code (`email`) |
| `auth/password-reset/verify/` | POST | Verify the code in the requesting browser (`code`) |
| `auth/password-reset/complete/` | POST | Consume verified recovery and set a new password (`password`) |

Registration fields: `display_name`, `email`, `password`, `language` (`en`/`ar`), `timezone` (IANA name). Password minimum: 12 plus Django validators. Login fields: `email`, `password`. Verification uses six ASCII digits in `code`. Email identities are normalized; existing accounts cannot be overwritten by registration.

Django handles password validation/authentication and session rotation. Unverified/inactive/unknown/wrong credentials get a generic error. Login limits are 10/email and 30/direct peer IP per sliding 15 minutes. Email-code requests allow three/email per sliding hour and 20/direct peer IP for abuse control. Proxy-aware IP handling remains a production configuration decision.

Verification hashes/expiry/attempts, rate counters and sessions live in PostgreSQL; no data cache or Redis. Row locks coordinate updates. Codes expire after ten minutes and five wrong attempts. User creation and consumption commit together; superseded sends cannot reactivate old challenges.

Email is intentionally synchronous with a ten-second timeout and no background worker/retries. Failed sends consume allowance and leave a challenge inactive. A replacement invalidates the previous code even if sending fails. Successful SMTP submission is not proof of inbox delivery.

Password reset is separate from registration. Request responses are identical for eligible, unknown, inactive and unverified addresses, including SMTP failure; this response does not confirm delivery. Recovery has separate limits of three requests/email/hour and 20/direct peer IP/hour. Codes are hash-only, expire after ten minutes and allow five wrong attempts. Verification rotates the browser session key and binds recovery proof to that session; completing recovery must happen before the original code expiry. Resending invalidates both the old code and any verified proof. Password validation includes requiring a different password. Completion consumes recovery atomically, flushes the requesting session and changes Django's authentication hash so every earlier authenticated session is denied on its next request. It does not sign in automatically. The frontend refreshes CSRF before subsequent sign-in.

## Signed-in devices

Apply additive `identity/0003_user_session_generation_signedinsession` to the intended development database before starting this version. It adds a per-user revocation counter and indexed metadata linked to Django's existing session table; it does not change passwords or reset user data. Production migration remains a separate deployment decision.

The session list returns only browser/device labels, sign-in time, last activity and `is_current`, never keys, auth hashes, raw user-agent headers or IPs. Device labels are approximate. New login/verification sessions record sign-in time. Existing sessions are enrolled on their next authenticated request, with an unavailable sign-in time; unused legacy sessions will not appear yet. Last activity records authenticated API use, not background browser activity. The existing fixed 12-hour lifetime is preserved. Expired sessions are excluded; metadata cascades when Django deletes its session. Scheduled session retention remains a separate operational task.

Sign-out-all locks the user row, advances its revocation counter and deletes tracked sessions. Middleware rejects earlier counters on subsequent requests, including untracked legacy sessions or stale session data saved by an already-running request. A request already authorized before revocation may finish; revocation cannot undo completed work. New sign-ins still work. Password recovery also revokes tracked metadata. Tests cover stale saves and authentication begun before a password change; true PostgreSQL concurrency still requires separate integration verification.

## Authenticator two-factor authentication

Apply additive `identity/0004_twofactorlogin_twofactorsettings` to the intended development database before running this version. Existing users remain opted out. PyOTP handles standard six-digit, 30-second TOTP; the accepted window allows one adjacent time step in each direction. Successful timecodes are stored and cannot be reused. The user row is locked for verification and proof consumption, including recovery codes, so concurrent submissions cannot both authenticate.

`PENNY_2FA_ENCRYPTION_KEY` must be a stable key generated with `cryptography.fernet.Fernet.generate_key()`, stored in the ignored local environment or hosting secrets. Never print it or commit it. Preserve it securely alongside database recovery procedures; replacing it breaks decryption of existing authenticator secrets. Do not derive it from a password or silently regenerate it at startup. Fernet encrypts active and pending secrets; qrcode generates the provisioning image locally, with no external QR service. Setup secrets/QRs appear only in the password-confirmed setup response and stay out of browser storage.

For an enabled account, password sign-in returns HTTP 202 with `two_factor_required: true`. It rotates into an anonymous browser session containing only the challenge reference; no private access is granted yet. Complete `auth/2fa/login/` with `otp` and optional `recovery: true`. The challenge expires after five minutes/five wrong proofs, is bound to the requesting session and snapshots the password auth hash and session generation. Password changes or session revocation invalidate it. The existing password-login limits remain, with shared 2FA limits of 10 attempts/user and 30/direct peer IP per 15 minutes. Failed password checks also consume security-action allowance.

Setup needs the current password, expires after ten minutes and is bound to that browser. Enabling requires a valid new authenticator code. Ten random 16-character hexadecimal recovery codes are shown once; only Django password hashes persist. A code is consumed atomically and never disables 2FA by itself. Regenerating invalidates the entire older set. Disabling or regenerating requires both the password and a fresh authenticator or unused recovery code. Enabling/disabling/regenerating revokes other sessions and pending logins while renewing the current authenticated browser session; refresh CSRF afterward.

Password reset preserves 2FA and unused recovery codes and signs out existing sessions as before. Losing both the authenticator and every recovery code cannot be bypassed by password reset; no support bypass exists. Users must save their recovery codes outside Penny. No trusted-device exemption is implemented.

Local automated verification includes the complete 78-test backend suite against a disposable PostgreSQL database, with simultaneous reuse tests for authenticator and recovery codes. SQLite runs 76 tests and skips those two PostgreSQL-only tests. Tests use isolated email and test-only hashing; browser UI checks use simulated API responses. Actual phone/authenticator scanning and production deployment acceptance are separate.

## Hourly cleanup

```powershell
.\.venv\Scripts\python.exe manage.py purge_expired_codes --dry-run
# Mutates the intended database; use only when authorized:
.\.venv\Scripts\python.exe manage.py purge_expired_codes
```

Clears expired hashes and pending 2FA setup ciphertext, and removes expired pre-authentication login challenges. Keeps active authenticator secrets/recovery hashes, live challenges, coordination rows, attempt counts, rate limits, drafts and user data. Batches are 500 rows, maximum 20 batches total across all types, bounded by `--batch-size`/`--max-batches`. Expiry is rechecked during updates. Cleanup does not extend challenge validity; expiry is enforced regardless of scheduling.

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

Default isolated tests skip real `.env`, use SQLite/in-memory mail, and include test-only SMTP-port/encryption-key fixtures. The PostgreSQL-only concurrency tests skip under SQLite; the full suite was also executed separately in a disposable PostgreSQL database with in-memory email. Neither proves email delivery or production readiness. No CI is configured; checks are local commands.

Production serving/container packaging, ingress/CSRF/TLS configuration, stale-draft/session retention, recovery and deployment acceptance remain pending. Configure secrets through platform environment settings, not the local file. Never reset/migrate a production database without separate authorization.
