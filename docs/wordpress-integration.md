# WordPress & WooCommerce Integration Guide

This guide describes how downstream WordPress and WooCommerce integrations consume normalized catalog and delta synchronization feeds from the Digisound Car Audio Scraper service.

---

## 1. Architectural Boundaries

- **Standalone Headless Scraper**: The scraper operates completely headless. It writes exclusively to its own PostgreSQL database (`digisound`).
- **No Direct WP Database Writes**: The scraper never writes to WooCommerce or WordPress tables. All downstream synchronization is consumer-driven through read APIs or operator CLI exports.
- **SoundImports Service Isolation**: This scraper operates independently of the SoundImports service and databases.

---

## 2. Product Identification & Exact SKU Lookup

### Exact SKU Matching Rules
In car audio supplier catalogs, SKUs often contain spaces, slashes, dashes, and dots (e.g., `GZW 10-30A`, `HEC HD-AUDIO USB - DSP PRO MK3`).
The scraper preserves SKUs **exactly as served by the supplier**.

When querying by SKU:
- **Endpoint**: `GET /api/product/sku/{sku:path}`
- **URL Encoding**: Because SKUs contain spaces (`%20`) and slashes (`%2F`), clients must URL-encode the SKU parameter.

#### Examples:
```bash
# Querying SKU with spaces: "GZW 10-30A"
curl -H "X-API-Key: $READ_API_KEY" \
  "https://scraper-host/api/product/sku/GZW%2010-30A"

# Querying SKU with spaces and dashes: "HEC HD-AUDIO USB - DSP PRO MK3"
curl -H "X-API-Key: $READ_API_KEY" \
  "https://scraper-host/api/product/sku/HEC%20HD-AUDIO%20USB%20-%20DSP%20PRO%20MK3"
```

### Ambiguous SKU Handling (`HTTP 409 Conflict`)
If a supplier has assigned the identical SKU to multiple distinct product records (or accessory variants), the API **refuses to select a match arbitrarily**.
Instead, it responds with:
- **Status Code**: `409 Conflict`
- **Error Code**: `ambiguous_sku`
- **Payload**:
```json
{
  "error": {
    "code": "ambiguous_sku",
    "message": "Multiple products match exact SKU 'DUP-SKU-01'",
    "details": {
      "sku": "DUP-SKU-01",
      "candidate_count": 2,
      "candidates": [
        {
          "source_product_id": "10412",
          "title": "Variant A - Black",
          "canonical_url": "https://www.digisound.ee/products/car-stereo/variant-a"
        },
        {
          "source_product_id": "10413",
          "title": "Variant B - Silver",
          "canonical_url": "https://www.digisound.ee/products/car-stereo/variant-b"
        }
      ]
    }
  }
}
```
**Client Action**: When receiving `409 Conflict`, the WooCommerce integration must fall back to resolving by `source_product_id` via `GET /api/product/id/{source_product_id}` or prompt an operator.

---

## 3. Incremental Delta Synchronization

To keep WooCommerce prices, stock, and descriptions synchronized efficiently without rescanning the full catalog, consumers should poll the delta feed.

### Endpoint: `GET /api/products/delta`

- **Parameters**:
  - `from_seq` (int, default 0): Sequence floor. Only changes strictly greater than this sequence are returned.
  - `cursor` (str, optional): Opaque base64 cursor encoding the current pagination window.
  - `limit` (int, default 50, max 500): Batch size.

### Window Stability Guarantee
The delta feed provides a **stable snapshot window**:
1. When a client initiates a sync with `from_seq`, the server snapshots `window_until = max(seq)` at that instant.
2. While the consumer paginates across subsequent pages using `next_cursor`, new mutations committed in the background receive `seq > window_until` and **do not leak** into the active window.
3. If a network interruption occurs, repeating the request with the identical `cursor` returns the exact same page deterministically.
4. Once `has_more == false`, the client records `window_until` as its new checkpoint and passes `from_seq = window_until` on its next sync cycle.

### Change Types:
- `created`: First time product discovered.
- `content_updated`: Title, brand, descriptions, specifications, or images modified.
- `commerce_updated`: Price, VAT basis, stock status, or stock quantity modified.
- `both_updated`: Both content and commerce modified.

---

## 4. Critical Data Invariants for WooCommerce Mappings

1. **Stock Quantity NULL vs 0**:
   - In Digisound, many products display text availability without a numeric inventory count (e.g., "Laos" / "Tellimisel").
   - `stock_quantity` is `NULL` when unstated by the supplier.
   - **DO NOT convert `NULL` to `0`**. Converting `NULL` to `0` will mark in-stock items as out-of-stock in WooCommerce!
   - Only set WooCommerce `stock_quantity` if `stock_quantity is not None`. Otherwise, manage availability via `stock_status` (`instock`, `outofstock`, `onbackorder`).

2. **Backorder Distinction**:
   - `backorder` is distinct from `out_of_stock`.
   - Map `stock_status == "backorder"` to WooCommerce `onbackorder`.

3. **Tax Basis**:
   - Digisound prices include standard Estonian VAT (22%) unless specified otherwise.
   - Field `price_tax_basis` is `"standard_vat"`, `"reverse_charge"`, or `"unknown"`.

4. **Preservation of Unseen Products (Invariant 13)**:
   - When a product is not returned in a crawl, **do not automatically delete or trash the WooCommerce product**.
   - Downstream importers should flag products for human review using `python -m app.cli unseen-report` or an equivalent review workflow.
