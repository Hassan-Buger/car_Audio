"""Unit tests for HTML listing & detail parsers, validators, and scraper client."""

from pathlib import Path

import httpx
import pytest
import respx

from app.scraper.client import ScraperClient
from app.scraper.detail_parser import parse_detail_page
from app.scraper.listing_parser import parse_listing_page
from app.scraper.validators import (
    ChallengeDetectedError,
    InvalidStatusCodeError,
    RepeatedPageError,
    UnexpectedEmptyPageError,
    is_challenge_page,
    validate_listing_response,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def listing_page_0_html() -> str:
    return (FIXTURES_DIR / "listing_page_0.html").read_text(encoding="utf-8")


@pytest.fixture
def listing_page_185_html() -> str:
    return (FIXTURES_DIR / "listing_page_185.html").read_text(encoding="utf-8")


@pytest.fixture
def listing_page_empty_html() -> str:
    return (FIXTURES_DIR / "listing_page_empty.html").read_text(encoding="utf-8")


@pytest.fixture
def listing_page_challenge_html() -> str:
    return (FIXTURES_DIR / "listing_page_challenge.html").read_text(encoding="utf-8")


@pytest.fixture
def product_detail_pioneer_html() -> str:
    return (FIXTURES_DIR / "product_detail_pioneer.html").read_text(encoding="utf-8")


@pytest.fixture
def product_detail_helix_html() -> str:
    return (FIXTURES_DIR / "product_detail_helix.html").read_text(encoding="utf-8")


class TestListingParser:
    """Tests for category listing page parsing and pagination detection."""

    def test_parse_listing_page_0(self, listing_page_0_html: str) -> None:
        result = parse_listing_page(listing_page_0_html)
        assert result.reported_total == 2229
        assert len(result.products) == 12
        assert result.has_next_page is True
        assert result.next_start == 12

        # Verify Pioneer reference product
        pioneer = result.products[0]
        assert pioneer.title == "Pioneer SPH-EVO64DAB"
        assert pioneer.sku == "SPH-EVO64DAB"
        assert pioneer.brand == "PIONEER"
        assert pioneer.price_raw == "499,00 €"
        assert pioneer.stock_text == "In stock"
        assert "pioneer-sph-evo64dab" in pioneer.url

        # Verify DD Audio reference products
        dd1 = result.products[1]
        assert dd1.sku == "VO-MN104-S4"
        assert dd1.brand == "DD Audio"

        dd2 = result.products[2]
        assert dd2.sku == "VO-XN306-S4"
        assert dd2.stock_text == "Temporarily out of stock"

        # Verify HELIX reference product with spaced SKU
        helix = result.products[3]
        assert helix.sku == "HEC HD-AUDIO USB - DSP PRO MK3"
        assert helix.brand == "HELIX"

    def test_parse_listing_page_185_end_of_pagination(self, listing_page_185_html: str) -> None:
        result = parse_listing_page(listing_page_185_html)
        assert result.reported_total == 2229
        assert len(result.products) == 2
        assert result.has_next_page is False
        assert result.next_start is None


class TestDetailParser:
    """Tests for product detail page parsing."""

    def test_parse_pioneer_detail(self, product_detail_pioneer_html: str) -> None:
        detail = parse_detail_page(product_detail_pioneer_html)
        assert detail.title == "Pioneer SPH-EVO64DAB"
        assert detail.brand == "PIONEER"
        assert detail.sku == "SPH-EVO64DAB"
        assert detail.ean_raw == "4988028476211"
        assert detail.regular_price_raw == "499,00 €"
        assert (
            detail.canonical_url
            == "https://www.digisound.ee/products/car-stereo/pioneer-sph-evo64dab"
        )
        assert detail.price_tax_text == "Sisaldab käibemaksu"
        assert detail.stock_text == "Laos"
        assert "Apple CarPlay" in str(detail.short_description)
        assert len(detail.specifications) == 6
        assert detail.specifications[0] == {"name": "Ekraani suurus", "value": "6.8 tolli"}
        assert len(detail.images) > 0

    def test_parse_helix_exact_sku(self, product_detail_helix_html: str) -> None:
        detail = parse_detail_page(product_detail_helix_html)
        # Invariant 10: SKU identity is exact, including spaces
        assert detail.sku == "HEC HD-AUDIO USB - DSP PRO MK3"
        assert detail.brand == "HELIX"
        assert detail.ean_raw == "4025901004123"
        assert detail.regular_price_raw == "249,00 €"


class TestPageValidators:
    """Tests for challenge detection and listing page validation rules."""

    def test_detect_apache_403_challenge(self, listing_page_challenge_html: str) -> None:
        assert is_challenge_page(403, listing_page_challenge_html) is True
        assert is_challenge_page(200, "<html>403 Forbidden - Access Denied</html>") is True
        assert is_challenge_page(200, "<html>Attention Required! | Cloudflare</html>") is True
        assert is_challenge_page(200, "<html>Normal Page Content</html>") is False

    def test_validate_listing_response_raises_on_challenge(
        self, listing_page_challenge_html: str
    ) -> None:
        with pytest.raises(ChallengeDetectedError):
            validate_listing_response(403, listing_page_challenge_html)

    def test_validate_listing_response_raises_on_invalid_status(self) -> None:
        with pytest.raises(InvalidStatusCodeError):
            validate_listing_response(500, "<html>Internal Server Error</html>")

    def test_validate_listing_response_unexpected_empty(self, listing_page_empty_html: str) -> None:
        with pytest.raises(UnexpectedEmptyPageError):
            validate_listing_response(
                status_code=200,
                html_text=listing_page_empty_html,
                current_offset=12,
                reported_total=2229,
                current_product_ids=[],
            )

    def test_validate_listing_response_repeated_page(self, listing_page_0_html: str) -> None:
        with pytest.raises(RepeatedPageError):
            validate_listing_response(
                status_code=200,
                html_text=listing_page_0_html,
                current_offset=12,
                reported_total=2229,
                current_product_ids=["prod1", "prod2"],
                previous_product_ids=["prod1", "prod2"],
            )

    def test_validate_listing_response_valid(self, listing_page_0_html: str) -> None:
        # Should not raise
        validate_listing_response(
            status_code=200,
            html_text=listing_page_0_html,
            current_offset=0,
            reported_total=2229,
            current_product_ids=["prod1", "prod2"],
            previous_product_ids=None,
        )


class TestScraperClient:
    """Tests for HTTP client retry and error behavior."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_client_does_not_retry_403(self) -> None:
        respx.get("https://www.digisound.ee/test").mock(
            return_value=httpx.Response(403, text="Forbidden")
        )

        async with ScraperClient() as client:
            with pytest.raises(ChallengeDetectedError):
                await client.get("https://www.digisound.ee/test")

    @pytest.mark.asyncio
    @respx.mock
    async def test_client_retries_on_500_then_succeeds(self) -> None:
        endpoint = respx.get("https://www.digisound.ee/retry-test")
        endpoint.side_effect = [
            httpx.Response(500, text="Error"),
            httpx.Response(200, text="<html>Success</html>"),
        ]

        async with ScraperClient() as client:
            response = await client.get("https://www.digisound.ee/retry-test")
            assert response.status_code == 200
            assert "Success" in response.text
