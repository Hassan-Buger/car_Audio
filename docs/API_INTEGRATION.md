# Digisound catalog API: integration guide

Read-only catalog API over the Digisound **CAR STEREO** tree (`source = "digisound"`), kept fresh by
scheduled crawls. It does not write to WordPress or to any other system; a consumer pulls from it.

`<API_URL>` below is **https://caraudio-production-8a3c.up.railway.app** (the Railway public domain of the `car_Audio` service). Interactive reference: `<API_URL>/docs` (OpenAPI at
`<API_URL>/openapi.json`; these describe the API only and contain no catalog data).

## Authentication

| Endpoints | Header | Server variable | If the variable is unset |
|-----------|--------|-----------------|--------------------------|
| All `GET /api/...` read endpoints | `X-API-Key: <read key>` | `READ_API_KEY` | endpoints are open |
| `POST /api/jobs/discovery`, `POST /api/jobs/stock` | `X-API-Key: <job key>` | `API_KEY` | HTTP 503 `service_unavailable` |
| `GET /api/jobs/{job_id}` | `X-API-Key: <read key>` | `READ_API_KEY` | open |
| `GET /live`, `GET /health` | none | none | always open |

The two keys are separate: the read key cannot start crawls. Use letters and digits only when generating a key (header values with other characters are not transmitted reliably). Keep both server-side in your
integration (never in browser code). Keys are compared in constant time. Rotating a key means
changing the Railway variable and redeploying; nothing else depends on it.

## Errors

Every error uses one envelope, including validation errors:

```json
{"error": {"code": "unauthorized", "message": "Invalid or missing read API key", "details": {}}}
```

| HTTP | `code` | Meaning |
|------|--------|---------|
| 400 | `invalid_cursor` | cursor was not produced by this API |
| 401 | `unauthorized` | missing or wrong `X-API-Key` |
| 404 | `not_found` | unknown product, category or job |
| 409 | `ambiguous_sku` | more than one product has that SKU; `details.candidates[]` lists them |
| 422 | `validation_error` | bad query parameter (e.g. `limit=0`) |
| 503 | `service_unavailable` / `/health` degraded | job key not configured, or database not ready |

## Endpoints

### Catalog

| Method and path | Purpose |
|-----------------|---------|
| `GET /api/stats` | totals: categories, products, in-stock, out-of-stock, backorder, latest finished discovery and stock run timestamps |
| `GET /api/categories` | all categories with `source_id`, `ordered_path`, `canonical_url`, `source_reported_count` |
| `GET /api/category/{id}/products?cursor=&limit=` | products of one category (`id` is the numeric `id` from `/api/categories`) |
| `GET /api/products?cursor=&limit=` | the full catalog, `limit` 1-200 (default 50) |
| `GET /api/product/id/{source_product_id}` | one product by Digisound slug |
| `GET /api/product/sku/{sku}` | one product by exact SKU |

**Pagination.** Responses carry `items`, `has_more`, `next_cursor`; `/api/products` also returns
`total_count` on the first page. Pass `next_cursor` back as `cursor` until `has_more` is `false`.
Cursors are opaque; do not build them. A full walk with `limit=200` is about 12 requests.

**SKUs.** Matching is exact (case, spaces, slashes). URL-encode the SKU:
`/api/product/sku/GZW%2010-30A`, `/api/product/sku/GZFU%2080A%2F2%20MANL`. A duplicate SKU returns
409 with candidates; then look the product up by `source_product_id`. Prefer `source_product_id`
as your stable key: SKUs on the source site are not guaranteed unique.

**Product fields.** `source_product_id`, `sku`, `title`, `brand`, `ean_raw`, `ean_valid`,
`ean_error`, `canonical_url`, `currency` (`EUR`), `regular_price`, `sale_price`,
`price_tax_basis`, `stock_status`, `stock_quantity`, `availability_text`, descriptions,
`specifications[]`, `images[]`, `category_paths` (every category path the product is listed in,
as arrays of names), `source_category_ids`, `content_hash`, `commerce_hash`, and timestamps
(`first_seen_at`, `last_seen_at`, `last_checked_at`, `content_updated_at`, `stock_updated_at`).

**Prices.** Digisound's pages do not state whether prices include VAT, so
`price_tax_basis` is `"unknown"`. Confirm the basis with the supplier before applying margins or
VAT in a shop; the API does not guess. `null` price means the site showed none.

**Stock.** `stock_status` is one of:

