# Render Free + Neon deployment

This deployment uses one **Free Python web service** and an existing Neon database.
No paid worker, cron job, persistent disk, Redis or additional service is required.
Application code remains ordinary Django/PostgreSQL, independent of either provider.

## What is prepared

- Gunicorn serves WSGI with one worker and two request threads. A separate Python process
  polls the existing PostgreSQL financial queue every ten seconds; jobs remain outside
  HTTP requests. `python -m scripts.serve` supervises both processes in one service.
- If either process exits, the launcher stops the other and exits unsuccessfully. SIGTERM
  stops both process groups, with a bounded grace period for an in-flight transaction.
  PostgreSQL rolls back an interrupted transaction. Existing interrupted-job protections
  apply; restore jobs are never automatically replayed.
- Startup checks production configuration and pending migrations before launching either
  process. Startup and build do **not** apply migrations or modify financial records.
- `/api/v1/health/` checks process liveness without a database. `/api/v1/ready/` performs
  a bounded PostgreSQL query and returns only `ok` or a generic uncached 503 response.

## Free-tier behavior

Render Free sleeps after 15 minutes without incoming traffic. The next request starts
it again, typically taking about a minute. Both the web server and financial worker stop
while sleeping. No uptime pings, self-requests or other sleep-prevention mechanism is used.

Queued work and financial records are in Neon, not the ephemeral filesystem. On wake,
the worker processes unexpired queued jobs. Jobs expire after one hour; restore previews
expire after ten minutes. If the instance sleeps or restarts during work, check the job
status before creating a new request. An interrupted restore is not automatically retried.
Expiry cleanup runs when the worker runs, so cleanup can be delayed while the service sleeps.

