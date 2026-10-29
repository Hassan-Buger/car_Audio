"""Tests for Delta API and protected job trigger endpoints."""

from collections.abc import AsyncGenerator
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.config import Settings, get_settings
from app.db.base import utc_now
from app.db.models import Product, ProductChange
from app.main import app


@pytest.fixture
async def api_client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """Async test client bound to FastAPI application with test database session override."""

    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    def _override_settings() -> Settings:
        return Settings(
            DATABASE_URL="postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/digisound_test",
            API_KEY="test-secret-job-key",
            READ_API_KEY="",
        )

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_settings] = _override_settings
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


class TestDeltaApi:
    """Tests for incremental delta changes synchronization."""

    @pytest.mark.asyncio
    async def test_delta_empty_database(self, api_client: AsyncClient) -> None:
        response = await api_client.get("/api/products/delta")
        assert response.status_code == 200
        data = response.json()
        assert data["items"] == []
        assert data["window_from"] == 0
        assert data["window_until"] == 0
        assert data["has_more"] is False
        assert data["next_cursor"] is None

    @pytest.mark.asyncio
    async def test_delta_pagination_and_window_stability(
        self, api_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        # Create test product
        prod = Product(
            source="digisound",
            source_product_id="test-delta-prod-1",
            sku="SKU-DELTA-1",
            title="Delta Product 1",
            canonical_url="https://www.digisound.ee/products/car-stereo/delta-1",
            currency="EUR",
            regular_price=Decimal("100.00"),
            price_tax_basis="standard_vat",
            stock_status="in_stock",
            specifications=[],
            images=[],
            category_paths=[["car-stereo"]],
            source_category_ids=["car-stereo"],
            content_hash="h1",
            commerce_hash="c1",
        )
        db_session.add(prod)
        await db_session.commit()
        await db_session.refresh(prod)

        # Create two changes
        ch1 = ProductChange(
            product_id=prod.id,
            change_type="created",
            changed_fields=["all"],
            new_data={"title": "Delta Product 1"},
            recorded_at=utc_now(),
        )
        ch2 = ProductChange(
            product_id=prod.id,
            change_type="commerce_updated",
            changed_fields=["regular_price"],
            new_data={"regular_price": "120.00"},
            recorded_at=utc_now(),
        )
        db_session.add_all([ch1, ch2])
        await db_session.commit()
        await db_session.refresh(ch1)
        await db_session.refresh(ch2)

        # 1. Fetch first page with limit=1
        resp1 = await api_client.get("/api/products/delta", params={"limit": 1})
        assert resp1.status_code == 200
        d1 = resp1.json()
        assert len(d1["items"]) == 1
        assert d1["items"][0]["seq"] == ch1.seq
        assert d1["items"][0]["source_product_id"] == "test-delta-prod-1"
        assert d1["items"][0]["sku"] == "SKU-DELTA-1"
        assert d1["has_more"] is True
        assert d1["next_cursor"] is not None
        assert d1["window_until"] == ch2.seq

        # 2. Test idempotency/retry of same cursor
        resp1_retry = await api_client.get(
            "/api/products/delta", params={"limit": 1, "cursor": d1["next_cursor"]}
        )
        assert resp1_retry.status_code == 200
        d1_retry = resp1_retry.json()
        assert len(d1_retry["items"]) == 1
        assert d1_retry["items"][0]["seq"] == ch2.seq
        assert d1_retry["has_more"] is False
        assert d1_retry["next_cursor"] is None

        # 3. Test window stability: insert a 3rd change while consumer was paginating
        ch3 = ProductChange(
            product_id=prod.id,
            change_type="commerce_updated",
            changed_fields=["stock_status"],
            new_data={"stock_status": "out_of_stock"},
            recorded_at=utc_now(),
        )
        db_session.add(ch3)
        await db_session.commit()
        await db_session.refresh(ch3)

        # Fetching page 2 with d1["next_cursor"] should NOT include ch3 because ch3.seq > window_until
        resp2 = await api_client.get(
            "/api/products/delta", params={"limit": 1, "cursor": d1["next_cursor"]}
        )
        assert resp2.status_code == 200
        d2 = resp2.json()
        assert len(d2["items"]) == 1
        assert d2["items"][0]["seq"] == ch2.seq
        assert d2["has_more"] is False
        assert d2["next_cursor"] is None

        # 4. Starting a new window from ch2.seq reveals ch3
        resp3 = await api_client.get(
            "/api/products/delta", params={"from_seq": ch2.seq, "limit": 10}
        )
        assert resp3.status_code == 200
        d3 = resp3.json()
        assert len(d3["items"]) == 1
        assert d3["items"][0]["seq"] == ch3.seq
        assert d3["has_more"] is False

    @pytest.mark.asyncio
    async def test_delta_malformed_cursor(self, api_client: AsyncClient) -> None:
        response = await api_client.get("/api/products/delta", params={"cursor": "invalid-b64-!!!"})
        assert response.status_code == 400
        data = response.json()
        assert data["error"]["code"] == "invalid_cursor"


class TestJobTriggerEndpoints:
    """Tests for protected POST /api/jobs/discovery and POST /api/jobs/stock."""

    @pytest.mark.asyncio
    async def test_trigger_without_api_key_unauthorized(self, api_client: AsyncClient) -> None:
        resp = await api_client.post("/api/jobs/discovery")
        assert resp.status_code == 401
        data = resp.json()
        assert data["error"]["code"] == "unauthorized"

    @pytest.mark.asyncio
    async def test_trigger_with_invalid_api_key_unauthorized(self, api_client: AsyncClient) -> None:
        resp = await api_client.post(
            "/api/jobs/discovery", headers={"X-API-Key": "wrong-secret-key"}
        )
        assert resp.status_code == 401
        data = resp.json()
        assert data["error"]["code"] == "unauthorized"

    @pytest.mark.asyncio
    async def test_trigger_discovery_success(self, api_client: AsyncClient) -> None:
        resp = await api_client.post(
            "/api/jobs/discovery", headers={"X-API-Key": "test-secret-job-key"}
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["type"] == "discovery"
        assert data["status"] == "queued"
        assert "job_id" in data

    @pytest.mark.asyncio
    async def test_trigger_stock_success(self, api_client: AsyncClient) -> None:
        resp = await api_client.post(
            "/api/jobs/stock", headers={"X-API-Key": "test-secret-job-key"}
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["type"] == "stock"
        assert data["status"] == "queued"
        assert "job_id" in data

    @pytest.mark.asyncio
    async def test_trigger_deduplication(
        self, api_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        # First trigger creates queued job
        resp1 = await api_client.post(
            "/api/jobs/discovery", headers={"X-API-Key": "test-secret-job-key"}
        )
        assert resp1.status_code == 202
        job_id_1 = resp1.json()["job_id"]

        # Second trigger detects existing queued job and returns it
        resp2 = await api_client.post(
            "/api/jobs/discovery", headers={"X-API-Key": "test-secret-job-key"}
        )
        assert resp2.status_code == 202
        job_id_2 = resp2.json()["job_id"]

        assert job_id_1 == job_id_2
