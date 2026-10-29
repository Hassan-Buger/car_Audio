"""Tests for explicit category-navigation parsing and tax-basis text recognition."""

from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from app.normalize.hashing import compute_content_hash
from app.normalize.price import normalize_tax_basis
from app.scraper.listing_parser import parse_category_links, parse_listing_page

ROOT = "https://www.digisound.ee/products/car-stereo"
FIXTURES = Path(__file__).parent / "fixtures"


def soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


class TestCategoryLinks:
    def test_finds_children_in_explicit_navigation(self) -> None:
        html = """<div class="category-tree">
          <a href="/products/car-stereo/amplifiers">Amplifiers (85)</a>
          <a href="/products/car-stereo/speakers">Speakers</a></div>"""
        links = parse_category_links(soup(html), ROOT)
        assert [(x.slug, x.name, x.count_hint) for x in links] == [
            ("amplifiers", "Amplifiers", 85),
            ("speakers", "Speakers", None),
        ]

    def test_ignores_facets_filters_pagination_and_foreign_paths(self) -> None:
        html = """<div class="category-tree">
          <a href="/products/car-stereo?manufacturers[1]=1">HELIX (217)</a>
          <a href="/products/car-stereo/amps?attributes[1][10]=1_10">Model</a>
          <a href="/products/car-stereo/amps?start=12">next</a>
          <a href="/products/home-audio/receivers">Home audio</a>
          <a href="/products/car-stereo">self</a></div>"""
        assert parse_category_links(soup(html), ROOT) == []

    def test_no_navigation_block_means_no_guessing(self) -> None:
        html = (FIXTURES / "listing_page_0.html").read_text(encoding="utf-8")
        page = parse_listing_page(html, category_url=ROOT)
        assert page.subcategories == []  # sidebar manufacturer/model facets are not categories
        assert page.title == "CAR STEREO" and page.reported_total == 2229

    def test_deduplicates_links(self) -> None:
        html = """<ul class="subcategories"><li><a href="/products/car-stereo/a">A</a></li>
        <li><a href="/products/car-stereo/a/">A again</a></li></ul>"""
        assert len(parse_category_links(soup(html), ROOT)) == 1


class TestTaxBasisText:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Sisaldab käibemaksu", "incl_vat"),
            ("Hind koos käibemaksuga", "incl_vat"),
            ("Hind sisaldab km", "incl_vat"),
            ("Price incl. VAT", "incl_vat"),
            ("Hind ilma käibemaksuta", "excl_vat"),
            ("Ei sisalda käibemaksu", "excl_vat"),
            ("Ilma KM-ta", "excl_vat"),
            ("Excl. VAT", "excl_vat"),
            ("249,00 €", "unknown"),
            ("", "unknown"),
            (None, "unknown"),
        ],
    )
    def test_texts(self, text: str | None, expected: str) -> None:
        assert normalize_tax_basis(text) == expected

    def test_never_defaults_to_a_rate_or_basis(self) -> None:
        assert normalize_tax_basis("Soodushind") == "unknown"


class TestContentHashIndependentOfCategory:
    def test_hash_without_category_paths_is_stable(self) -> None:
        kw: dict[str, object] = {
            "source_product_id": "p",
            "sku": "S",
            "title": "T",
            "brand": "B",
            "ean_raw": None,
            "canonical_url": "u",
            "short_description": None,
            "long_description": None,
            "specifications": [],
            "images": [],
        }
        assert compute_content_hash(**kw) == compute_content_hash(**kw)  # type: ignore[arg-type]
        with_a = compute_content_hash(**kw, category_paths=[["a"]])  # type: ignore[arg-type]
        with_b = compute_content_hash(**kw, category_paths=[["b"]])  # type: ignore[arg-type]
        assert with_a != with_b != compute_content_hash(**kw)  # type: ignore[arg-type]


class TestLiveMarkupListing:
    """Listing markup modelled on the structure observed on the live site (invented values)."""

    def _html(self) -> str:
        from pathlib import Path

        return (Path(__file__).parent / "fixtures" / "listing_live_markup.html").read_text(
            encoding="utf-8"
        )

    def test_parses_live_style_cards(self) -> None:
        from app.scraper.listing_parser import parse_listing_page

        page = parse_listing_page(
            self._html(), category_url="https://www.digisound.ee/products/car-stereo"
        )
        assert page.reported_total == 2229
        assert page.next_start == 12 and page.has_next_page is True
        assert page.subcategories == []  # filters are never treated as categories
        assert [p.title for p in page.products] == ["EXAMPLE SUB 10A", "EXAMPLE AMP 4CH"]
        first, second = page.products
        assert first.sku == "SUB 10A" and second.sku == "AMP / 4CH"
        assert first.slug == "example-sub-10a" and first.source_product_id == "example-sub-10a"
        assert first.url == "https://www.digisound.ee/products/car-stereo/example-sub-10a"
        assert first.price_raw is not None and first.price_raw.startswith("249,00")
        assert first.stock_text == "In stock"
        assert second.stock_text == "Temporarily out of stock"
        assert first.spec_summary is not None and "subwoofer" in first.spec_summary


class TestLiveMarkupDetail:
    """Detail page markup modelled on the structure observed on the live site (invented values)."""

    def test_parses_live_style_detail(self) -> None:
        from pathlib import Path

        from app.scraper.detail_parser import parse_detail_page

        html = (Path(__file__).parent / "fixtures" / "detail_live_markup.html").read_text(
            encoding="utf-8"
        )
        d = parse_detail_page(html)
        assert d.title == "EXAMPLE SUB 10A"
        assert d.sku == "SUB 10A"  # exact page SKU, not the URL slug
        assert d.brand == "Example brand"  # not the site-logo link
        assert d.ean_raw == "4037072027303"
        assert d.regular_price_raw == "249,00 €"
        assert d.stock_text == "In stock"
        assert d.price_tax_text is None  # no VAT wording on the page: never assumed
        assert d.specifications == [
            {"name": "Voice coil", "value": '50 mm / 2"'},
            {"name": "Impedance", "value": "2 x 2 Ohm"},
        ]
        assert len(d.images) == 2
        assert d.category_breadcrumbs == ["CAR STEREO", "SUBWOOFERS AND ACCESSORIES", "Subwoofers"]
        assert d.category_crumbs[-1] == (
            "Subwoofers",
            "https://www.digisound.ee/products/car-stereo/subwoofers-and-accessories/subwoofers",
        )
        assert d.source_product_id == "example-sub-10a"
