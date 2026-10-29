# Digisound Car Audio Scraper

Autonomous, high-reliability backend scraper service, normalization engine, and incremental delta sync API for Digisound's car audio catalog ([`https://www.digisound.ee/products/car-stereo`](https://www.digisound.ee/products/car-stereo)).

---

## Features

- **Resumable Synchronisation**: Checkpoint-based discovery crawl storing per-page offsets in PostgreSQL; resumes seamlessly after interruptions.
- **Dual Content & Commerce Hashing**: Separate SHA-256 hashes prevent redundant updates when prices or descriptions have not changed.
- **Pure Normalization Suite**:
  - European decimal-comma and thousands-separator price parsing to `Decimal`.
  - GS1 Modulo-10 barcode validation for EAN-8, UPC-A, EAN-13, and GTIN-14.
  - Exact SKU preservation (preserving embedded spaces, slashes, and punctuation).
  - Explicit stock status mapping (`in_stock`, `out_of_stock`, `backorder`, `unknown`). Unstated numeric stock quantity remains `NULL` (never coerced to 0).
- **FastAPI REST API**:
  - **System**: `/health`, `/api/stats`
  - **Catalog**: `/api/categories`, `/api/products`, `/api/product/id/{id}`, `/api/product/sku/{sku:path}` (handles URL-encoded spaces and slashes, returns `HTTP 409 ambiguous_sku` on duplicates).
  - **Delta Feed**: `GET /api/products/delta` with stable-window cursor model (`from_seq`, `until_seq`, `last_seq`).
  - **Job Triggers**: Protected `POST /api/jobs/discovery` and `POST /api/jobs/stock` guarded by `X-API-Key`.
- **Operator CLI**: Typer commands for reconciliation CSV exports, WordPress/WooCommerce formatted exports, and unseen products review reports.
- **Automated Worker & Scheduler**: Background leased worker with heartbeat and cron schedule evaluation.

---

## Tech Stack

- **Runtime**: Python 3.12+
- **API Framework**: FastAPI + Pydantic v2 + Uvicorn
- **ORM & Database**: async SQLAlchemy 2.0 + asyncpg + PostgreSQL 14+
- **Database Migrations**: Alembic
- **HTTP Client & Parsing**: HTTPX + Tenacity + BeautifulSoup4 / lxml
- **CLI**: Typer
- **Scheduling**: Croniter
- **Testing**: Pytest + pytest-asyncio + respx (74 automated tests)
- **Deployment**: Docker, Railway, Procfile

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
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Configure Database & Keys
Copy `.env.example` to `.env` and fill in your values:
```bash
cp .env.example .env
```
Key environment variables:
```dotenv
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/digisound
API_KEY=your-secret-job-trigger-key
READ_API_KEY=  # Optional: leave empty for open read API
HTTP_CONCURRENCY=2
REQUEST_DELAY_MS=500
```

### 4. Run Migrations
```bash
alembic upgrade head
```

### 5. Start the Web API
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```
Interactive API documentation is accessible at:
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

### 6. Start the Background Worker
In a separate terminal:
```bash
python -m app.worker
```

---

## Operator CLI Commands

The scraper includes CLI commands in `app.cli`:

```bash
# 1. Generate category completeness & reconciliation CSV
python -m app.cli reconciliation-csv --output reconciliation.csv

# 2. Export catalog formatted for WordPress / WooCommerce
python -m app.cli wordpress-export --format csv --output catalog.csv
python -m app.cli wordpress-export --format json --output catalog.json

# 3. Generate unseen products report for manual review (never auto-deleted)
python -m app.cli unseen-report --output unseen.csv

# 4. Manually trigger crawl jobs
python -m app.cli trigger discovery
python -m app.cli trigger stock

# 5. Display catalog summary statistics
python -m app.cli stats
```

---

## Running Tests & Quality Checks

```bash
# Run complete test suite (74 tests)
pytest tests/ -v

# Static analysis and linting
ruff check app tests
ruff format --check app tests
mypy app tests
```

---

## Railway Deployment

1. **Deploy Repository**:
   - Push this repository to GitHub (`https://github.com/Hassan-Buger/car_Audio`).
   - Create a new project in [Railway](https://railway.app) and select **Deploy from GitHub repo**.
2. **Add PostgreSQL Service**:
   - In Railway, click **+ New** -> **Database** -> **Add PostgreSQL**.
   - Connect the PostgreSQL database to the web service via the `DATABASE_URL` environment variable reference (`${{Postgres.DATABASE_URL}}`).
3. **Configure Environment Variables**:
   - `DATABASE_URL`: `${{Postgres.DATABASE_URL}}`
   - `API_KEY`: A secure random secret for job trigger authentication.
   - `READ_API_KEY`: (Optional) Secret key to restrict read endpoints.
   - `DISCOVERY_CRON`: `0 2 * * *` (nightly at 02:00 UTC)
   - `STOCK_CRON`: `0 */4 * * *` (every 4 hours)
4. **Deploy Background Worker**:
   - In Railway, add another service from the same GitHub repo.
   - Set the start command in settings to: `python -m app.worker`.
   - Set the same environment variables.

> **Important Deployment Note**: Digisound web servers employ an Apache 403 geo-block on non-EU IP addresses. When creating your Railway project, select an EU region (e.g. Frankfurt, Germany / Amsterdam, Netherlands) so outbound requests reach Digisound without network-level access blocks.

---

## Documentation

- [WordPress Integration Guide](docs/wordpress-integration.md): Exact SKU lookup, 409 handling, and delta feed consumption.
- [Source Findings & Limitations](docs/limitations.md): Creamarketing WebAdmin 7 details, filter facets analysis, and geo-blocking behavior.
- [Deployment Evidence](docs/deploy-evidence.md): Migration verification, test suite evidence, and curl testing commands.
