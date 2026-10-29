# Multi-stage Dockerfile for Digisound Car Audio Scraper (Web API & Background Worker)
FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Final runtime image
FROM python:3.12-slim AS runner

WORKDIR /app

# Install runtime libpq for asyncpg / psycopg
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create unprivileged application user
RUN groupadd -g 1001 appgroup && \
    useradd -u 1001 -g appgroup -s /bin/bash -m appuser

# Copy installed Python packages from builder
COPY --from=builder /root/.local /home/appuser/.local

# Copy application files
COPY alembic.ini .
COPY alembic/ alembic/
COPY app/ app/

# Set ownership to non-root user
RUN chown -R appuser:appgroup /app /home/appuser

USER appuser
ENV PATH="/home/appuser/.local/bin:${PATH}"
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

# Default entrypoint runs the FastAPI web service
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
