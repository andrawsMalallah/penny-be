# Keep the Python version aligned with the existing backend CI/runtime.
FROM python:3.14.10-slim-bookworm@sha256:3fe1a0b6c2d6b2ebb572906cdabeca75aeb558c0c17db5ffefbcf8094871c32b

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=config.settings.production \
    PORT=10000

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && python -m pip check \
    && groupadd --gid 10001 penny \
    && useradd --uid 10001 --gid penny --create-home --shell /usr/sbin/nologin penny

# Explicit copies keep development configuration and credentials out of the image.
COPY manage.py ./
COPY apps/ ./apps/
COPY config/ ./config/
COPY scripts/ ./scripts/

USER 10001:10001
EXPOSE 10000
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD ["python", "-m", "scripts.container_health", "ready"]
CMD ["python", "-m", "scripts.serve"]
