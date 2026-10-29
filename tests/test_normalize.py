"""Unit tests for pure normalization, validation, and hashing functions."""

from decimal import Decimal

import pytest

from app.normalize.ean import validate_ean
from app.normalize.hashing import compute_commerce_hash, compute_content_hash
from app.normalize.images import is_placeholder_image, normalize_image_urls, resolve_image_url
from app.normalize.price import extract_currency, normalize_tax_basis, parse_price
from app.normalize.sku import normalize_sku
from app.normalize.specs import normalize_specification, normalize_specifications_list
from app.normalize.stock import (
    STOCK_STATUS_BACKORDER,
    STOCK_STATUS_IN_STOCK,
    STOCK_STATUS_OUT_OF_STOCK,
    STOCK_STATUS_UNKNOWN,
    normalize_stock_status,
    parse_stock_quantity,
)


class TestPriceNormalization:
    """Tests for price parsing and tax basis."""

    def test_parse_european_decimal_comma(self) -> None:
        assert parse_price("249,00 €") == Decimal("249.00")
        assert parse_price("249,50 EUR") == Decimal("249.50")
        assert parse_price("0,99 €") == Decimal("0.99")

    def test_parse_thousands_separator_space(self) -> None:
        assert parse_price("1 249,00 €") == Decimal("1249.00")
        assert parse_price("12 500,50") == Decimal("12500.50")

    def test_parse_thousands_separator_dot(self) -> None:
        assert parse_price("1.249,00 €") == Decimal("1249.00")
        assert parse_price("10.000,00 EUR") == Decimal("10000.00")

    def test_parse_standard_decimal_dot(self) -> None:
        assert parse_price("99.99") == Decimal("99.99")
        assert parse_price("149.00") == Decimal("149.00")

    def test_parse_empty_or_none(self) -> None:
        assert parse_price(None) is None
        assert parse_price("") is None
        assert parse_price("   ") is None
        assert parse_price("€") is None

    def test_parse_invalid_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            parse_price("abc,def")

    def test_extract_currency(self) -> None:
        assert extract_currency("249,00 €") == "EUR"
        assert extract_currency("100 EUR") == "EUR"
        assert extract_currency("$99.99") == "USD"
        assert extract_currency("£50") == "GBP"
        assert extract_currency(None) == "EUR"
        assert extract_currency("100.00") == "EUR"

    def test_tax_basis_normalization(self) -> None:
        assert normalize_tax_basis("Hind sisaldab km") == "incl_vat"
        assert normalize_tax_basis("Koos KM-ga") == "incl_vat"
        assert normalize_tax_basis("Price incl. VAT") == "incl_vat"
        assert normalize_tax_basis("Ilma KM-ta") == "excl_vat"
        assert normalize_tax_basis("Excl. VAT") == "excl_vat"
        assert normalize_tax_basis(None) == "unknown"
        assert normalize_tax_basis("249,00 €") == "unknown"


class TestStockNormalization:
    """Tests for stock status and numeric quantity parsing."""

    def test_in_stock_variations(self) -> None:
        assert normalize_stock_status("In stock") == STOCK_STATUS_IN_STOCK
        assert normalize_stock_status("Laos") == STOCK_STATUS_IN_STOCK
        assert normalize_stock_status("Kohe olemas") == STOCK_STATUS_IN_STOCK
        assert normalize_stock_status("Saadaval") == STOCK_STATUS_IN_STOCK

    def test_out_of_stock_variations(self) -> None:
        assert normalize_stock_status("Out of stock") == STOCK_STATUS_OUT_OF_STOCK
        assert normalize_stock_status("Temporarily out of stock") == STOCK_STATUS_OUT_OF_STOCK
        assert normalize_stock_status("Ajutiselt otsas") == STOCK_STATUS_OUT_OF_STOCK
        assert normalize_stock_status("Läbimüüdud") == STOCK_STATUS_OUT_OF_STOCK

    def test_backorder_distinct_from_out_of_stock(self) -> None:
        assert normalize_stock_status("Backorder") == STOCK_STATUS_BACKORDER
        assert normalize_stock_status("Tellimisel") == STOCK_STATUS_BACKORDER
        assert normalize_stock_status("Järeltellimus") == STOCK_STATUS_BACKORDER
        assert normalize_stock_status("Pre-order") == STOCK_STATUS_BACKORDER

    def test_unknown_stock_status(self) -> None:
        assert normalize_stock_status(None) == STOCK_STATUS_UNKNOWN
        assert normalize_stock_status("") == STOCK_STATUS_UNKNOWN
        assert normalize_stock_status("Call for price") == STOCK_STATUS_UNKNOWN

    def test_parse_stock_quantity_numbered(self) -> None:
        assert parse_stock_quantity("5 tk") == 5
        assert parse_stock_quantity("Laos 3 tk") == 3
        assert parse_stock_quantity("12 pcs") == 12
        assert parse_stock_quantity("25") == 25

    def test_parse_stock_quantity_unknown_returns_none(self) -> None:
        # Invariant 6: Unknown stock quantity is NULL, not 0
        assert parse_stock_quantity(None) is None
        assert parse_stock_quantity("") is None
        assert parse_stock_quantity("In stock") is None
        assert parse_stock_quantity("Temporarily out of stock") is None


