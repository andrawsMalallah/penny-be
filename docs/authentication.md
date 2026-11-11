# Core authentication API

Implemented: registration, sign-in/out, current user, password changes, device reads,
sliding sessions, optional authenticator MFA and recovery codes. Android/server step-up
remains pending. Other-device revocation is disabled; financial APIs are documented in
[the financial API](finances.md) under the approved sequencing exception.

The backend uses standard Django/DRF/PostgreSQL APIs and process configuration. It has no
hosting-provider SDK, metadata detection, proprietary database API or hardcoded hostnames.
It can run on a compatible WSGI/ASGI host and PostgreSQL server. Selecting a cloud service
does not change the application contract. Local penny-only tests are the user's explicit
local choice, not a deployment database-name restriction.

## OpenAPI compatibility note

The runtime logout endpoint accepts an empty body. Its generated OpenAPI schema currently
includes legacy login-body metadata even though those fields are not required by logout
at runtime. Cleanup preserves that existing generated contract; correcting the metadata
is deferred to a separately reviewed API-contract change. The table below describes
runtime behavior.

## Contract

All paths start with /api/v1/auth/. JSON bodies reject unknown fields. Authentication uses
Django's session cookie; the cookie/session identifier is never returned as a JSON field.
Retain cookies in protected mobile storage. Unsafe requests send the CSRF token returned
by csrf/ (and refreshed by register/login responses) in X-CSRFToken. No CSRF exemptions
are introduced for mobile. Android clients must protect origin-scoped cookies locally and
use HTTPS. Native transport/local locking source is implemented in the separate mobile app;
APK compilation and real-device behavior remain unverified. Server device proof is separate
from local unlocking; see [the assessment](device-verification.md).

| Method | Path | Input | Result |
| --- | --- | --- | --- |
| GET | csrf/ | None | csrf_token and corresponding CSRF cookie; no-store |
| POST | register/ | username, password, optional display_name/device_name | 201, user and fresh csrf_token; signed-in cookie |
| POST | login/ | username, password, optional device_name | 200, user and fresh csrf_token; rotated session |
| POST | logout/ | Empty object | 204; current server session invalidated |
| GET | me/ | None | id, username, display_name of current user |
| POST | password/ | current_password, new_password | 200; current device retained, other sessions invalidated |
| GET | devices/ | Optional cursor | results/next/previous; owned active devices only |
| GET | mfa/ | None | enabled and recovery_codes_remaining; never existing secrets/codes |
| POST | mfa/setup/ | password | manual_key, enrollment_uri, expires_in=300 |
| POST | mfa/confirm/ | code | Activate enrollment; recovery_codes shown once and csrf_token |
| POST | mfa/login/ | code | Complete pending login using TOTP or recovery code; user/csrf_token |
| POST | mfa/recovery-codes/ | password, code | Replace all recovery codes; new codes shown once/csrf_token |
| POST | mfa/disable/ | password, code | Disable MFA, delete secret/codes; empty recovery_codes/csrf_token |

### MFA lifecycle

For MFA-enabled accounts, login/ returns 200 with mfa_required=true, expires_in=300
and csrf_token, without a user or authenticated session. Retain the anonymous cookie and
complete mfa/login/ within five minutes. All protected endpoints deny pending sessions.
The challenge is server-side and bound to that cookie, the user and the password auth hash.
Successful completion consumes the challenge and rotates into a normal 90-day device session.
Retrying password login replaces this client's previous challenge/session.

Enrollment requires an authenticated device and current password. It expires after five
minutes, is bound to the setup device, and remains inactive until mfa/confirm/ verifies a
six-digit TOTP. Restarting setup replaces an unconfirmed secret. The standard otpauth URI
and manual key are returned only by setup; QR rendering is deferred to mobile work.
TOTP uses SHA1, six digits, 30-second steps and one step of clock tolerance. An accepted
step cannot be accepted again, including a code consumed during activation.

Activation keeps and rotates the setup session, deletes all other device sessions and
invalidates pending sign-ins. Ten independent 32-character hex recovery codes are generated
with django-otp's random utility, returned once and stored only using Django password hashes.
Each recovery code is single-use. There is no code retrieval endpoint or reconstructible seed.
Store codes privately; a lost response requires regeneration with the authenticator or a
remaining recovery code. Penny does not provide password recovery through these codes.

Disabling MFA or regenerating recovery codes requires both the current password and a fresh
TOTP or unused recovery code. Regeneration immediately replaces the entire old set.
Both operations rotate the current session and invalidate pending sign-ins; other existing
authenticated devices remain signed in. Password changes also invalidate pending challenges.
All transitions share the user lock with password sign-in/change. Recovery consumption,
TOTP step advancement, code replacement and session changes commit or roll back together.

MFA attempts share the existing login account/IP limits, including successful setup and
verification attempts. Limits persist even when verification fails. A fresh six-digit TOTP
may require waiting for the next authenticator interval after setup or another successful use.
Failed verification returns the same 403 detail. MFA endpoints require CSRF; unknown fields
are rejected. All auth responses are no-store. Secrets are encrypted using the existing
PENNY_MFA_ENCRYPTION_KEY, never logged, and sensitive service/view locals are marked for
Django's production exception reporter. Missing/wrong encryption keys cannot authorize MFA.

Usernames are case-insensitive for login and uniqueness and stay immutable. Passwords retain
whitespace and follow the existing policy. Device names default to Android device when
omitted and allow up to 80 characters; names are labels, never authorization evidence.
Device results contain id, device_name, created_at, last_active_at and is_current; no session
keys, password hashes, IP addresses or other user's data. Page size is 50, newest first.

