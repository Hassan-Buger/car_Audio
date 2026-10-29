"""HTML parser for category listing pages."""

import re
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from app.scraper.models import ParsedCategoryLink, ParsedListingPage, ParsedProductSummary

# Explicit category-navigation blocks. Manufacturer / attribute / model filters are NOT categories
# and are deliberately not matched by these selectors.
CATEGORY_NAV_SELECTOR = (
    ".category-tree a, .subcategories a, .category-list a, "
    ".sidebar-categories a, nav.categories a, .category-navigation a"
)
_FACET_HINTS = ("manufacturers", "attributes", "filter", "sort=", "order=", "page=", "start=")


def parse_category_links(
    soup: BeautifulSoup, parent_url: str, base_url: str = "https://www.digisound.ee"
) -> list[ParsedCategoryLink]:
    """Extract child category links from explicit category navigation blocks.

    Only links whose path is strictly below ``parent_url`` are returned; filter/facet links
    (manufacturer, attribute, model, sort, pagination) are ignored. Returns an empty list when
    the page has no explicit category navigation - category structure is never guessed.
    """
    parent_path = urlparse(urljoin(base_url, parent_url)).path.rstrip("/")
    seen: set[str] = set()
    links: list[ParsedCategoryLink] = []
    for anchor in soup.select(CATEGORY_NAV_SELECTOR):
        if not isinstance(anchor, Tag):
            continue
        href = str(anchor.get("href", "")).strip()
        if not href or any(hint in href.lower() for hint in _FACET_HINTS):
            continue
        full = urljoin(base_url, href)
        parsed = urlparse(full)
        path = parsed.path.rstrip("/")
        if parsed.query or not path.startswith(parent_path + "/"):
            continue
        canonical = f"{parsed.scheme}://{parsed.netloc}{path}"
        if canonical in seen:
            continue
        seen.add(canonical)
        raw_name = anchor.get_text(" ", strip=True)
        count_hint: int | None = None
        count_match = re.search(r"\((\d+)\)\s*$", raw_name)
        if count_match:
            count_hint = int(count_match.group(1))
            raw_name = raw_name[: count_match.start()].strip()
        slug = path.rsplit("/", 1)[-1]
        links.append(
            ParsedCategoryLink(
                name=raw_name or slug, url=canonical, slug=slug, count_hint=count_hint
            )
        )
    return links


def parse_reported_total(soup: BeautifulSoup) -> int | None:
    """Extract source-reported total product count (e.g. '2229 Products')."""
    # Look for dedicated counter element
    count_el = soup.select_one(".total-products-count, .product-count, .results-count")
    if count_el:
        text = count_el.get_text(strip=True)
        match = re.search(r"(\d+)", text)
        if match:
            return int(match.group(1))

    # Search headings and paragraphs for e.g. '2229 Products' or '2229 toodet'
    text_content = soup.get_text()
    match = re.search(r"(\d+)\s*(?:Products|toodet|Toodet)", text_content, re.IGNORECASE)
    if match:
        return int(match.group(1))

    return None


def parse_listing_page(
    html_text: str,
    base_url: str = "https://www.digisound.ee",
    category_url: str | None = None,
) -> ParsedListingPage:
    """Parse a category listing page into structured products and pagination state."""
    soup = BeautifulSoup(html_text, "lxml")

    h1 = soup.select_one(".category-header h1, h1")
    page_title = h1.get_text(strip=True) if h1 else None
    subcategories = parse_category_links(soup, category_url, base_url) if category_url else []

    reported_total = parse_reported_total(soup)

    products: list[ParsedProductSummary] = []
    # Identify product cards
    cards = soup.select(".product-card, .product-item, div[data-product-id], li.products-list-item")

    for card in cards:
        if not isinstance(card, Tag):
            continue

        # Extract title and URL
        # Legacy markup links from the title; the live site links from the image block and uses a
        # plain ``div.product-title`` for the name.
        link_el = card.select_one(
            ".product-title a, h2 a, h3 a, a.product-link, .product-image a, a[href]"
        )
        if not link_el:
            continue
        name_el = card.select_one(".product-title, h2, h3")
        title = name_el.get_text(" ", strip=True) if name_el else ""
        if not title:
            title = link_el.get_text(strip=True) or str(link_el.get("title", "")).strip()
        if not title:
            continue
        rel_url = link_el.get("href", "")
        if not rel_url:
            continue
        full_url = urljoin(base_url, str(rel_url))

        # Extract slug and source_product_id
        parsed_url = urlparse(full_url)
        path_parts = [p for p in parsed_url.path.strip("/").split("/") if p]
        slug = path_parts[-1] if path_parts else ""

        source_product_id = card.get("data-slug") or card.get("data-product-id") or slug
        if isinstance(source_product_id, list):
            source_product_id = source_product_id[0]
        source_product_id = str(source_product_id)

        # Brand
        brand_el = card.select_one(".product-brand, .brand")
        brand = brand_el.get_text(strip=True) if brand_el else None

        # SKU / Model code
        sku_el = card.select_one(".product-sku, .product-code, .sku, .code")
        sku = sku_el.get_text(strip=True) if sku_el else slug

        # Price
        price_el = card.select_one(".price-current, .product-price, .price")
        price_raw = price_el.get_text(strip=True) if price_el else None

        # Stock status text
        stock_el = card.select_one(
            ".product-stock, .stock-status, .availability, "
            ".product-availability .availability-title, .product-availability"
        )
        stock_text = stock_el.get_text(strip=True) if stock_el else None

        # Short specification
        specs_el = card.select_one(
            ".product-specs-summary, .specs-summary, .short-specs, .product-short-description"
        )
        spec_summary = specs_el.get_text(strip=True) if specs_el else None

        # Thumbnail / image
        img_el = card.select_one("img")
        img_src: str | None = None
        if img_el:
            raw_src = img_el.get("data-src") or img_el.get("src")
            if raw_src:
                if isinstance(raw_src, list):
                    raw_src = raw_src[0]
                img_src = urljoin(base_url, str(raw_src))

        products.append(
            ParsedProductSummary(
                source_product_id=source_product_id,
                slug=slug,
                sku=sku,
                title=title,
                brand=brand,
                url=full_url,
                price_raw=price_raw,
                stock_text=stock_text,
                spec_summary=spec_summary,
                image_url=img_src,
            )
        )

    # Next page pagination link detection
    # Look for e.g. <a class="load-more-btn" href="/products/car-stereo?start=12">
    load_more_link = soup.select_one(
        "a.load-more-btn, a.load-more, .pagination a[href*='start='], a[href*='?start='], a[href*='&start=']"
    )

    next_start: int | None = None
    has_next_page = False

    if load_more_link and isinstance(load_more_link, Tag):
        href = load_more_link.get("href", "")
        parsed_href = urlparse(str(href))
        query_params = parse_qs(parsed_href.query)
        if "start" in query_params:
            try:
                next_start = int(query_params["start"][0])
                has_next_page = True
            except (ValueError, IndexError):
                pass

    return ParsedListingPage(
        products=products,
        reported_total=reported_total,
        next_start=next_start,
        has_next_page=has_next_page,
        title=page_title,
        subcategories=subcategories,
    )
