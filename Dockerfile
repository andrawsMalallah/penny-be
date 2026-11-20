FROM python:3.14-slim AS runtime-base

# Refresh distribution security fixes before application files enter the image.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

FROM runtime-base AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PENNY_SKIP_DOTENV=1
WORKDIR /app

COPY requirements.txt requirements-production.txt ./
# Pip and its vendored tooling are needed only while installing runtime dependencies.
RUN pip install --no-cache-dir -r requirements-production.txt \
    && python -m pip uninstall --yes pip \
    && useradd --create-home --uid 10001 penny
COPY --chown=penny:penny app ./app
COPY --chown=penny:penny config ./config
COPY --chown=penny:penny manage.py gunicorn.conf.py ./
USER penny
EXPOSE 8000
CMD ["gunicorn", "--config", "gunicorn.conf.py", "config.wsgi:application"]