| Value | Source wording (listing card) | Notes |
|-------|-------------------------------|-------|
| `in_stock` | "In stock", "In stock (outgoing model)" | `stock_quantity` is set only when the site shows a number |
| `backorder` | "Back order", "Arriving in stock: <date>" | orderable, not on the shelf |
| `out_of_stock` | "Temporarily out of stock", "Discontinued product" | "Discontinued" is mapped to out of stock |
| `unknown` | anything unrecognised | the raw wording is always kept in `availability_text` |

The category listing card is the source of truth for the wording; the detail page's schema.org
flag is coarser (it says "InStock" for back-order items) and is used only if the card has no text.
`availability_text` is always the site's own wording, so you can apply your own mapping.

### Incremental sync (recommended for keeping a shop up to date)

`GET /api/products/delta?from_seq=<n>&limit=<1-500>` returns changes with `seq > n`, oldest first.

1. First run: `from_seq=0`, or skip it and import via `/api/products`, then start the feed from the
   `window_until` of a first delta call.
2. Page through the window using `next_cursor` as `cursor` until `has_more` is `false`.
3. Store the response's `window_until` and use it as the next `from_seq`.

The window is fixed when the first page is read: changes committed meanwhile do not leak into it,
and the same cursor always returns the same page. Each item has `change_type`
(`created`, `content_updated`, `commerce_updated`, `both_updated`, `categories_updated`),
`changed_fields`, and `new_data`. `commerce_updated` is a price or stock change;
`categories_updated` means only category membership changed. Handle repeated items idempotently
(upsert by `source_product_id`). Products are never deleted or deactivated by the crawler, so the
feed has no delete events; a product that disappears from the site simply stops updating
(`last_seen_at` stops moving).

### Jobs

| Method and path | Purpose |
|-----------------|---------|
| `POST /api/jobs/stock` | enqueue a stock/price refresh (about 7 minutes) |
| `POST /api/jobs/discovery` | enqueue a full discovery crawl (about 70 minutes) |
| `GET /api/jobs/{job_id}` | status: `queued`, `running`, `completed`, `failed`; `result_summary` when done |

Both POSTs return `202` with `job_id`. If a job of that type is already queued or running, you get
that job back (deduplicated), so repeated calls are safe. The crawl is deliberately gentle
(low concurrency, request delay); do not trigger discovery more than needed.

### Health

`GET /live` is liveness only. `GET /health` reports database connectivity, deployed commit,
applied vs expected migration revision, and last successful discovery and stock times; it returns
503 when degraded. `/health` makes no request to the source site.

## Schedules (UTC)

| Job | Cron | Notes |
|-----|------|-------|
| Stock refresh | `0 */4 * * *` | every 4 hours |
| Discovery | `0 2 * * *` | nightly, about 70 minutes |

Scheduling is stateless and the queue is database-backed: a restart or redeploy resumes the
interrupted job from its per-category checkpoint under the same run id, and produces at most one
catch-up job. Change timing with `STOCK_CRON`, `DISCOVERY_CRON`, `SCHEDULE_TIMEZONE`.

## Examples

```bash
curl -s "$API_URL/live"
curl -s -H "X-API-Key: $READ_KEY" "$API_URL/api/stats"
curl -s -H "X-API-Key: $READ_KEY" "$API_URL/api/products?limit=200"
curl -s -H "X-API-Key: $READ_KEY" "$API_URL/api/product/sku/GZW%2010-30A"
curl -s -H "X-API-Key: $READ_KEY" "$API_URL/api/products/delta?from_seq=0&limit=500"
curl -s -X POST -H "X-API-Key: $JOB_KEY" "$API_URL/api/jobs/stock"
```

```python
import httpx

def walk_products(api_url: str, read_key: str):
    cursor = None
    with httpx.Client(base_url=api_url, headers={"X-API-Key": read_key}, timeout=30) as c:
        while True:
            r = c.get("/api/products", params={"limit": 200, **({"cursor": cursor} if cursor else {})})
            r.raise_for_status()
            page = r.json()
            yield from page["items"]
            if not page["has_more"]:
                return
            cursor = page["next_cursor"]
```

## Operating notes

- The API serves what is in PostgreSQL; it never calls Digisound while answering a request.
- Digisound blocks some regions with HTTP 403. A blocked crawl fails visibly; the crawler does not
  bypass access restrictions. The deployment region (US West) is currently served.
- Catalog size at the last full discovery: 2229 products in 203 categories, all categories
  reconciled against the source's own counts.
