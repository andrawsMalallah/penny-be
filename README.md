# Penny API

Django REST Framework API for Penny's web expense tracker. Authentication includes registration, email-code verification, sign-in, password recovery, session management, optional authenticator 2FA and bounded security-state maintenance. Private accounts/groups and opening balances are implemented; financial transactions remain planned separately.

## Accounts and groups

The `finance` app provides verified-user-only, CSRF-protected, no-store APIs. Lists use 30 items per page, stable name/id ordering and eager-loaded group/opening records. All lookups/mutations derive ownership from the session; unknown input fields are rejected.

- `GET/POST /api/v1/accounts/`: list/create money accounts. Create with `name`, nullable `group`, and decimal-string `opening_iqd`/`opening_usd` (nonnegative, up to 16 integer and two fractional digits).
- `GET/POST /api/v1/accounts/<id>/`: read/update metadata (`name`, nullable `group`) only. Opening money cannot be edited here.
- `POST /api/v1/accounts/<id>/archive/`: explicit `{ "archived": true }` or false to restore. Opening money/history stays; archival clears its spending default atomically.
- `GET/POST /api/v1/account-groups/`, `GET/POST /api/v1/account-groups/<id>/`, `POST /api/v1/account-groups/<id>/archive/`: private group list/create/rename/archive/restore. Groups with active accounts cannot be archived. Restore a group's archived accounts only after restoring their group.
- Lists accept `?page=2&archived=false` (or true), never an unbounded page size. Detail endpoints include archived records owned by the caller. There are no delete endpoints.
- Settings GET/POST now includes nullable `default_account`; only an active owned account is valid. Explicit null clears it; omitting this field preserves an existing selection. No default is selected automatically, and reads create no rows.

All financial/settings writes lock the user first. OpeningBalance is a separate creation-time, timestamped record, not income. The current balance serializer reads opening amounts because financial transaction features have not been implemented yet; do not infer a completed ledger/reporting engine. Archived money is still held, not erased. Names remain unique per user (case-insensitive), including archived items. Groups are optional and are not seeded.

The additive schema is `finance/0001_initial` plus `identity/0006_usersettings_default_account`; both have been applied to verified local development. Fresh deployments must separately review and apply migrations. Local verification is 114 passing PostgreSQL tests, including three new finance concurrency checks; isolated SQLite skips those, two factor concurrency tests and the maximum-precision money case because SQLite NUMERIC affinity is not exact at that boundary. No production acceptance is implied.

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
| `auth/session/` | GET | Guest-safe startup state: verified `user` or null, plus `two_factor_pending`; HTTP 200 for guests |
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

The session list returns only browser/device labels, sign-in time, last activity and `is_current`, never keys, auth hashes, raw user-agent headers or IPs. Device labels are approximate and reflect the browser's reported identity. Recognized labels refresh on each authenticated request for that session, fixing descriptions stuck after browser updates/emulation changes; missing or unknown headers preserve known labels. Other sessions' descriptions and original sign-in times are preserved. New login/verification sessions record sign-in time. Existing sessions are enrolled on their next authenticated request, with an unavailable sign-in time; unused legacy sessions will not appear yet. Last activity records authenticated API use, not background browser activity. The fixed 12-hour lifetime is preserved. Expired sessions are excluded; metadata cascades when Django deletes its session. Scheduled retention remains a separate operational task.

Sign-out-all locks the user row, advances its revocation counter and deletes tracked sessions. Middleware rejects earlier counters on subsequent requests, including untracked legacy sessions or stale session data saved by an already-running request. A request already authorized before revocation may finish; revocation cannot undo completed work. New sign-ins still work. Password recovery also revokes tracked metadata. Tests cover stale saves and authentication begun before a password change; true PostgreSQL concurrency still requires separate integration verification.

## Authenticator two-factor authentication

Apply additive `identity/0004_twofactorlogin_twofactorsettings` to the intended development database before running this version. Existing users remain opted out. PyOTP handles standard six-digit, 30-second TOTP; the accepted window allows one adjacent time step in each direction. Successful timecodes are stored and cannot be reused. The user row is locked for verification and proof consumption, including recovery codes, so concurrent submissions cannot both authenticate.

`PENNY_2FA_ENCRYPTION_KEY` must be a stable key generated with `cryptography.fernet.Fernet.generate_key()`, stored in the ignored local environment or hosting secrets. Never print it or commit it. Preserve it securely alongside database recovery procedures; replacing it breaks decryption of existing authenticator secrets. Do not derive it from a password or silently regenerate it at startup. Fernet encrypts active and pending secrets; qrcode generates the provisioning image locally, with no external QR service. Setup secrets/QRs appear only in the password-confirmed setup response and stay out of browser storage.