class TestEanValidation:
    """Tests for GS1 EAN/GTIN validation."""

    def test_valid_ean13(self) -> None:
        # Standard valid EAN-13 barcodes
        # 4006381333931: 4*1 + 0*3 + 0*1 + 6*3 + 3*1 + 8*3 + 1*1 + 3*3 + 3*1 + 3*3 + 9*1 + 3*3 = 4+0+0+18+3+24+1+9+3+9+9+9 = 89 -> 10-(89%10)=1
        is_valid, err = validate_ean("4006381333931")
        assert is_valid is True
        assert err is None

    def test_valid_ean8(self) -> None:
        # 96385074
        is_valid, err = validate_ean("96385074")
        assert is_valid is True
        assert err is None

    def test_valid_upc_a(self) -> None:
        # 036000291452 (12 digits)
        is_valid, err = validate_ean("036000291452")
        assert is_valid is True
        assert err is None

    def test_invalid_checksum(self) -> None:
        is_valid, err = validate_ean("4006381333932")
        assert is_valid is False
        assert "Checksum mismatch" in str(err)

    def test_invalid_length(self) -> None:
        is_valid, err = validate_ean("12345")
        assert is_valid is False
        assert "Invalid EAN length" in str(err)

    def test_non_digit_ean(self) -> None:
        is_valid, err = validate_ean("400638133393A")
        assert is_valid is False
        assert "non-digit" in str(err)

    def test_missing_or_empty_ean(self) -> None:
        is_valid, err = validate_ean(None)
        assert is_valid is False
        assert err == "Missing EAN"

        is_valid, err = validate_ean("")
        assert is_valid is False
        assert err == "Empty EAN"


class TestSkuNormalization:
    """Tests for exact SKU preservation."""

    def test_preserve_spaces_and_slashes(self) -> None:
        # Invariant 10: SKU identity is exact, including spaces, case, slashes, punctuation
        assert normalize_sku("GZW 10-30A") == "GZW 10-30A"
        assert normalize_sku("HEC HD-AUDIO USB - DSP PRO MK3") == "HEC HD-AUDIO USB - DSP PRO MK3"
        assert normalize_sku("VO-MN104-S4/A") == "VO-MN104-S4/A"

    def test_trim_surrounding_whitespace_only(self) -> None:
        assert normalize_sku("  SPH-EVO64DAB \n") == "SPH-EVO64DAB"
        assert normalize_sku(None) == ""


class TestImageNormalization:
    """Tests for image URL resolution, placeholder detection, and deduplication."""

    def test_resolve_relative_urls(self) -> None:
        assert (
            resolve_image_url("/cache/images/pioneer.jpg", base_url="https://www.digisound.ee")
            == "https://www.digisound.ee/cache/images/pioneer.jpg"
        )
        assert resolve_image_url("//cdn.digisound.ee/img.png") == "https://cdn.digisound.ee/img.png"
        assert (
            resolve_image_url("https://www.digisound.ee/img.png")
            == "https://www.digisound.ee/img.png"
        )

    def test_placeholder_filtering(self) -> None:
        assert is_placeholder_image("https://www.digisound.ee/assets/placeholder.png") is True
        assert is_placeholder_image("https://www.digisound.ee/assets/blank.gif") is True
        assert is_placeholder_image("data:image/png;base64,xxxx") is True
        assert is_placeholder_image("https://www.digisound.ee/products/real_speaker.jpg") is False

    def test_normalize_image_urls_deduplication(self) -> None:
        raw = [
            "/img1.jpg",
            "/placeholder.png",
            "/img2.jpg",
            "/img1.jpg",  # duplicate
            "https://www.digisound.ee/img3.jpg",
        ]
        result = normalize_image_urls(raw, base_url="https://www.digisound.ee")
        assert result == [
            "https://www.digisound.ee/img1.jpg",
            "https://www.digisound.ee/img2.jpg",
            "https://www.digisound.ee/img3.jpg",
        ]


