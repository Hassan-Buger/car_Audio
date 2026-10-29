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
