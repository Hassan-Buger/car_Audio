"""Integration tests for synchronization engine, resumability, and reconciliation."""

from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Category, Product, ProductCategory, ProductChange
from app.sync.engine import SyncEngine
from app.sync.reconciliation import (
    CategoryReconciliationRecord,
    evaluate_category_completeness,
    generate_reconciliation_csv,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def listing_page_0() -> str:
    return (FIXTURES_DIR / "listing_page_0.html").read_text(encoding="utf-8")


@pytest.fixture
def listing_page_185() -> str:
    return (FIXTURES_DIR / "listing_page_185.html").read_text(encoding="utf-8")


@pytest.fixture
def pioneer_detail() -> str:
    return (FIXTURES_DIR / "product_detail_pioneer.html").read_text(encoding="utf-8")


@pytest.fixture
def helix_detail() -> str:
    return (FIXTURES_DIR / "product_detail_helix.html").read_text(encoding="utf-8")


class TestSyncEngineIntegration:
    """End-to-end integration tests using real PostgreSQL test database."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_discovery_run_and_upsert(
        self,
        db_session: AsyncSession,
        listing_page_0: str,
        listing_page_185: str,
        pioneer_detail: str,
        helix_detail: str,
    ) -> None:
        # Mock source listing pages (specific param route first)
        respx.get("https://www.digisound.ee/products/car-stereo", params={"start": 12}).mock(
            return_value=httpx.Response(200, text=listing_page_185)
        )
        respx.get("https://www.digisound.ee/products/car-stereo").mock(
            return_value=httpx.Response(200, text=listing_page_0)
        )

        # Mock product detail pages
        respx.get("https://www.digisound.ee/products/car-stereo/pioneer-sph-evo64dab").mock(
            return_value=httpx.Response(200, text=pioneer_detail)
        )
        respx.get(
            "https://www.digisound.ee/products/car-stereo/helix-hec-hd-audio-usb-dsp-pro-mk3"
        ).mock(return_value=httpx.Response(200, text=helix_detail))
        # Default mock for other product detail calls
        respx.get(url__regex=r"https://www.digisound.ee/products/car-stereo/.*").mock(
            return_value=httpx.Response(200, text="<html><title>Detail</title></html>")
        )

        engine = SyncEngine(db_session)
        run = await engine.execute_discovery_run()

        assert run is not None
        assert run.total_products_discovered > 0
        assert run.new_products > 0

        # Verify Category
        cat_query = select(Category).where(Category.source_id == "car-stereo")
        cat = (await db_session.execute(cat_query)).scalar_one_or_none()
        assert cat is not None
        assert cat.name == "CAR STEREO"

        # Verify Pioneer product in DB
        prod_query = select(Product).where(
            Product.source == "digisound", Product.sku == "SPH-EVO64DAB"
        )
        pioneer_prod = (await db_session.execute(prod_query)).scalar_one_or_none()
        assert pioneer_prod is not None
        assert pioneer_prod.source == "digisound"
        assert pioneer_prod.brand == "PIONEER"
        assert pioneer_prod.regular_price == 499.00
        assert pioneer_prod.ean_raw == "4988028476211"
        assert pioneer_prod.ean_valid is True

        # Verify HELIX product exact SKU with spaces
        helix_query = select(Product).where(
            Product.source == "digisound", Product.sku == "HEC HD-AUDIO USB - DSP PRO MK3"
        )
        helix_prod = (await db_session.execute(helix_query)).scalar_one_or_none()
        assert helix_prod is not None
        assert helix_prod.sku == "HEC HD-AUDIO USB - DSP PRO MK3"

        # Verify Product Category association
        assoc_query = select(ProductCategory).where(ProductCategory.product_id == pioneer_prod.id)
        assoc = (await db_session.execute(assoc_query)).scalar_one_or_none()
        assert assoc is not None
        assert assoc.ordered_path == ["car-stereo"]

        # Verify Product Changes log
        change_query = select(ProductChange).where(ProductChange.product_id == pioneer_prod.id)
        changes = list((await db_session.execute(change_query)).scalars().all())
        assert len(changes) == 1
        assert changes[0].change_type == "created"

    @pytest.mark.asyncio
    @respx.mock
    async def test_unchanged_hashes_preserve_timestamps(
        self,
        db_session: AsyncSession,
        listing_page_185: str,
        pioneer_detail: str,
    ) -> None:
        # Mock single-page crawl
        respx.get("https://www.digisound.ee/products/car-stereo").mock(
            return_value=httpx.Response(200, text=listing_page_185)
        )
        respx.get(url__regex=r"https://www.digisound.ee/products/car-stereo/.*").mock(
            return_value=httpx.Response(200, text="<html><title>Detail</title></html>")
        )

        engine = SyncEngine(db_session)
        # First run
        run1 = await engine.execute_discovery_run()
        assert run1.new_products > 0

        # Get initial timestamps
        prod_query = (
            select(Product).where(Product.source == "digisound").order_by(Product.id).limit(1)
        )
        prod_before = (await db_session.execute(prod_query)).scalar_one()
        content_ts_before = prod_before.content_updated_at
        stock_ts_before = prod_before.stock_updated_at

        # Second run with identical data
        run2 = await engine.execute_discovery_run()
        assert run2.unchanged_products > 0

        # Timestamps must remain identical (Invariant 17)
        prod_after = (await db_session.execute(prod_query)).scalar_one()
        assert prod_after.content_updated_at == content_ts_before
        assert prod_after.stock_updated_at == stock_ts_before


class TestReconciliationLogic:
    """Tests for completeness evaluation and reconciliation CSV export."""

    def test_evaluate_completeness(self) -> None:
        # Exact match
        is_comp, err = evaluate_category_completeness(
            distinct_collected=2229,
            source_reported=2229,
            failed_requests=0,
            pagination_ended=True,
        )
        assert is_comp is True
        assert err is None

        # Discrepancy
        is_comp, err = evaluate_category_completeness(
            distinct_collected=2200,
            source_reported=2229,
            failed_requests=0,
            pagination_ended=True,
        )
        assert is_comp is False
        assert "Count discrepancy" in str(err)

        # Failed requests
        is_comp, err = evaluate_category_completeness(
            distinct_collected=2229,
            source_reported=2229,
            failed_requests=2,
            pagination_ended=True,
        )
        assert is_comp is False
        assert "failed requests" in str(err)

    def test_generate_reconciliation_csv(self) -> None:
        records = [
            CategoryReconciliationRecord(
                category_id=1,
                source_id="car-stereo",
                category_path="car-stereo",
                source_reported_count=2229,
                distinct_collected_count=2229,
                pagination_complete=True,
                failed_requests=0,
                discrepancy_reason=None,
            )
        ]
        csv_text = generate_reconciliation_csv(records)
        assert "category_id,source_id,category_path" in csv_text
        assert "1,car-stereo,car-stereo,2229,2229,true,0," in csv_text
