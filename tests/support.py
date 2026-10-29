"""Helpers to build synthetic listing/detail HTML for engine tests (test data only)."""

from collections.abc import Sequence
from dataclasses import dataclass

from app.config import Settings

BASE = "https://www.digisound.ee"
ROOT = f"{BASE}/products/car-stereo"


def build_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "DATABASE_URL": "postgresql+asyncpg://postgres:postgres@localhost:5432/digisound_test",
        "REQUEST_DELAY_MS": 0,
        "HTTP_CONCURRENCY": 2,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


@dataclass(frozen=True)
class Item:
    slug: str
    sku: str
    price: str = "100,00 €"
    stock: str = "In stock"
    category: str = "car-stereo"  # path segment(s) below /products/

    @property
    def url_path(self) -> str:
        return f"/products/{self.category}/{self.slug}"


def listing_html(
    items: Sequence[Item],
    total: int,
    next_start: int | None = None,
    title: str = "CAR STEREO",
    subcategories: Sequence[tuple[str, str]] = (),
    base_path: str = "/products/car-stereo",
) -> str:
    cards = "\n".join(
        f"""<div class="product-card" data-product-id="{i.slug}-id" data-slug="{i.slug}">
  <div class="product-info">
    <h2 class="product-title"><a href="{i.url_path}">{i.slug.upper()}</a></h2>
    <div class="product-sku">{i.sku}</div>
    <div class="product-stock">{i.stock}</div>
    <div class="product-price"><span class="price-current">{i.price}</span></div>
  </div>
</div>"""
        for i in items
    )
    nav = ""
    if subcategories:
        links = "".join(f'<a href="{href}">{name}</a>' for name, href in subcategories)
        nav = f'<div class="category-tree">{links}</div>'
    more = (
        f'<a class="load-more-btn" href="{base_path}?start={next_start}">Load more</a>'
        if next_start is not None
        else ""
    )
    return f"""<!DOCTYPE html><html><head><title>{title}</title></head><body>
{nav}
<main><div class="category-header"><h1>{title}</h1>
<div class="total-products-count">{total} Products</div></div>
<div class="products-grid">{cards}</div>
<div class="pagination-container">{more}</div></main></body></html>"""


def detail_html(
    item: Item,
    title: str | None = None,
    price: str | None = None,
    stock: str | None = None,
    vat: str | None = None,
    ean: str | None = "4006381333931",
    crumbs: Sequence[tuple[str, str]] = (),
) -> str:
    crumb_html = (
        '<nav class="breadcrumb">'
        + " ".join(f'<a href="{href}">{name}</a>' for name, href in crumbs)
        + "</nav>"
        if crumbs
        else ""
    )
    vat_html = f'<span class="vat-basis">{vat}</span>' if vat else ""
    ean_html = f'<div class="ean-row">EAN: <span class="ean">{ean}</span></div>' if ean else ""
    return f"""<!DOCTYPE html><html><head><title>{title or item.slug}</title>
<link rel="canonical" href="{BASE}{item.url_path}"></head><body>
<main>{crumb_html}<div class="product-details"><div class="brand">ACME</div>
<h1 class="title">{title or item.slug.upper()}</h1>
<div class="sku-row">Tootekood: <span class="sku">{item.sku}</span></div>{ean_html}
<div class="price-box"><span class="price-current">{price or item.price}</span>{vat_html}</div>
<div class="stock-info"><span class="stock-status">{stock or item.stock}</span></div>
<div class="short-description">Short text for {item.slug}</div></div>
<table class="specs-table"><tbody><tr><td class="name">Power</td><td class="value">100 W</td></tr></tbody></table>
</main></body></html>"""
