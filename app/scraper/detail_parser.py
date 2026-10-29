"""HTML parser for product detail pages."""

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from app.scraper.models import ParsedProductDetail


def parse_detail_page(
    html_text: str, fallback_url: str = "", base_url: str = "https://www.digisound.ee"
) -> ParsedProductDetail:
    """Parse product detail page markup into structured fields."""
    soup = BeautifulSoup(html_text, "lxml")

    # Canonical URL
    canonical_link = soup.select_one("link[rel='canonical']")
    if canonical_link and canonical_link.get("href"):
        canonical_url = str(canonical_link.get("href"))
    else:
        canonical_url = fallback_url or base_url

    canonical_url = urljoin(base_url, canonical_url)

    # Slug & source product id
    parsed_path = urlparse(canonical_url).path.strip("/").split("/")
    slug = parsed_path[-1] if parsed_path else "unknown-product"
    source_product_id = slug

    # Title
    title_el = soup.select_one("h1.title, h1.product-title, h1")
    title = title_el.get_text(strip=True) if title_el else slug

    # Brand
    brand_el = soup.select_one(".brand, .product-brand, [itemprop='brand']")
    brand = brand_el.get_text(strip=True) if brand_el else None

    # SKU
    sku = ""
    sku_el = soup.select_one(".sku, span.sku, .product-sku, [itemprop='sku']")
    if sku_el:
        sku = sku_el.get_text(strip=True)
    else:
        # Search for Tootekood / SKU text
        match = re.search(r"(?:Tootekood|SKU|Kood)[:\s]+([^\n<]+)", soup.get_text())
        if match:
            sku = match.group(1).strip()
        else:
            sku = slug

    # EAN
    ean_raw = None
    ean_el = soup.select_one(".ean, span.ean, .barcode, [itemprop='gtin13']")
    if ean_el:
        ean_raw = ean_el.get_text(strip=True)
    else:
        match = re.search(r"(?:EAN|GTIN|Vöötkood)[:\s]+([0-9]{8,14})", soup.get_text())
        if match:
            ean_raw = match.group(1).strip()

    # Pricing & VAT
    price_el = soup.select_one(".price-current, .price-box .price, .product-price")
    regular_price_raw = price_el.get_text(strip=True) if price_el else None

    sale_el = soup.select_one(".price-sale, .sale-price")
    sale_price_raw = sale_el.get_text(strip=True) if sale_el else None

    vat_el = soup.select_one(".vat-basis, .tax-text, .price-tax")
    price_tax_text = vat_el.get_text(strip=True) if vat_el else None

    # Stock
    stock_el = soup.select_one(".stock-status, .stock-info, .availability")
    stock_text = stock_el.get_text(strip=True) if stock_el else None
    availability_text = stock_text

    # Descriptions
    short_el = soup.select_one(".short-description, .summary, .product-summary")
    short_description = short_el.get_text(strip=True) if short_el else None

    long_el = soup.select_one("#description, .tab-content#description, .product-description")
    long_description = long_el.get_text(strip=True) if long_el else None

    # Specifications
    specifications: list[dict[str, str]] = []
    spec_rows = soup.select("table.specs-table tr, .specs-list tr, dl.specs")
    for row in spec_rows:
        if isinstance(row, Tag):
            name_el = row.select_one(".name, th, dt, td:first-child")
            val_el = row.select_one(".value, td:last-child, dd")
            if name_el and val_el:
                s_name = name_el.get_text(strip=True)
                s_val = val_el.get_text(strip=True)
                if s_name and s_val and s_name.lower() != "parameeter":
                    specifications.append({"name": s_name, "value": s_val})

    # Images
    images: list[str] = []
    # Collect main image and thumbnails
    gallery_imgs = soup.select(".product-gallery img, .thumbnails a, img.main-image")
    for el in gallery_imgs:
        if isinstance(el, Tag):
            src = el.get("href") or el.get("data-src") or el.get("src")
            if src:
                images.append(urljoin(base_url, str(src)))

    # Breadcrumbs
    category_breadcrumbs: list[str] = []
    crumb_links = soup.select(".breadcrumb a, nav.breadcrumb a")
    for link in crumb_links:
        if isinstance(link, Tag):
            txt = link.get_text(strip=True)
            if txt and txt.lower() not in ("avaleht", "home"):
                category_breadcrumbs.append(txt)

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
    )
