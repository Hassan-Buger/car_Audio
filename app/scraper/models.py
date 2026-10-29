"""Typed models for raw parsed scraper outputs."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ParsedProductSummary:
    """Product summary extracted from a category listing card."""

    source_product_id: str
    slug: str
    sku: str
    title: str
    brand: str | None
    url: str
    price_raw: str | None
    stock_text: str | None
    spec_summary: str | None = None
    image_url: str | None = None


@dataclass(frozen=True)
class ParsedCategoryLink:
    """A child category link found in an explicit category-navigation block."""

    name: str
    url: str
    slug: str
    count_hint: int | None = None


@dataclass(frozen=True)
class ParsedListingPage:
    """Listing page output including extracted products and pagination markers."""

    products: list[ParsedProductSummary]
    reported_total: int | None
    next_start: int | None
    has_next_page: bool
    title: str | None = None
    subcategories: list[ParsedCategoryLink] = field(default_factory=list)


@dataclass(frozen=True)
class ParsedProductDetail:
    """Full product detail extracted from a product page."""

    source_product_id: str
    sku: str
    title: str
    brand: str | None
    ean_raw: str | None
    canonical_url: str
    regular_price_raw: str | None
    sale_price_raw: str | None
    price_tax_text: str | None
    stock_text: str | None
    availability_text: str | None
    short_description: str | None
    long_description: str | None
    specifications: list[dict[str, str]] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    category_breadcrumbs: list[str] = field(default_factory=list)
    category_crumbs: list[tuple[str, str]] = field(default_factory=list)  # (name, absolute url)
