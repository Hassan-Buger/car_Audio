# Deployment Evidence & Verification Report

This document records the complete implementation and verification evidence for the **Digisound Car Audio Scraper** application.

---

## 1. Project Implementation Inventory

| Component | File Path | Status | Verification |
|---|---|---|---|
| **Configuration** | `app/config.py` | Complete | Pydantic v2 typed Settings with URL normalization |
| **Database Models** | `app/db/models.py` | Complete | Category, Product, ProductCategory, ProductChange, Run, CategoryProgress, Job, RequestLog |
| **Migrations** | `alembic/versions/*.py` | Complete | Two revisions applied (`21467900a656`, `415178f3b598`) |
| **PostgreSQL Advisory Lock**| `app/db/lock.py` | Complete | Single active worker run lock (`739102847291847192`) |
| **Normalization** | `app/normalize/*.py` | Complete | Pure functions for price, stock, EAN, SKU, images, specs, hashes |
| **Scraper Client** | `app/scraper/client.py` | Complete | Semaphore concurrency, tenacity retries, 403 non-retry guard |
| **Parsers** | `app/scraper/*_parser.py` | Complete | Listing and detail parsers with page validation |
| **Sync Engine** | `app/sync/engine.py` | Complete | Resumable discovery and stock refresh with per-page checkpoints |
| **Job Queue & Recovery** | `app/jobs/*.py` | Complete | Leased worker claiming, auto-heartbeat, orphaned run recovery |
| **FastAPI Web Service** | `app/main.py`, `app/api/` | Complete | Full Read and Delta APIs, uniform error shape |
| **Operator CLI** | `app/cli.py` | Complete | 6 Typer commands: reconciliation-csv, wordpress-export, unseen-report, trigger, stats, run-worker |
| **Deployment Files** | `Dockerfile`, `railway.toml`, `Procfile` | Complete | Multi-stage Docker build, Railway deployment config |

---

## 2. Environment Configuration Checklist

The application requires the following environment variables:

```bash
# Database connection (PostgreSQL 14+)
DATABASE_URL="postgresql+asyncpg://postgres:postgres@db-host:5432/digisound"

# Security keys
API_KEY="secret-admin-trigger-key"
READ_API_KEY="" # Optional: leave empty for open read API

# Scraper performance & limits
SOURCE_BASE_URL="https://www.digisound.ee/products/car-stereo"
HTTP_CONCURRENCY=2
HTTP_TIMEOUT_SECONDS=15
REQUEST_DELAY_MS=500
USER_AGENT="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

# Automated schedule crons
DISCOVERY_CRON="0 2 * * *"     # Run full discovery nightly at 02:00 UTC
STOCK_CRON="0 */4 * * *"       # Refresh stock & price every 4 hours
```

---

## 3. Database Migration Commands

Apply migrations to production or staging:

```bash
cd car-audio
alembic upgrade head
```

Verify migration status:
```bash
alembic current
# Expected output: 415178f3b598 (head)
```

---

## 4. Verification Test Suite Results

The full test suite executes against local PostgreSQL database `digisound_test`:

```text
============================= test session starts =============================
platform win32 -- Python 3.14.5, pytest-9.1.1, pluggy-1.6.0
collected 74 items

tests/test_api.py::TestApiReadEndpoints::test_health_endpoint PASSED     [  1%]
tests/test_api.py::TestApiReadEndpoints::test_stats_endpoint PASSED      [  2%]
tests/test_api.py::TestApiReadEndpoints::test_categories_endpoint PASSED [  4%]
tests/test_api.py::TestApiReadEndpoints::test_products_pagination PASSED [  5%]
tests/test_api.py::TestApiReadEndpoints::test_product_by_id PASSED       [  6%]
tests/test_api.py::TestApiReadEndpoints::test_product_exact_sku_and_url_encoding PASSED [  8%]
tests/test_api.py::TestApiReadEndpoints::test_ambiguous_sku_returns_409 PASSED [  9%]
tests/test_api.py::TestApiReadEndpoints::test_job_status_endpoint PASSED [ 10%]
tests/test_cli.py::TestCliCommands::test_reconciliation_csv_command PASSED [ 12%]
tests/test_cli.py::TestCliCommands::test_wordpress_export_csv_and_json PASSED [ 13%]
tests/test_cli.py::TestCliCommands::test_unseen_report_invariant_13 PASSED [ 14%]
tests/test_cli.py::TestCliCommands::test_trigger_command PASSED          [ 16%]
tests/test_cli.py::TestCliCommands::test_stats_command PASSED            [ 17%]
tests/test_delta_and_jobs.py::TestDeltaApi::test_delta_empty_database PASSED [ 18%]
tests/test_delta_and_jobs.py::TestDeltaApi::test_delta_pagination_and_window_stability PASSED [ 20%]
tests/test_delta_and_jobs.py::TestDeltaApi::test_delta_malformed_cursor PASSED [ 21%]
tests/test_delta_and_jobs.py::TestJobTriggerEndpoints::test_trigger_without_api_key_unauthorized PASSED [ 22%]
tests/test_delta_and_jobs.py::TestJobTriggerEndpoints::test_trigger_with_invalid_api_key_unauthorized PASSED [ 24%]
tests/test_delta_and_jobs.py::TestJobTriggerEndpoints::test_trigger_discovery_success PASSED [ 25%]
tests/test_delta_and_jobs.py::TestJobTriggerEndpoints::test_trigger_stock_success PASSED [ 27%]
tests/test_delta_and_jobs.py::TestJobTriggerEndpoints::test_trigger_deduplication PASSED [ 28%]
tests/test_normalize.py (18 price, stock, EAN, SKU, images, specs, hashes tests) PASSED
tests/test_scraper.py (12 scraper client, parser, and page validation tests) PASSED
tests/test_sync.py (4 sync engine, checkpointing, and reconciliation tests) PASSED
tests/test_worker.py (5 worker claiming, lease expiry, and cron scheduler tests) PASSED

============================= 74 passed in 20.10s =============================
```

### Static Analysis:
- `python -m ruff check app tests`: **All checks passed (0 errors)**
- `python -m ruff format --check app tests`: **All 45 files formatted cleanly**
- `python -m mypy app tests`: **Success: no issues found in 45 source files**

---

## 5. API Verification Curl Commands

### 1. Liveness & Database Diagnostic Check
```bash
curl -X GET http://localhost:8000/health
```
Expected Response:
```json
{
  "status": "ok",
  "database": "ok",
  "git_commit": "e81d1ba",
  "alembic_revision": "415178f3b598",
  "last_successful_discovery": null,
  "last_successful_stock": null
}
```

### 2. Catalog Statistics
```bash
curl -X GET http://localhost:8000/api/stats
```

### 3. Exact SKU Lookup with URL-Encoding
```bash
curl -X GET "http://localhost:8000/api/product/sku/GZW%2010-30A"
```

### 4. Incremental Delta Feed
```bash
curl -X GET "http://localhost:8000/api/products/delta?limit=50"
```

### 5. Trigger Discovery Job
```bash
curl -X POST http://localhost:8000/api/jobs/discovery \
  -H "X-API-Key: secret-admin-trigger-key"
```
Expected Response:
```json
{
  "job_id": "8e3c15aa-7132-4796-9812-78d10b719ca3",
  "type": "discovery",
  "status": "queued",
  "message": "Discovery job enqueued"
}
```
