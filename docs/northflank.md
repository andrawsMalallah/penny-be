# Northflank Docker deployment

Use one Northflank Sandbox service and the existing Neon PostgreSQL database. The image
runs the existing supervisor: Gunicorn and the PostgreSQL financial worker together.
No Redis, Northflank Job, persistent volume or additional database is needed. Use one
replica initially. Monitor memory during encrypted backups/restores before real use.

## Build and service configuration

1. After separately authorized publication, select the backend repository and a commit
   with passing Backend checks. Create a combined build/deployment service using Dockerfile.
2. For the standalone backend repository, use build context `/` and Dockerfile
   `/Dockerfile`. In a combined checkout, use `/backend` and `/backend/Dockerfile`.
   Keep the Docker CMD unchanged: `python -m scripts.serve`.
3. Expose port `10000` as public **HTTP**. Northflank terminates public HTTPS at its edge.
   Keep `PORT=10000` unless you also change the service port. Do not expose a public TCP
   port that bypasses the HTTP ingress. Obtain the generated HTTPS hostname.
4. Set runtime variables below in Northflank secrets/environment settings, never build
   arguments. Do not upload the local `.env`. Keep existing Django and Fernet production
   keys when moving the existing database; replacing them can invalidate sessions or
   make stored MFA secrets unreadable.
5. Keep automatic deployment disabled for initial acceptance. Existing GitHub CI does
   not build this Docker image. Deploy only a passing commit and inspect the Docker build
   result; a Docker CI gate requires a separately approved workflow change.

```dotenv
PORT=10000
DJANGO_SETTINGS_MODULE=config.settings.production
PENNY_ALLOWED_HOSTS=<exact-Northflank-public-hostname>
PENNY_TRUST_PROXY_HTTPS=1
PENNY_SECRET_KEY=<existing-persistent-production-key>
PENNY_MFA_ENCRYPTION_KEY=<existing-persistent-production-Fernet-key>
PENNY_DB_HOST=<existing-Neon-pooled-hostname>
PENNY_DB_PORT=5432
PENNY_DB_NAME=<existing-database-name>
PENNY_DB_USER=<existing-role>
PENNY_DB_PASSWORD=<private-current-password>
PENNY_DB_SSLMODE=require
PENNY_DB_CHANNEL_BINDING=require
```

`PENNY_ALLOWED_HOSTS` accepts comma-separated exact hostnames, without schemes or paths.
`DATABASE_URL` is not used. Python is selected by the image; PYTHON_VERSION is unnecessary.
Proxy trust requires the ingress to control X-Forwarded-Proto. Verify public HTTPS does
not redirect in a loop, HTTP redirects to HTTPS, and forged scheme headers cannot bypass
HTTPS enforcement before accepting traffic. Do not change cookie/CSRF protections to fix
an ingress configuration problem. Client-IP limits retain their existing direct-peer behavior.

## Health checks

Configure Northflank **CMD** probes. HTTP probes accept redirects, which could hide an
unhealthy Django endpoint. The supplied helper uses loopback, the configured allowed Host
and trusted HTTPS header, rejects redirects and requires an exact 200 JSON response.

| Probe | Command | Initial delay | Interval | Timeout | Failures |
| --- | --- | --- | --- | --- | --- |
| Startup | `python -m scripts.container_health ready` | 10 s | 10 s | 10 s | 12 |
| Readiness | `python -m scripts.container_health ready` | 10 s | 30 s | 10 s | 3 |
| Liveness | `python -m scripts.container_health health` | 30 s | 30 s | 10 s | 3 |

Use success threshold 1. Readiness checks PostgreSQL; liveness does not, avoiding restarts
solely because Neon is unavailable. The supervisor exits if either web or worker dies.
The Docker HEALTHCHECK supplies readiness for local Docker; configure Northflank probes
explicitly rather than assuming the platform imports it. Allow at least 30 seconds for
container termination where configurable, so the supervisor can drain its children.

Startup runs production checks and `migrate --check`, never migrations or seeding.
If migrations are pending, stop and follow the separately authorized migration procedure
in [deployment.md](deployment.md#schema-changes-and-recovery). Do not run tests against Neon.

## Local build and validation (PowerShell)

From the backend directory with Docker Desktop's Linux engine running:

```powershell
docker build --pull -t penny-backend:local .
docker run --rm --entrypoint python penny-backend:local -m pip check
docker run --rm --entrypoint python penny-backend:local -c "import os; assert os.getuid() == 10001"
```

The build requires no app secrets or database. Runtime smoke tests need privately injected
configuration and a local TLS-enabled PostgreSQL database with compatible schema. Do not
reuse production credentials or aim the queue worker at deployed records for local tests.
Do not pass secret values literally in shell commands. The standard local PostgreSQL helper
uses TLS disabled, so it is not automatically a production-container fixture.

## Acceptance and cutover

On a desktop browser/Postman, check the generated HTTPS `/api/v1/health/` and
`/api/v1/ready/`: both must return 200 and `{"status":"ok"}`. `/` intentionally returns
404. Verify HTTP redirection, forwarded-header behavior, secure session/CSRF cookies and
sign-in/current-user/logout using a disposable account. Request a CSV or backup and check
that it reaches ready without manual worker execution. Observe CPU/memory and test a
service restart before trusting queue recovery. Existing hosted acceptance/recovery checks
in deployment.md still apply. Android testing remains a separate milestone.

When reusing the existing production database, coordinate cutover: stop the old Render
service before starting the new worker. Do not leave two hosting deployments processing
the same queue unintentionally. Retain production keys and database recovery access.

Sandbox currently advertises always-on services and a limited free allowance. Confirm
the actual compute allocation, usage and billing in your account. It is a hobby/test tier,
not a production availability guarantee. Neon may still have its own idle/cold-start limits.

Sources: [Northflank pricing](https://northflank.com/pricing),
[Docker build/deploy](https://northflank.com/docs/v1/application/getting-started/build-and-deploy-your-code),
[health probes](https://northflank.com/docs/v1/application/observe/configure-health-checks),
[networking](https://northflank.com/docs/v1/application/network/networking-on-northflank).
