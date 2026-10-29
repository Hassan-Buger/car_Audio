"""HTML parser for product detail pages.

The live Digisound detail page publishes a schema.org ``Product`` JSON-LD block (name, SKU, brand,
offer price/currency/availability, images); that block is the preferred source. HTML selectors
fill the gaps (EAN, specifications, descriptions, breadcrumbs) and keep older markup working.
"""

import json
import re
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from app.scraper.models import ParsedProductDetail

_AVAILABILITY_TEXT = {
    "instock": "In stock",
    "outofstock": "Out of stock",
    "preorder": "Pre-order",
    "backorder": "Backorder",
    "soldout": "Out of stock",
    "limitedavailability": "Limited availability",
}


def _product_jsonld(soup: BeautifulSoup) -> dict[str, Any]:
    """Return the first schema.org Product object found in JSON-LD blocks (or an empty dict)."""
    for block in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = block.string or block.get_text()
        if not raw or not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        candidates = data if isinstance(data, list) else [data]
        for cand in candidates:
            if isinstance(cand, dict) and cand.get("@type") == "Product":
                return cand
    return {}


def _format_price(value: Any, currency: str | None) -> str | None:
    """Render a JSON-LD numeric price in the site's display style, e.g. ``249,00 €``."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", "."))
    except ValueError:
        return None
    text = f"{number:.2f}".replace(".", ",")
    return f"{text} €" if (currency or "EUR").upper() == "EUR" else f"{text} {currency}"


def _parse_specs(soup: BeautifulSoup) -> list[dict[str, str]]:
    specifications: list[dict[str, str]] = []
    # Live markup: <div class="content-section specs"><div class="block-content"><p>Name: value<br/>...
    for block in soup.select(".content-section.specs .block-content"):
        for br in block.find_all("br"):
            br.replace_with("\n")
        for line in block.get_text("\n").splitlines():
            name, sep, value = line.partition(":")
            name, value = name.strip(), value.strip()
            if sep and name and value:
                specifications.append({"name": name, "value": value})
    if specifications:
        return specifications
    # Legacy table / definition-list markup
    for row in soup.select("table.specs-table tr, .specs-list tr, dl.specs"):
        if isinstance(row, Tag):
            name_el = row.select_one(".name, th, dt, td:first-child")
            val_el = row.select_one(".value, td:last-child, dd")
            if name_el and val_el:
                s_name = name_el.get_text(strip=True)
                s_val = val_el.get_text(strip=True)
                if s_name and s_val and s_name.lower() != "parameeter":
                    specifications.append({"name": s_name, "value": s_val})
    return specifications


def parse_detail_page(
    html_text: str, fallback_url: str = "", base_url: str = "https://www.digisound.ee"
) -> ParsedProductDetail:
    """Parse product detail page markup into structured fields."""
    soup = BeautifulSoup(html_text, "lxml")
    ld = _product_jsonld(soup)
    offer_raw = ld.get("offers")
    offer: dict[str, Any] = (
        offer_raw[0] if isinstance(offer_raw, list) and offer_raw else offer_raw or {}
    )
    if not isinstance(offer, dict):
        offer = {}

    # Canonical URL
    canonical_link = soup.select_one("link[rel='canonical']")
    if canonical_link and canonical_link.get("href"):
        canonical_url = str(canonical_link.get("href"))
    else:
        canonical_url = fallback_url or str(ld.get("url") or "") or base_url
    canonical_url = urljoin(base_url, canonical_url)

    # Slug & source product id (the URL slug is the stable identity)
    parsed_path = urlparse(canonical_url).path.strip("/").split("/")
    slug = parsed_path[-1] if parsed_path else "unknown-product"
    source_product_id = slug

    # Title
    title_el = soup.select_one("h1.title, h1.product-title, .product-title, h1")
    title = str(ld.get("name") or "").strip() or (title_el.get_text(strip=True) if title_el else "")
    title = title or slug

    # Brand: JSON-LD only. The ``.brand`` class on the live site is the site logo, not a product
    # brand, so it is never used.
    brand_ld = ld.get("brand")
    brand: str | None = None
    if isinstance(brand_ld, dict):
        brand = str(brand_ld.get("name") or "").strip() or None
    elif isinstance(brand_ld, str):
        brand = brand_ld.strip() or None
    if brand is None:
        legacy = soup.select_one(
            ".product-brand, [itemprop='brand'], div.brand, span.brand, p.brand"
        )
        brand = legacy.get_text(strip=True) if legacy else None

    # SKU: exact text from the page (spaces, slashes preserved); the slug is the last resort.
    sku = str(ld.get("sku") or "").strip()
    if not sku:
        sku_el = soup.select_one(
            ".product-reference .InternalItemID, .product-reference, "
            ".sku, span.sku, .product-sku, [itemprop='sku']"
        )
        if sku_el:
            sku = sku_el.get_text(strip=True)
    if not sku:
        match = re.search(r"(?:Tootekood|SKU|Kood)[:\s]+([^\n<]+)", soup.get_text())
        sku = match.group(1).strip() if match else slug

    # EAN
    ean_raw = None
    ean_el = soup.select_one(".product-ean, .ean, span.ean, .barcode, [itemprop='gtin13']")
    ean_text = ean_el.get_text(" ", strip=True) if ean_el else ""
    ean_match = re.search(r"\d{8,14}", ean_text)
    if ean_match:
        ean_raw = ean_match.group(0)
    else:
        match = re.search(r"(?:EAN|GTIN|Vöötkood)[:\s]+([0-9]{8,14})", soup.get_text())
        if match:
            ean_raw = match.group(1).strip()
    if ean_raw is None:
        for key in ("gtin13", "gtin", "gtin14", "gtin8"):
            if ld.get(key):
                ean_raw = str(ld[key])
                break

    # Pricing. Only an explicit price is returned; the VAT basis comes solely from page wording.
    currency = str(offer.get("priceCurrency") or "") or None
    regular_price_raw = _format_price(offer.get("price"), currency)
    if regular_price_raw is None:
        price_el = soup.select_one(".price-current, .price-box .price, .product-price")
        regular_price_raw = price_el.get_text(strip=True) if price_el else None

    sale_el = soup.select_one(".price-sale, .sale-price")
    sale_price_raw = sale_el.get_text(strip=True) if sale_el else None

    vat_el = soup.select_one(".vat-basis, .tax-text, .price-tax")
    price_tax_text = vat_el.get_text(strip=True) if vat_el else None

    # Stock / availability
    availability = str(offer.get("availability") or "").rsplit("/", 1)[-1].strip()
    stock_text = _AVAILABILITY_TEXT.get(availability.lower()) if availability else None
    if stock_text is None:
        stock_el = soup.select_one(".stock-status, .stock-info, .availability")
        stock_text = stock_el.get_text(strip=True) if stock_el else None
    availability_text = stock_text

    # Descriptions
    short_el = soup.select_one(".short-description, .summary, .product-summary")
    short_description = short_el.get_text(strip=True) if short_el else None
    if short_description is None and ld.get("description"):
        short_description = str(ld["description"]).strip() or None

    long_el = soup.select_one(
        "#description, .tab-content#description, .product-description, "
        ".content-section.description .block-content, .content-section.description"
    )
    long_description = long_el.get_text(" ", strip=True) if long_el else None
    if long_description:
        long_description = re.sub(r"^Description\s+", "", long_description) or None

    specifications = _parse_specs(soup)

    # Images
    images: list[str] = []
    ld_images = ld.get("image")
    if isinstance(ld_images, str):
        ld_images = [ld_images]
    for src in ld_images if isinstance(ld_images, list) else []:
        if isinstance(src, str) and src:
            images.append(urljoin(base_url, src))
    if not images:
        for el in soup.select(
            ".product-slideshow-slide img, .product-gallery img, .thumbnails a, img.main-image"
        ):
            if isinstance(el, Tag):
                src = el.get("href") or el.get("data-src") or el.get("src")
                if src:
                    images.append(urljoin(base_url, str(src)))
    images = list(dict.fromkeys(images))

    # Breadcrumbs: names for legacy callers, (name, url) pairs for the real category path.
    category_breadcrumbs: list[str] = []
    category_crumbs: list[tuple[str, str]] = []
    for link in soup.select(".product-breadcrumbs a, .breadcrumb a, nav.breadcrumb a"):
        if isinstance(link, Tag):
            txt = link.get_text(strip=True)
            href = str(link.get("href") or "").strip()
            if txt and txt.lower() not in ("avaleht", "home", "products"):
                category_breadcrumbs.append(txt)
                if href:
                    category_crumbs.append((txt, urljoin(base_url, href)))

    return ParsedProductDetail(
        source_product_id=source_product_id,
        sku=sku,
        title=title,
        brand=brand,
        ean_raw=ean_raw,
        canonical_url=canonical_url,
        regular_price_raw=regular_price_raw,
        sale_price_raw=sale_price_raw,
        price_tax_text=price_tax_text,
        stock_text=stock_text,
        availability_text=availability_text,
        short_description=short_description,
        long_description=long_description,
        specifications=specifications,
        images=images,
        category_breadcrumbs=category_breadcrumbs,
        category_crumbs=category_crumbs,
    )