class TestSpecificationNormalization:
    """Tests for specification parsing."""

    def test_normalize_specification_with_units(self) -> None:
        spec = normalize_specification("Võimsus RMS", "100 W")
        assert spec["name"] == "Võimsus RMS"
        assert spec["value"] == "100"
        assert spec["unit"] == "W"

        spec2 = normalize_specification("Takistus", "4 Ohm")
        assert spec2["name"] == "Takistus"
        assert spec2["value"] == "4"
        assert spec2["unit"] == "Ohm"

    def test_normalize_specification_without_units(self) -> None:
        spec = normalize_specification("Värv", "Must")
        assert spec["name"] == "Värv"
        assert spec["value"] == "Must"
        assert spec["unit"] is None

    def test_normalize_specifications_list_deduplication(self) -> None:
        raw = [
            {"name": "Power", "value": "100 W"},
            {"name": "Power", "value": "100 W"},  # duplicate
            {"name": "Impedance", "value": "4 Ohm"},
        ]
        specs = normalize_specifications_list(raw)
        assert len(specs) == 2
        assert specs[0]["name"] == "Power"
        assert specs[1]["name"] == "Impedance"


class TestHashingDeterminism:
    """Tests for content and commerce SHA-256 hashing."""

    def test_content_hash_determinism(self) -> None:
        h1 = compute_content_hash(
            source_product_id="sph-evo64dab",
            sku="SPH-EVO64DAB",
            title="Pioneer SPH-EVO64DAB",
            brand="PIONEER",
            ean_raw="4006381333931",
            canonical_url="https://www.digisound.ee/products/car-stereo/sph-evo64dab",
            short_description="Short desc",
            long_description="Long desc",
            specifications=[{"name": "Screen", "value": "6.8", "unit": "inch"}],
            images=["https://www.digisound.ee/img1.jpg"],
            category_paths=[["car-stereo"]],
        )
        h2 = compute_content_hash(
            source_product_id="sph-evo64dab",
            sku="SPH-EVO64DAB",
            title="Pioneer SPH-EVO64DAB",
            brand="PIONEER",
            ean_raw="4006381333931",
            canonical_url="https://www.digisound.ee/products/car-stereo/sph-evo64dab",
            short_description="Short desc",
            long_description="Long desc",
            specifications=[{"name": "Screen", "value": "6.8", "unit": "inch"}],
            images=["https://www.digisound.ee/img1.jpg"],
            category_paths=[["car-stereo"]],
        )
        assert h1 == h2
        assert len(h1) == 64

    def test_content_hash_changes_on_title_change(self) -> None:
        h1 = compute_content_hash(
            source_product_id="123",
            sku="SKU-1",
            title="Original Title",
            brand="Brand",
            ean_raw=None,
            canonical_url="http://example.com/123",
            short_description=None,
            long_description=None,
            specifications=[],
            images=[],
            category_paths=[["cat1"]],
        )
        h2 = compute_content_hash(
            source_product_id="123",
            sku="SKU-1",
            title="Updated Title",
            brand="Brand",
            ean_raw=None,
            canonical_url="http://example.com/123",
            short_description=None,
            long_description=None,
            specifications=[],
            images=[],
            category_paths=[["cat1"]],
        )
        assert h1 != h2

    def test_commerce_hash_determinism_and_changes(self) -> None:
        h1 = compute_commerce_hash(
            currency="EUR",
            regular_price=Decimal("249.00"),
            sale_price=None,
            price_tax_basis="unknown",
            stock_quantity=None,
            stock_status="in_stock",
            availability_text="In stock",
        )
        h2 = compute_commerce_hash(
            currency="EUR",
            regular_price=Decimal("249.00"),
            sale_price=None,
            price_tax_basis="unknown",
            stock_quantity=None,
            stock_status="in_stock",
            availability_text="In stock",
        )
        assert h1 == h2

        # Price update triggers new commerce hash
        h3 = compute_commerce_hash(
            currency="EUR",
            regular_price=Decimal("229.00"),
            sale_price=None,
            price_tax_basis="unknown",
            stock_quantity=None,
            stock_status="in_stock",
            availability_text="In stock",
        )
        assert h1 != h3
