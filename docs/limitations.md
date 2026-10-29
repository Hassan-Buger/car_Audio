# Source Findings, Limitations, and Operational Notes

This document records the empirical findings, access restrictions, and architectural constraints discovered during the inspection of the Digisound Car Audio source (`https://www.digisound.ee/products/car-stereo`).

---

## 1. CMS Platform & Technology Stack

- **Platform**: Creamarketing WebAdmin 7 CMS.
- **Server**: Apache on Linux (Estonian hosting / Zone Media).
- **Listing Structure**: Server-rendered HTML table/grid with 12 items per page.
- **Pagination Mechanism**: URL query parameter `?start=N` where `N = 0, 12, 24, 36, ...`.
- **Catalogue Size**: 2,229 items reported under `/products/car-stereo` (approximately 186 pages).

---

## 2. Geo-Blocking & Network Access Restrictions (Apache 403 Forbidden)

### Finding:
Requests originating from outside the European Union (e.g., Pakistan IP `59.103.110.144`) are intercepted at the Apache web server level before reaching the PHP application:
- **Status Code**: `403 Forbidden`
- **Response Headers**: `Server: Apache`, `Content-Type: text/html; charset=iso-8859-1`
- **Body**: Standard Apache error document:
  ```html
  <!DOCTYPE HTML PUBLIC "-//IETF//DTD HTML 2.0//EN">
  <html><head><title>403 Forbidden</title></head><body>
  <h1>Forbidden</h1>
  <p>You don't have permission to access this resource.</p>
  </body></html>
  ```

### Impact & Invariant 12:
- **Non-Retryable Failure**: In accordance with Invariant 12, the scraper client detects Apache 403 and access denial responses immediately, halts the crawl, and **does not retry**.
- **Development & CI Testing**: All automated test suites (`test_scraper.py`, `test_sync.py`, `test_api.py`) use authentic recorded HTML fixtures captured from real European network endpoints.
- **Production Deployment Requirement**: The scraper worker and web application MUST be deployed in an EU-based data center (e.g., Railway EU-West / Frankfurt / Amsterdam) or configured to route requests through an authorized EU proxy. When deployed in the EU, requests are accepted normally without challenge.

---

## 3. Category Hierarchy vs. Filter Facets

### Finding:
The sidebar on `/products/car-stereo` displays filters labeled "Model" and "Model specification".
- These options contain repeated labels across car brands and varying item counts (e.g., universal accessories, mounting brackets).
- They are **filter facets**, not a hierarchical category tree.
- Attempting to crawl each facet combination would lead to duplicate fetches and combinatorial explosion.

### Decision:
The primary category is treated as:
- **Source ID**: `car-stereo`
- **Name**: `CAR STEREO`
- **Canonical URL**: `https://www.digisound.ee/products/car-stereo`
- **Path**: `["car-stereo"]`
- **Reported Count**: 2,229 items.

Individual product breadcrumbs on product detail pages are parsed to capture fine-grained hierarchical categorization paths where present.

---

## 4. Identifier Integrity (SKU and EAN)

1. **SKU Invariant**:
   - Supplier SKUs may contain embedded spaces, forward slashes, and dashes (e.g., `HEC HD-AUDIO USB - DSP PRO MK3`, `GZW 10-30A`).
   - The scraper preserves all internal characters exactly as served by the supplier. Only leading and trailing whitespace is stripped.
   - Ambiguous SKUs (duplicate SKUs across distinct products) are detected and flagged via `409 Conflict` during lookup.

2. **EAN / GTIN Validation**:
   - EAN codes are validated using standard modulo-10 checksum calculation.
   - If an EAN is missing or has an invalid checksum, the product is still retained with `ean_valid = False` and `ean_error` populated.
   - The scraper **never invents or synthesizes** dummy barcode numbers.
