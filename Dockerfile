# syntax=docker/dockerfile:1
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

FROM python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN groupadd --gid 1001 appuser && useradd --uid 1001 --gid 1001 --create-home appuser
COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
RUN chown -R appuser:appuser /app
USER appuser

# SERVICE_ROLE selects web | worker | all (default all). Railway injects PORT.
# The entrypoint ignores the Procfile: this CMD is the single source of truth for the start command.
CMD ["python", "-m", "app.entrypoint"]