For an enabled account, password sign-in returns HTTP 202 with `two_factor_required: true`. It rotates into an anonymous browser session containing only the challenge reference; no private access is granted yet. Complete `auth/2fa/login/` with `otp` and optional `recovery: true`. The challenge expires after five minutes/five wrong proofs, is bound to the requesting session and snapshots the password auth hash and session generation. Password changes or session revocation invalidate it. The existing password-login limits remain, with shared 2FA limits of 10 attempts/user and 30/direct peer IP per 15 minutes. Failed password checks also consume security-action allowance.

Setup needs the current password, expires after ten minutes and is bound to that browser. Enabling requires a valid new authenticator code. Ten random 16-character hexadecimal recovery codes are shown once; only Django password hashes persist. A code is consumed atomically and never disables 2FA by itself. Regenerating invalidates the entire older set. Disabling or regenerating requires both the password and a fresh authenticator or unused recovery code. Enabling/disabling/regenerating revokes other sessions and pending logins while renewing the current authenticated browser session; refresh CSRF afterward.

Password reset preserves 2FA and unused recovery codes and signs out existing sessions as before. Losing both the authenticator and every recovery code cannot be bypassed by password reset; no support bypass exists. Users must save their recovery codes outside Penny. No trusted-device exemption is implemented.

Local automated verification includes all 114 backend tests against a disposable PostgreSQL database. Coverage includes simultaneous authenticator/recovery-code reuse, account creation and archive/default/group races, exact opening money, private settings, guest-safe session state and device-label refresh. SQLite passes 108 tests and skips six PostgreSQL-only cases: two factor-reuse tests, three finance concurrency tests and maximum money precision. Tests use isolated email and test-only hashing; browser UI checks use simulated API responses. Actual phone acceptance and production deployment acceptance are separate.

## Hourly cleanup

```powershell
.\.venv\Scripts\python.exe manage.py purge_expired_codes --dry-run
# Mutates the intended database; use only when authorized:
.\.venv\Scripts\python.exe manage.py purge_expired_codes
```

Clears expired hashes and pending 2FA setup ciphertext, and removes expired pre-authentication login challenges. Keeps active authenticator secrets/recovery hashes, live challenges, coordination rows, attempt counts, rate limits, drafts and user data. Batches are 500 rows, maximum 20 batches total across all types, bounded by `--batch-size`/`--max-batches`. Expiry is rechecked during updates. Cleanup does not extend challenge validity; expiry is enforced regardless of scheduling.

For Northflank deployment, use a short scheduled Job with the same backend image, application working directory, command `python manage.py purge_expired_codes`, schedule `0 * * * *`, Forbid concurrency, 60-second deadline, no automatic retries and no run-on-source-change. Supply DB configuration and the Django secret; SMTP credentials/public ports are unnecessary. First inspect with dry-run, then activate after deployment approval. No hosted schedule is included or active in this repository. [Northflank scheduled jobs](https://northflank.com/docs/v1/application/run/run-an-image-once-or-on-a-schedule).

Startup uses `auth/session/`, replacing the previous guest `auth/me/` 403 followed by a factor-pending request. Only verified authenticated sessions receive the existing safe user serializer. Guests receive null identity and a boolean for any valid browser-bound second-factor challenge, checked against its expiry, failure count, password hash and session generation. Responses are no-store; a fresh guest read creates no session. No session keys or secrets are returned, private access/CSRF protection remain unchanged, and `auth/me/` stays protected for explicit profile reads and stale-CSRF diagnosis.

## Private settings

Apply additive `identity/0005_usersettings` to the intended development database before using Settings. It adds a per-user table with optional default currency and exchange rate; existing users, balances and security state are preserved. Production migration requires separate authorization.

`GET /api/v1/settings/` returns the authenticated verified user's `language`, `default_currency` (`IQD`, `USD` or an empty string), nullable `exchange_rate` and nullable `default_account`. A first read supplies empty preferences without creating a row. `POST` requires language, currency, rate and CSRF; `default_account` is optional for compatibility, and unsupported fields are rejected. The default must be an active account owned by the user. Omission preserves the selection; explicit null clears it. Ownership always comes from the session, never a supplied user ID. Writes lock the user and save account language/preferences atomically. Responses use no-store. The spending-default field requires the additive Accounts migrations described above.

The optional rate means **1 USD = X IQD**. It is a positive exact decimal string with up to 12 integer digits and six decimal places, or null to clear. JSON numeric rates are rejected to keep float values outside the contract. Database constraints also enforce positive rates and supported currencies. No rate is preset and no history is recalculated. A spending default is explicitly selected from existing active accounts; no account is chosen automatically. Appearance remains a browser preference.

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
