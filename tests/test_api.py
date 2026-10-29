"""Tests for FastAPI endpoints in app/api/."""

from collections.abc import AsyncGenerator
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.db.base import utc_now
from app.db.models import Category, Job, Product
from app.main import app


@pytest.fixture
async def api_client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """Async test client bound to FastAPI application with test database session override."""

    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()
    app.dependency_overrides.clear()


class TestApiReadEndpoints:
    """Tests for system and catalog read endpoints."""

    @pytest.mark.asyncio
    async def test_health_endpoint(self, api_client: AsyncClient, db_session: AsyncSession) -> None:
        response = await api_client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["database"] == "ok"
        assert "git_commit" in data
        assert "alembic_revision" in data

    @pytest.mark.asyncio
    async def test_stats_endpoint(self, api_client: AsyncClient, db_session: AsyncSession) -> None:
        response = await api_client.get("/api/stats")
        assert response.status_code == 200
        data = response.json()
        assert "total_categories" in data
        assert "total_products" in data
        assert "in_stock_products" in data

    @pytest.mark.asyncio
    async def test_categories_endpoint(
        self, api_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        # Create test category
        cat = Category(
            source_id="car-stereo",
            name="CAR STEREO",
            slug="car-stereo",
            canonical_url="https://www.digisound.ee/products/car-stereo",
            ordered_path=["car-stereo"],
            source_reported_count=2229,
        )
        db_session.add(cat)
        await db_session.commit()

        response = await api_client.get("/api/categories")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["source_id"] == "car-stereo"
        assert data[0]["name"] == "CAR STEREO"

    @pytest.mark.asyncio
    async def test_products_pagination(
        self, api_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        now = utc_now()
        # Create 3 products
        for i in range(1, 4):
            prod = Product(
                source="digisound",
                source_product_id=f"prod-{i}",
                sku=f"SKU-{i}",
                title=f"Product {i}",
                canonical_url=f"https://www.digisound.ee/products/car-stereo/prod-{i}",
                content_hash=f"hash{i}",
                commerce_hash=f"comm{i}",
                price_tax_basis="unknown",
                stock_status="in_stock",
                specifications=[],
                images=[],
                category_paths=[["car-stereo"]],
                source_category_ids=["car-stereo"],
                content_updated_at=now,
                stock_updated_at=now,
                first_seen_at=now,
                last_seen_at=now,
                last_checked_at=now,
            )
            db_session.add(prod)
        await db_session.commit()

        # Page 1: limit 2
        resp1 = await api_client.get("/api/products?limit=2")
        assert resp1.status_code == 200
        d1 = resp1.json()
        assert len(d1["items"]) == 2
        assert d1["has_more"] is True
        assert d1["next_cursor"] is not None

        # Page 2: with cursor
        resp2 = await api_client.get(f"/api/products?limit=2&cursor={d1['next_cursor']}")
        assert resp2.status_code == 200
        d2 = resp2.json()
        assert len(d2["items"]) == 1
        assert d2["has_more"] is False

    @pytest.mark.asyncio
    async def test_product_by_id(self, api_client: AsyncClient, db_session: AsyncSession) -> None:
        now = utc_now()
        prod = Product(
            source="digisound",
            source_product_id="sph-evo64dab",
            sku="SPH-EVO64DAB",
            title="Pioneer SPH-EVO64DAB",
            brand="PIONEER",
            canonical_url="https://www.digisound.ee/products/car-stereo/sph-evo64dab",
            content_hash="h1",
            commerce_hash="c1",
            regular_price=Decimal("499.00"),
            price_tax_basis="unknown",
            stock_status="in_stock",
            specifications=[],
            images=[],
            category_paths=[["car-stereo"]],
            source_category_ids=["car-stereo"],
            content_updated_at=now,
            stock_updated_at=now,
            first_seen_at=now,
            last_seen_at=now,
            last_checked_at=now,
        )
        db_session.add(prod)
        await db_session.commit()

        # Found
        resp = await api_client.get("/api/product/id/sph-evo64dab")
        assert resp.status_code == 200
        assert resp.json()["sku"] == "SPH-EVO64DAB"

        # Not found
        resp404 = await api_client.get("/api/product/id/non-existent")
        assert resp404.status_code == 404
        assert resp404.json()["error"]["code"] == "not_found"

    @pytest.mark.asyncio
    async def test_product_exact_sku_and_url_encoding(
        self, api_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        now = utc_now()
        # Product with spaces in SKU: 'HEC HD-AUDIO USB - DSP PRO MK3'
        helix = Product(
            source="digisound",
            source_product_id="helix-hec",
            sku="HEC HD-AUDIO USB - DSP PRO MK3",
            title="HELIX HEC",
            brand="HELIX",
            canonical_url="https://www.digisound.ee/products/car-stereo/helix-hec",
            content_hash="h2",
            commerce_hash="c2",
            price_tax_basis="unknown",
            stock_status="in_stock",
            specifications=[],
            images=[],
            category_paths=[["car-stereo"]],
            source_category_ids=["car-stereo"],
            content_updated_at=now,
            stock_updated_at=now,
            first_seen_at=now,
            last_seen_at=now,
            last_checked_at=now,
        )
        db_session.add(helix)
        await db_session.commit()

        # Test URL-encoded space query: HEC%20HD-AUDIO%20USB%20-%20DSP%20PRO%20MK3
        resp = await api_client.get("/api/product/sku/HEC%20HD-AUDIO%20USB%20-%20DSP%20PRO%20MK3")
        assert resp.status_code == 200
        assert resp.json()["sku"] == "HEC HD-AUDIO USB - DSP PRO MK3"

    @pytest.mark.asyncio
    async def test_ambiguous_sku_returns_409(
        self, api_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        # Invariant 10: If multiple products share exact SKU, return 409 ambiguous_sku
        now = utc_now()
        p1 = Product(
            source="digisound",
            source_product_id="item-a",
            sku="SHARED-SKU",
            title="Item A",
            canonical_url="http://example.com/a",
            content_hash="ha",
            commerce_hash="ca",
            price_tax_basis="unknown",
            stock_status="in_stock",
            specifications=[],
            images=[],
            category_paths=[["car-stereo"]],
            source_category_ids=["car-stereo"],
            content_updated_at=now,
            stock_updated_at=now,
            first_seen_at=now,
            last_seen_at=now,
            last_checked_at=now,
        )
        p2 = Product(
            source="digisound",
            source_product_id="item-b",
            sku="SHARED-SKU",
            title="Item B",
            canonical_url="http://example.com/b",
            content_hash="hb",
            commerce_hash="cb",
            price_tax_basis="unknown",
            stock_status="in_stock",
            specifications=[],
            images=[],
            category_paths=[["car-stereo"]],
            source_category_ids=["car-stereo"],
            content_updated_at=now,
            stock_updated_at=now,
            first_seen_at=now,
            last_seen_at=now,
            last_checked_at=now,
        )
        db_session.add_all([p1, p2])
        await db_session.commit()

        resp = await api_client.get("/api/product/sku/SHARED-SKU")
        assert resp.status_code == 409
        body = resp.json()
        assert body["error"]["code"] == "ambiguous_sku"
        assert body["error"]["details"]["candidate_count"] == 2
        candidates = body["error"]["details"]["candidates"]
        assert len(candidates) == 2
        assert {c["source_product_id"] for c in candidates} == {"item-a", "item-b"}

    @pytest.mark.asyncio
    async def test_job_status_endpoint(
        self, api_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        job = Job(
            type="discovery",
            status="completed",
            params={"test": True},
            result_summary={"total": 10},
        )
        db_session.add(job)
        await db_session.commit()

        resp = await api_client.get(f"/api/jobs/{job.id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"
        assert resp.json()["type"] == "discovery"
