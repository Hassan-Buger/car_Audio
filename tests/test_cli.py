"""Tests for operator CLI commands in app/cli.py."""

import json
from collections.abc import Generator
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from typer.testing import CliRunner

from app.cli import cli_app
from app.config import get_settings
from app.db.base import utc_now
from app.db.models import Category, CategoryProgress, Product, Run

runner = CliRunner()


@pytest.fixture(autouse=True)
def setup_test_db_env(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/digisound_test",
    )
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class TestCliCommands:
    """Test suite for Typer CLI operator commands."""

    @pytest.mark.asyncio
    async def test_reconciliation_csv_command(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        # Create category, run, and progress
        cat = Category(
            source_id="car-stereo",
            name="CAR STEREO",
            slug="car-stereo",
            canonical_url="https://www.digisound.ee/products/car-stereo",
            ordered_path=["car-stereo"],
            source_reported_count=10,
        )
        run1 = Run(
            type="discovery",
            status="complete",
            started_at=utc_now(),
            finished_at=utc_now(),
        )
        db_session.add_all([cat, run1])
        await db_session.commit()
        await db_session.refresh(cat)
        await db_session.refresh(run1)

        prog = CategoryProgress(
            run_id=run1.id,
            category_id=cat.id,
            status="complete",
            next_start_offset=10,
            pages_fetched=1,
            failed_requests=0,
            distinct_products_seen=10,
            source_reported_count=10,
        )
        db_session.add(prog)
        await db_session.commit()

        # Run CLI command to file
        out_file = tmp_path / "recon.csv"
        result = runner.invoke(cli_app, ["reconciliation-csv", "-o", str(out_file)])
        assert result.exit_code == 0
        assert out_file.exists()

        content = out_file.read_text(encoding="utf-8")
        assert "category_id" in content
        assert "car-stereo" in content

    @pytest.mark.asyncio
    async def test_wordpress_export_csv_and_json(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        prod = Product(
            source="digisound",
            source_product_id="test-wp-export-1",
            sku="HELIX DSP / USB",
            title="Helix DSP USB Interface",
            brand="Helix",
            ean_raw="4025144400123",
            ean_valid=True,
            canonical_url="https://www.digisound.ee/products/car-stereo/helix-dsp",
            currency="EUR",
            regular_price=Decimal("499.00"),
            sale_price=Decimal("449.00"),
            price_tax_basis="standard_vat",
            stock_quantity=5,
            stock_status="in_stock",
            availability_text="In Stock",
            specifications=[{"name": "Channels", "value": "8", "unit": None}],
            images=["https://www.digisound.ee/img1.jpg"],
            category_paths=[["car-stereo", "dsp"]],
            source_category_ids=["dsp"],
            content_hash="h1",
            commerce_hash="c1",
        )
        db_session.add(prod)
        await db_session.commit()

        # 1. Test CSV export
        csv_file = tmp_path / "wp_export.csv"
        res_csv = runner.invoke(
            cli_app, ["wordpress-export", "--format", "csv", "-o", str(csv_file)]
        )
        assert res_csv.exit_code == 0
        assert csv_file.exists()
        csv_content = csv_file.read_text(encoding="utf-8")
        assert "HELIX DSP / USB" in csv_content
        assert "499.00" in csv_content
        assert "Helix" in csv_content

        # 2. Test JSON export
        json_file = tmp_path / "wp_export.json"
        res_json = runner.invoke(
            cli_app, ["wordpress-export", "--format", "json", "-o", str(json_file)]
        )
        assert res_json.exit_code == 0
        assert json_file.exists()
        json_data = json.loads(json_file.read_text(encoding="utf-8"))
        assert len(json_data) == 1
        assert json_data[0]["sku"] == "HELIX DSP / USB"
        assert json_data[0]["stock_quantity"] == 5

    @pytest.mark.asyncio
    async def test_unseen_report_invariant_13(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        # Create completed discovery run
        run1 = Run(
            type="discovery",
            status="complete",
            started_at=utc_now(),
            finished_at=utc_now(),
        )
        db_session.add(run1)
        await db_session.commit()
        await db_session.refresh(run1)

        # Create product that was NOT seen in run1
        unseen_prod = Product(
            source="digisound",
            source_product_id="old-prod-unseen",
            sku="OLD-UNSEEN-01",
            title="Discontinued Amplifier",
            canonical_url="https://www.digisound.ee/products/car-stereo/old-amp",
            currency="EUR",
            regular_price=Decimal("199.00"),
            price_tax_basis="standard_vat",
            stock_status="out_of_stock",
            specifications=[],
            images=[],
            category_paths=[["car-stereo"]],
            source_category_ids=["car-stereo"],
            content_hash="h-old",
            commerce_hash="c-old",
            last_seen_run_id=None,  # Not seen in run1!
        )
        db_session.add(unseen_prod)
        await db_session.commit()

        # Run unseen report
        report_file = tmp_path / "unseen.csv"
        res = runner.invoke(cli_app, ["unseen-report", "-o", str(report_file)])
        assert res.exit_code == 0
        assert report_file.exists()
        content = report_file.read_text(encoding="utf-8")
        assert "INVARIANT 13" in content
        assert "OLD-UNSEEN-01" in content
        assert "Discontinued Amplifier" in content

    @pytest.mark.asyncio
    async def test_trigger_command(self, db_session: AsyncSession) -> None:
        res = runner.invoke(cli_app, ["trigger", "discovery"])
        assert res.exit_code == 0
        assert "Job enqueued" in res.stdout
        assert "discovery" in res.stdout

        res_invalid = runner.invoke(cli_app, ["trigger", "nonexistent"])
        assert res_invalid.exit_code != 0

    @pytest.mark.asyncio
    async def test_stats_command(self, db_session: AsyncSession) -> None:
        res = runner.invoke(cli_app, ["stats"])
        assert res.exit_code == 0
        assert "DIGISOUND CATALOG STATISTICS" in res.stdout
        assert "total_products" in res.stdout