Validation failures return 400; missing authentication, bad credentials and CSRF failures
return 403; exhausted attempt limits return 429. Login failures for unknown/inactive users
and wrong passwords return the same generic message. Auth responses prohibit caching.

Sessions expire after 90 days of inactivity. The device record checks the authoritative
expiry before access; only successful authorized responses renew it. Rejected requests,
CSRF bootstrap, health checks and failed authentication do not extend it. Both Django's
session and the device record must be valid. Password change and sign-in serialize on the
user row; session expiry/renewal does not revive deleted sessions.

## Attempt limits and maintenance

Database-backed counters serialize concurrent attempts using PostgreSQL locks. The defaults
are 5 attempts per case-normalized account and 50 per peer IP per 15 minutes, plus 10
registrations per peer IP per hour. Password-change verification shares login limits.
Successful attempts also count; limits expire automatically. Keys use Django's keyed hashes
so raw usernames/IPs are not retained in the rate-limit table. Settings can override limits.

Only the server's REMOTE_ADDR is used. No forwarded header is trusted automatically.
When deploying behind any proxy, configure its trusted-client-address handling deliberately
for that infrastructure. Keep HTTPS termination/forwarded-protocol trust explicit as well.
No platform-specific proxy convention is assumed by this application.

Run standard Django clearsessions and this generic command periodically using whichever
scheduler the deployment provides (no scheduler/CI was created here):

```powershell
.\.venv\Scripts\python.exe manage.py prune_auth_state
```

It deletes expired device records/MFA challenges and attempt buckets older than one day; it does not
delete users or financial data. Expired records are already denied before maintenance.
Never print .env values, passwords, session cookies or CSRF tokens during diagnostics.

## Manual test on Windows

1. From backend/, start `.\.venv\Scripts\python.exe -m scripts.development runserver`.
2. Open http://127.0.0.1:8000/api/v1/health/ in your Windows browser: status ok.
3. In another PowerShell window, use the following flow. Get-Credential asks for a new
   throwaway username and a policy-compliant password. Cookie/CSRF values stay in variables.

```powershell
$pennyAuthRoot = 'http://127.0.0.1:8000/api/v1/auth'
$pennyBootstrap = Invoke-RestMethod "$pennyAuthRoot/csrf/" -SessionVariable pennyWebSession
$pennyCredential = Get-Credential -Message 'New throwaway Penny account'
$pennyBody = @{
    username = $pennyCredential.UserName
    password = $pennyCredential.GetNetworkCredential().Password
    device_name = 'Windows API check'
} | ConvertTo-Json
$pennyRegistration = Invoke-RestMethod "$pennyAuthRoot/register/" -Method Post `
    -WebSession $pennyWebSession -ContentType 'application/json' -Body $pennyBody `
    -Headers @{ 'X-CSRFToken' = $pennyBootstrap.csrf_token }
$pennyMe = Invoke-RestMethod "$pennyAuthRoot/me/" -WebSession $pennyWebSession
$pennyMe.username
$pennyDevices = Invoke-RestMethod "$pennyAuthRoot/devices/" -WebSession $pennyWebSession
$pennyDevices.results | Select-Object device_name,is_current
$pennyLogout = Invoke-WebRequest "$pennyAuthRoot/logout/" -Method Post `
    -WebSession $pennyWebSession -ContentType 'application/json' -Body '{}' `
    -Headers @{ 'X-CSRFToken' = $pennyRegistration.csrf_token } -UseBasicParsing
$pennyLogout.StatusCode
```

4. Expect your username, one current device and logout status 204. Then me/ should fail
   with 403. Keep credentials/cookies out of screenshots or shared terminal output.
5. Sign in on two clients and change the password on one: it stays signed in; the other
   loses access. Login with different username capitalization should work. Restart the
   server without clearing the client's cookies: the valid session should still work.

Automated regression and concurrency checks run with
`.\.venv\Scripts\python.exe -m scripts.development test` on the SAME local penny tables.
The user approved local test cleanup; this is not authorization to test deployed data.

## Manual MFA check on Windows and an authenticator phone

Use a throwaway account with the PowerShell cookie/CSRF helper pattern above; the backend
is a JSON API rather than an account-management web UI. Prefix the paths below with http://127.0.0.1:8000/api/v1/auth/.

1. Sign in on two independent cookie clients. POST mfa/setup/ with your current password.
   Privately enter its manual_key into your phone's authenticator as a time-based account.
2. POST mfa/confirm/ with the authenticator's current code. Privately save the ten recovery
   codes. The setup client must still access me/; the second client's me/ must return 403.
3. Sign in again on the second client: expect mfa_required, then me/ returning 403.
   Wait for the next authenticator code and POST mfa/login/ using that client's cookies/CSRF.
   Expect me/ to work. Reusing that same code in another pending login must fail.
4. Complete a pending login using one recovery code; reuse must fail. GET mfa/ should show
   one fewer remaining code. Restarting the server must retain MFA and existing sessions.
5. POST mfa/recovery-codes/ using password plus an unused recovery code. Old codes and
   previously pending challenges must fail. New codes must work after a fresh password login.
6. POST mfa/disable/ with password plus a fresh TOTP/unused recovery code. GET mfa/ must show
   disabled and zero codes. A fresh password login should no longer require MFA.

Fetch csrf/ before a write or use the latest returned csrf_token. Avoid printing cookies,
passwords, setup responses or recovery-code responses into shared terminals/screenshots.
The default five account attempts per 15 minutes also count successful verification actions;
pace manual scenarios or use separate synthetic accounts rather than weakening the limiter.
