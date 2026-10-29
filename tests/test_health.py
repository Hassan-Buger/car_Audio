"""Readiness semantics of /health and pure liveness of /live."""

from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.config import Settings, get_settings
from app.db.migrations import get_expected_alembic_head
from app.main import app

DB = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/digisound_test"


class BrokenSession:
    async def execute(self, *a: object, **k: object) -> None:
        raise RuntimeError("password authentication failed for user secret_user at 10.0.0.5")

    async def rollback(self) -> None:
        return None


@pytest.fixture
async def client_factory(db_session: AsyncSession):  # type: ignore[no-untyped-def]
    clients: list[AsyncClient] = []

    def build(settings: Settings, session: object | None = None) -> AsyncClient:
        async def _db() -> AsyncGenerator[object, None]:
            yield session if session is not None else db_session

        app.dependency_overrides[get_db] = _db
        app.dependency_overrides[get_settings] = lambda: settings
        c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        clients.append(c)
        return c

    yield build
    for c in clients:
        await c.aclose()
    app.dependency_overrides.clear()


class TestLiveness:
    @pytest.mark.asyncio
    async def test_live_is_up_without_database(self, client_factory) -> None:  # type: ignore[no-untyped-def]
        client = client_factory(Settings(_env_file=None), BrokenSession())
        resp = await client.get("/live")
        assert resp.status_code == 200 and resp.json() == {"status": "alive"}


class TestReadiness:
    @pytest.mark.asyncio
    async def test_ok_when_database_reachable(self, client_factory) -> None:  # type: ignore[no-untyped-def]
        resp = await client_factory(Settings(DATABASE_URL=DB, GIT_COMMIT_SHA="abcdef123456")).get(
            "/health"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok" and body["database"] == "ok"
        assert body["git_commit"] == "abcdef1"  # real build commit, not a hard-coded constant

    @pytest.mark.asyncio
    async def test_503_when_database_not_configured(self, client_factory, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.delenv("DATABASE_URL", raising=False)
        resp = await client_factory(Settings(_env_file=None)).get("/health")
        assert resp.status_code == 503
        assert resp.json()["status"] == "degraded" and resp.json()["database"] == "not_configured"

    @pytest.mark.asyncio
    async def test_503_when_database_down_and_no_error_text_leaked(self, client_factory) -> None:  # type: ignore[no-untyped-def]
        resp = await client_factory(Settings(DATABASE_URL=DB), BrokenSession()).get("/health")
        assert resp.status_code == 503
        assert resp.json()["database"] == "unavailable"
        assert "secret_user" not in resp.text and "10.0.0.5" not in resp.text

    @pytest.mark.asyncio
    async def test_503_when_migrations_are_behind(
        self, client_factory, db_session: AsyncSession
    ) -> None:  # type: ignore[no-untyped-def]
        await db_session.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num varchar(32) NOT NULL)")
        )
        await db_session.execute(text("DELETE FROM alembic_version"))
        await db_session.execute(text("INSERT INTO alembic_version VALUES ('21467900a656')"))
        await db_session.commit()
        try:
            resp = await client_factory(Settings(DATABASE_URL=DB)).get("/health")
            assert resp.status_code == 503
            body = resp.json()
            assert body["migrations_current"] is False
            assert body["alembic_revision"] == "21467900a656"
            assert body["expected_alembic_revision"] == get_expected_alembic_head()
        finally:
            await db_session.execute(text("DROP TABLE alembic_version"))
            await db_session.commit()

    @pytest.mark.asyncio
    async def test_production_requires_known_current_revision(self, client_factory) -> None:  # type: ignore[no-untyped-def]
        resp = await client_factory(Settings(DATABASE_URL=DB, ENVIRONMENT="production")).get(
            "/health"
        )
        assert resp.status_code == 503  # test DB is built by create_all: no alembic_version row
