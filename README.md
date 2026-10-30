# Digisound Car Audio Scraper

Autonomous, high-reliability backend scraper service, normalization engine, and incremental delta sync API for Digisound's car audio catalog ([`https://www.digisound.ee/products/car-stereo`](https://www.digisound.ee/products/car-stereo)).

---

## Features

- **Resumable Synchronisation**: Checkpoint-based discovery crawl storing per-page offsets in PostgreSQL; an interrupted run is resumed (same run, same job) after a restart.
- **Dual Content & Commerce Hashing**: Separate SHA-256 hashes prevent redundant updates when prices or descriptions have not changed.
- **Pure Normalization Suite**:
  - European decimal-comma and thousands-separator price parsing to `Decimal`.
  - VAT basis taken only from page text (never assumed).
  - GS1 Modulo-10 barcode validation for EAN-8, UPC-A, EAN-13, and GTIN-14.
  - Exact SKU preservation (preserving embedded spaces, slashes, and punctuation).
  - Explicit stock status mapping (`in_stock`, `out_of_stock`, `backorder`, `unknown`). Unstated numeric stock quantity remains `NULL` (never coerced to 0).
- **FastAPI REST API**:
  - **System**: `/live` (liveness), `/health` (readiness: database + migrations; HTTP 503 when degraded), `/api/stats`
  - **Catalog**: `/api/categories`, `/api/products`, `/api/product/id/{id}`, `/api/product/sku/{sku:path}` (handles URL-encoded spaces and slashes, returns `HTTP 409 ambiguous_sku` on duplicates).
  - **Delta Feed**: `GET /api/products/delta` with stable-window cursor model (`from_seq`, `until_seq`, `last_seq`).
  - **Job Triggers**: Protected `POST /api/jobs/discovery` and `POST /api/jobs/stock` guarded by `X-API-Key`.
- **Operator CLI**: Typer commands for reconciliation CSV exports, WordPress/WooCommerce formatted exports, and unseen products review reports.
- **Automated Worker & Scheduler**: Background leased worker with heartbeat and a stateless, database-backed cron scheduler (evaluated in `SCHEDULE_TIMEZONE`).

---

## Tech Stack

- **Runtime**: Python 3.12+
- **API Framework**: FastAPI + Pydantic v2 + Uvicorn
- **ORM & Database**: async SQLAlchemy 2.0 + asyncpg + PostgreSQL 14+
- **Database Migrations**: Alembic
- **HTTP Client & Parsing**: HTTPX + Tenacity + BeautifulSoup4 / lxml
- **CLI**: Typer
- **Scheduling**: Croniter
- **Testing**: Pytest + pytest-asyncio + respx (149 automated tests)
- **Deployment**: Docker, Railway

---

## Quick Start (Local Development)

### 1. Requirements
- Python 3.12+
- PostgreSQL running locally or in Docker

### 2. Setup Environment
```bash
# Clone the repository
git clone https://github.com/Hassan-Buger/car_Audio.git
cd car_Audio

# Create virtual environment
python -m venv .venv
source .venv/bin/activate # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Configure Database & Keys
Copy `.env.example` to `.env` and fill in your values (never commit real secrets):
```bash
cp .env.example .env
```
See `.env.example` for every variable.

### 4. Run Migrations
```bash
alembic upgrade head
```

### 5. Start API and worker together
```bash
python -m app.entrypoint
```
`SERVICE_ROLE` selects `web`, `worker` or `all` (default). Or run them separately:
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
python -m app.worker
```
Interactive API documentation: `http://localhost:8000/docs`

---

## Operator CLI Commands

```bash
python -m app.cli reconciliation-csv --output reconciliation.csv
python -m app.cli wordpress-export --format csv --output catalog.csv
python -m app.cli unseen-report --output unseen.csv   # never auto-deletes
python -m app.cli trigger discovery
python -m app.cli trigger stock
python -m app.cli stats
```

---

## Running Tests & Quality Checks

Tests need a PostgreSQL database; set `TEST_DB_URL` (asyncpg URL).

```bash
pytest tests/ -v
ruff check app tests
ruff format --check app tests
```

---

## Railway Deployment

See [docs/RAILWAY_DEPLOYMENT.md](docs/RAILWAY_DEPLOYMENT.md). In short: the Dockerfile runs `python -m app.entrypoint` (migrate, then API and worker); add a PostgreSQL service and set `DATABASE_URL=${{Postgres.DATABASE_URL}}`; set `API_KEY` and `READ_API_KEY` yourself in Railway; set `BOOTSTRAP_DISCOVERY=true` for the first crawl; use `/live` as the healthcheck path.

> **Source access**: Digisound may return 403 to non-EU IP addresses. The scraper never bypasses this; a 403 fails the run and is recorded. Prefer an EU region (e.g. Amsterdam) if the source blocks your region.

---

## Documentation

- [Railway Deployment](docs/RAILWAY_DEPLOYMENT.md): Roles, variables, schedules and completeness rules.
- [API Integration Guide](docs/API_INTEGRATION.md): Authentication, endpoints, pagination, delta feed, stock mapping and schedules.
- [WordPress Integration Guide](docs/wordpress-integration.md): Exact SKU lookup, 409 handling, and delta feed consumption.
- [Source Findings & Limitations](docs/limitations.md): Creamarketing WebAdmin 7 details, filter facets analysis, and geo-blocking behavior.
- [Deployment Evidence](docs/deploy-evidence.md): Earlier migration verification and curl testing commands.