Background activity does not guarantee the service stays awake. This arrangement is
appropriate for an initial free preview, not proof of reliable production scheduling.
Render explicitly advises against production applications on Free instances. Free services
also lack dashboard/SSH shell access, one-off jobs, persistent disks and paid pre-deploy
commands. Follow [Render Free documentation](https://render.com/docs/free) for usage limits.

## Render dashboard configuration

The user has configured the Render dashboard settings. This guide records the intended
configuration; successful Linux/hosted operation still needs the acceptance checks below.
Create or configure a **Web Service**, with **Python 3** runtime and **Free** compute.
Use the backend repository. If the repository root contains `manage.py`, leave Root
Directory blank. Only use `backend` as Root Directory if deploying a combined repository
whose backend actually lives in that subdirectory.

| Setting | Value |
| --- | --- |
| Build Command | `pip install -r requirements.txt && pip check` |
| Start Command | `python -m scripts.serve` |
| Health Check Path | `/api/v1/ready/` |
| Auto-Deploy | Off for the initial manual rollout |

The API does not serve a frontend, admin site or production Swagger page. It needs no
static-file collection or static-serving dependency. `/` intentionally returns 404.
Gunicorn binds `0.0.0.0:$PORT`; Render supplies PORT (normally 10000).

Set the following environment variables in Render's Environment page. Replace all
placeholders. Do not upload the local `.env`, put credentials into Git, or share them in chat.

```dotenv
PYTHON_VERSION=3.14.10
PYTHONUNBUFFERED=1
DJANGO_SETTINGS_MODULE=config.settings.production
PENNY_ALLOWED_HOSTS=<exact-service-name>.onrender.com
PENNY_TRUST_PROXY_HTTPS=1
PENNY_SECRET_KEY=<new-persistent-Django-secret>
PENNY_MFA_ENCRYPTION_KEY=<new-persistent-Fernet-key>
PENNY_DB_HOST=<Neon-pooled-hostname>
PENNY_DB_PORT=5432
PENNY_DB_NAME=neondb
PENNY_DB_USER=neondb_owner
PENNY_DB_PASSWORD=<current-private-Neon-password>
PENNY_DB_SSLMODE=require
PENNY_DB_CHANNEL_BINDING=require
```

Use the database name and role from your actual Neon connection details if different.
`DATABASE_URL` is not consumed. The separate `PENNY_DB_*` variables are authoritative.
`PENNY_ALLOWED_HOSTS` takes exact hostnames separated by commas, without schemes, paths
or wildcard domains. Add each custom hostname explicitly if introduced later.

Generate fresh production keys using the existing Windows virtual environment, then copy
the outputs privately into Render:

```powershell
.\.venv\Scripts\python.exe -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
.\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Generate these once, not on service startup. Retain both in a private password manager.
Changing the Django key can invalidate sessions; losing/changing the Fernet key makes
existing authenticator secrets unreadable. Never copy local development keys into production.
Rotate credentials that have been pasted into chat and update Render with the replacement.

## HTTPS and proxy boundary

Render terminates public TLS and redirects public HTTP to HTTPS before forwarding to the
service over HTTP. The public service port is not directly reachable from the internet.
`PENNY_TRUST_PROXY_HTTPS=1` explicitly enables Django's `X-Forwarded-Proto: https` handling
for that ingress. It defaults off on other deployments; enable it only where the ingress
controls the header and clients cannot bypass that ingress. Secure cookies, CSRF checks,
HSTS and Django's own HTTPS redirect remain enabled. Gunicorn does not separately trust
additional forwarding/scheme headers.

Before accepting user traffic, test public HTTP redirection, normal HTTPS without loops,
and attempts to supply forged `X-Forwarded-Proto` values. A forwarded-header configuration
is not considered proven until checked against the deployed ingress. Do not add wildcard
CSRF origins or CORS just to fix a failed sign-in. Native Android does not require CORS;
a separately hosted browser frontend needs its own reviewed cookie/origin design.

The current IP attempt limits use the direct peer (`REMOTE_ADDR`), which behind Render
may group clients behind a shared ingress address. Account limits remain enforced. Do not
trust arbitrary `X-Forwarded-For` values; review the actual ingress chain separately before
changing client-IP handling. This may reduce sign-in/registration availability under load.

Sources: [Render web services](https://render.com/docs/web-services),
[Python versions](https://render.com/docs/python-version),
[Django proxy setting](https://docs.djangoproject.com/en/6.1/ref/settings/#secure-proxy-ssl-header).

## Schema changes and recovery

The new Neon database had **23 migrations applied and no pending migrations** on
2026-11-10. That proves schema initialization, not hosted application acceptance.
Never configure the build/start command to auto-migrate, seed, flush or reset production.
If future startup reports a pending migration, review it, obtain authorization, take a
recoverable database snapshot/dump, and run only the approved forward migrations using
a private local connection. Render Free has no dashboard shell/pre-deploy facility for this.
Do not run the shared local test runner against Neon.

Application ZIP backups cover an individual user's financial dataset; they do not replace
database backups covering accounts, MFA, sessions and all users. Before relying on the
service for real financial data, establish Neon recovery/retention for the chosen plan,
retain production keys separately, and prove a database restore into a disposable copy.
No database restore, snapshot or recovery drill is performed by these preparation files.

For application rollback, select a compatible prior deployment. Code rollback does not
reverse database migrations; never roll back the schema blindly. In particular, initial
reverse migrations drop tables. Pause traffic and review compatibility before recovery.

## Hosted acceptance after publication/deployment

1. Inspect build/start logs: dependency installation, startup checks and both processes
   must succeed. No password, MFA secret or financial dataset should appear in logs.
2. On a desktop browser, request `https://<host>/api/v1/health/` and `/api/v1/ready/`:
   expect `200 {"status":"ok"}`. `/` returns 404; production docs/schema return 403.
   Test HTTP redirects and forged forwarding headers; HTTPS must not loop.
3. Using a disposable acceptance account, verify CSRF, registration, sign-in/current user,
   logout, password changes and MFA/recovery. Cookies must be Secure/HttpOnly as applicable.
   Repeat ownership checks with a second account; never use real financial data for testing.
4. Verify one financial operation/idempotent replay, then request a backup/CSV and observe
   `queued` becoming `ready` without manually invoking a worker. Download and check it.
   Preview a restore; verify no changes before confirmation. Confirm only for that disposable
   account, then check reconciliation and unchanged credentials/MFA.
5. Leave the service idle for at least 15 minutes, then reopen it. Allow for cold start.
   Verify saved data/session behavior and worker recovery; do not set up keep-alive pings.
6. Real Android authentication, native locking and mobile financial integration are later
   acceptance milestones. A desktop HTTP check is not APK/device evidence.

Do not run the development HTTP checker against production: it is for local disposable
records. CI and initial Git publication were separately approved; cloud resources and
deployed-database changes still require their own authorization.

## CI and deployment enforcement

The workflow `.github/workflows/backend.yml` runs **Backend checks** on main pushes,
pull requests targeting main and manual dispatch. It uses Python 3.14.10 and an isolated
PostgreSQL 17 service, never Neon or repository production secrets. It installs pinned
dependencies, checks lint/format and migration drift, executes all regressions, compiles
Python modules, and starts Linux Gunicorn for an HTTP liveness check. This API has no
frontend production bundle to build. No paid Render service is required.

After the first successful run, enable enforcement yourself:

1. GitHub repository **Settings > Branches > Add classic branch protection rule**:
   use branch pattern `main`. Enable **Require a pull request before merging** and
   **Require status checks to pass before merging**. Select **Backend checks**, choose
   GitHub Actions as its source if offered, and enable **Require branches to be up to date
   before merging**. For a solo repository, do not require another person's approving review.
2. Enable **Do not allow bypassing the above settings** so administrators are covered.
   Leave force pushes/deletions disabled, then save. Subsequent changes go through a
   branch and passing pull request; do not use a force push or admin bypass.
3. In Render, open the backend service's **Settings > Build & Deploy > Auto-Deploy** and
   select **After CI Checks Pass**. Confirm the deployed branch is `main`, then save.
   Until then, leave auto-deploy off. Manual deployment can bypass the automatic gate;
   only deploy a commit whose **Backend checks** run succeeded.

Until those dashboard steps are completed, CI is informational rather than enforced.
The first main push bootstraps the workflow; it cannot have hosted CI results before it
exists remotely. Local checks gate that initial push, then verify the exact commit's run.
Hosted CI does not prove actual Render ingress, Neon recovery or Android device behavior.

References: [GitHub branch protection](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches),
[Render deployment gates](https://render.com/docs/deploys).
