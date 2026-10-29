"""Hardening tests for the sync engine: failures, completeness, resume, categories, stock runs."""

import asyncio
from decimal import Decimal

import httpx
import pytest
import respx
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db.models import (
    Category,
    CategoryProgress,
    Product,
    ProductCategory,
    ProductChange,
    RequestLog,
    Run,
    RunCategoryProduct,
)
from app.scraper.client import ScraperClient
from app.sync.engine import RunLockHeldError, SyncEngine
from tests.support import BASE, ROOT, Item, build_settings, detail_html, listing_html

A = Item("alpha-amp", "ALPHA 1", "100,00 €", "In stock")
B = Item("beta-sub", "BETA/2", "200,00 €", "Laos 3 tk")
C = Item("gamma-spk", "GAMMA 3", "300,00 €", "Ajutiselt otsas")


def make_engine(session: AsyncSession, **kw: object) -> SyncEngine:
    settings = build_settings()
    return SyncEngine(session, settings, ScraperClient(settings), **kw)  # type: ignore[arg-type]


def mock_catalog(items: list[Item], total: int | None = None, **detail_kw: object) -> None:
    """Single-page root listing + detail pages for every item."""
    respx.get(ROOT).mock(
        return_value=httpx.Response(200, text=listing_html(items, total or len(items)))
    )
    for it in items:
        respx.get(f"{BASE}{it.url_path}").mock(
            return_value=httpx.Response(200, text=detail_html(it, **detail_kw))  # type: ignore[arg-type]
        )


async def count(session: AsyncSession, model: type) -> int:
    return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


class TestCompletenessAndDynamicCounts:
    @pytest.mark.asyncio
    @respx.mock
    async def test_complete_when_distinct_equals_source_reported(
        self, db_session: AsyncSession
    ) -> None:
        mock_catalog([A, B, C])  # source says 3 products, 3 listed
        run = await make_engine(db_session).execute_discovery_run()
        assert run.status == "complete"
        assert (run.total_categories, run.completed_categories) == (1, 1)
        assert (run.total_products_discovered, run.new_products) == (3, 3)
        cat = (await db_session.execute(select(Category))).scalar_one()
        assert cat.source_reported_count == 3  # dynamic, not a hard-coded constant

    @pytest.mark.asyncio
    @respx.mock
    async def test_discrepancy_makes_run_incomplete_never_complete(
        self, db_session: AsyncSession
    ) -> None:
        mock_catalog([A, B], total=5)  # source claims 5, only 2 listed
        run = await make_engine(db_session).execute_discovery_run()
        assert run.status == "incomplete"
        assert run.completed_categories == 0
        assert "Count discrepancy" in (run.error_summary or "")
        # Products already fetched are kept, nothing is deleted
        assert await count(db_session, Product) == 2

    @pytest.mark.asyncio
    @respx.mock
    async def test_membership_is_exact_per_category(self, db_session: AsyncSession) -> None:
        mock_catalog([A, B])
        run = await make_engine(db_session).execute_discovery_run()
        cat = (await db_session.execute(select(Category))).scalar_one()
        members = await db_session.execute(
            select(func.count()).where(
                RunCategoryProduct.run_id == run.id, RunCategoryProduct.category_id == cat.id
            )
        )
        assert members.scalar_one() == 2


class TestDetailFailures:
    @pytest.mark.asyncio
    @respx.mock
    async def test_failed_detail_is_not_replaced_by_listing_data(
        self, db_session: AsyncSession
    ) -> None:
        mock_catalog([A, B])
        respx.get(f"{BASE}{B.url_path}").mock(return_value=httpx.Response(404, text="gone"))
        run = await make_engine(db_session).execute_discovery_run()

        skus = {p.sku for p in (await db_session.execute(select(Product))).scalars()}
        assert skus == {"ALPHA 1"}  # B was NOT written from its listing card
        assert run.status == "incomplete"
        assert run.failed_requests == 1
        logs = (await db_session.execute(select(RequestLog))).scalars().all()
        assert len(logs) == 1 and BASE + B.url_path == logs[0].url
        assert "HTTP 404" in (logs[0].error or "")
        prog = (await db_session.execute(select(CategoryProgress))).scalar_one()
        assert prog.status == "incomplete" and prog.failed_requests == 1

    @pytest.mark.asyncio
    @respx.mock
    async def test_existing_product_untouched_when_its_detail_fails(
        self, db_session: AsyncSession
    ) -> None:
        mock_catalog([A])
        await make_engine(db_session).execute_discovery_run()
        before = (await db_session.execute(select(Product))).scalar_one()
        before_hash, before_updated = before.content_hash, before.content_updated_at

        respx.get(f"{BASE}{A.url_path}").mock(return_value=httpx.Response(404, text="gone"))
        run2 = await make_engine(db_session).execute_discovery_run()
        after = (await db_session.execute(select(Product))).scalar_one()
        assert run2.status == "incomplete"
        assert after.content_hash == before_hash
        assert after.content_updated_at == before_updated
        assert await count(db_session, Product) == 1  # never deleted or deactivated


class TestBlockedSource:
    @pytest.mark.asyncio
    @respx.mock
    async def test_403_on_first_listing_fails_run_without_retry_storm(
        self, db_session: AsyncSession
    ) -> None:
        route = respx.get(ROOT).mock(return_value=httpx.Response(403, text="Forbidden"))
        run = await make_engine(db_session).execute_discovery_run()
        assert run.status == "failed"
        assert "access restricted" in (run.error_summary or "").lower()
        assert route.call_count == 1  # 403 is never retried
        assert await count(db_session, Product) == 0

    @pytest.mark.asyncio
    @respx.mock
    async def test_challenge_mid_crawl_keeps_prior_products_and_fails_run(
        self, db_session: AsyncSession
    ) -> None:
        respx.get(ROOT, params={"start": 1}).mock(
            return_value=httpx.Response(403, text="Forbidden")
        )
        respx.get(ROOT).mock(
            return_value=httpx.Response(200, text=listing_html([A], total=3, next_start=1))
        )
        respx.get(f"{BASE}{A.url_path}").mock(return_value=httpx.Response(200, text=detail_html(A)))
        run = await make_engine(db_session).execute_discovery_run()
        assert run.status == "failed"
        assert await count(db_session, Product) == 1


class TestPaginationAndResume:
    def _mock_two_pages(self) -> None:
        respx.get(ROOT, params={"start": 1}).mock(
            return_value=httpx.Response(200, text=listing_html([B], total=2))
        )
        respx.get(ROOT).mock(
            return_value=httpx.Response(200, text=listing_html([A], total=2, next_start=1))
        )
        for it in (A, B):
            respx.get(f"{BASE}{it.url_path}").mock(
                return_value=httpx.Response(200, text=detail_html(it))
            )

    @pytest.mark.asyncio
    @respx.mock
    async def test_paginates_to_the_end_and_completes(self, db_session: AsyncSession) -> None:
        self._mock_two_pages()
        run = await make_engine(db_session).execute_discovery_run()
        assert run.status == "complete"
        assert run.total_products_discovered == 2

    @pytest.mark.asyncio
    @respx.mock
    async def test_resume_after_interruption_continues_from_checkpoint(
        self, db_session: AsyncSession
    ) -> None:
        self._mock_two_pages()
        # Worker is told to stop after the first page has been checkpointed
        run = await make_engine(db_session, should_stop=lambda: True).execute_discovery_run()
        assert run.status == "running"  # still resumable, not closed
        prog = (await db_session.execute(select(CategoryProgress))).scalar_one()
        assert prog.next_start_offset == 1 and prog.pages_fetched == 1
        assert await count(db_session, Product) == 1

        calls_before = len(respx.calls)
        resumed = await make_engine(db_session).execute_discovery_run(run_id=run.id)
        assert resumed.id == run.id  # same run, counters preserved
        assert resumed.status == "complete"
        assert (resumed.total_products_discovered, resumed.new_products) == (2, 2)
        fetched = [str(c.request.url) for c in list(respx.calls)[calls_before:]]
        assert f"{ROOT}?start=1" in fetched
        assert not any(u.endswith(A.url_path) for u in fetched)  # page 1 not re-fetched
        assert await count(db_session, Run) == 1

    @pytest.mark.asyncio
    @respx.mock
    async def test_resume_without_run_id_picks_up_interrupted_run(
        self, db_session: AsyncSession
    ) -> None:
        self._mock_two_pages()
        run = await make_engine(db_session, should_stop=lambda: True).execute_discovery_run()
        resumed = await make_engine(db_session).execute_discovery_run()
        assert resumed.id == run.id and resumed.status == "complete"

    @pytest.mark.asyncio
    @respx.mock
    async def test_repeated_page_is_detected(self, db_session: AsyncSession) -> None:
        respx.get(ROOT, params={"start": 1}).mock(
            return_value=httpx.Response(200, text=listing_html([A], total=5, next_start=2))
        )
        respx.get(ROOT).mock(
            return_value=httpx.Response(200, text=listing_html([A], total=5, next_start=1))
        )
        respx.get(f"{BASE}{A.url_path}").mock(return_value=httpx.Response(200, text=detail_html(A)))
        run = await make_engine(db_session).execute_discovery_run()
        assert run.status == "incomplete"
        assert "RepeatedPageError" in (run.error_summary or "")


class TestCategories:
    @pytest.mark.asyncio
    @respx.mock
    async def test_manufacturer_facets_are_not_categories(self, db_session: AsyncSession) -> None:
        from pathlib import Path

        html = (Path(__file__).parent / "fixtures" / "listing_page_185.html").read_text()
        respx.get(ROOT).mock(return_value=httpx.Response(200, text=html))
        respx.get(url__regex=rf"{BASE}/products/car-stereo/.+").mock(
            return_value=httpx.Response(200, text="<html><title>Detail</title></html>")
        )
        run = await make_engine(db_session).execute_discovery_run()
        cats = (await db_session.execute(select(Category))).scalars().all()
        assert [c.source_id for c in cats] == ["car-stereo"]
        assert run.total_categories == 1  # facets and sort links never become categories

    @pytest.mark.asyncio
    @respx.mock
    async def test_subcategories_paths_counts_and_multi_membership(
        self, db_session: AsyncSession
    ) -> None:
        amp_url = f"{BASE}/products/car-stereo/amplifiers"
        respx.get(ROOT).mock(
            return_value=httpx.Response(
                200,
                text=listing_html(
                    [A, B],
                    total=2,
                    subcategories=[("Amplifiers (1)", "/products/car-stereo/amplifiers")],
                ),
            )
        )
        respx.get(amp_url).mock(
            return_value=httpx.Response(
                200,
                text=listing_html(
                    [A], total=1, title="Amplifiers", base_path="/products/car-stereo/amplifiers"
                ),
            )
        )
        for it in (A, B):
            respx.get(f"{BASE}{it.url_path}").mock(
                return_value=httpx.Response(200, text=detail_html(it))
            )

        run = await make_engine(db_session).execute_discovery_run()
        assert run.status == "complete"
        assert (run.total_categories, run.completed_categories) == (2, 2)
        assert run.total_products_discovered == 2  # A counted once despite two categories

        cats = {c.source_id: c for c in (await db_session.execute(select(Category))).scalars()}
        assert set(cats) == {"car-stereo", "car-stereo/amplifiers"}
        amp = cats["car-stereo/amplifiers"]
        assert amp.ordered_path == ["car-stereo", "amplifiers"]
        assert amp.parent_id == cats["car-stereo"].id
        assert amp.name == "Amplifiers" and amp.source_reported_count == 1

        prod_a = (
            await db_session.execute(select(Product).where(Product.sku == "ALPHA 1"))
        ).scalar_one()
        assert sorted(prod_a.category_paths) == [["car-stereo"], ["car-stereo", "amplifiers"]]
        assocs = (
            (
                await db_session.execute(
                    select(ProductCategory).where(ProductCategory.product_id == prod_a.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(assocs) == 2

        # Created and categorised in the same run: no spurious categories_updated change
        changes = (
            (
                await db_session.execute(
                    select(ProductChange).where(ProductChange.product_id == prod_a.id)
                )
            )
            .scalars()
            .all()
        )
        assert [c.change_type for c in changes] == ["created"]

    @pytest.mark.asyncio
    @respx.mock
    async def test_second_run_is_all_unchanged_despite_multi_category_product(
        self, db_session: AsyncSession
    ) -> None:
        """Content hash must not depend on which category was crawled (no false deltas)."""
        respx.get(ROOT).mock(
            return_value=httpx.Response(
                200,
                text=listing_html(
                    [A, B], total=2, subcategories=[("Amps", "/products/car-stereo/amplifiers")]
                ),
            )
        )
        respx.get(f"{BASE}/products/car-stereo/amplifiers").mock(
            return_value=httpx.Response(
                200,
                text=listing_html(
                    [A], total=1, title="Amps", base_path="/products/car-stereo/amplifiers"
                ),
            )
        )
        for it in (A, B):
            respx.get(f"{BASE}{it.url_path}").mock(
                return_value=httpx.Response(200, text=detail_html(it))
            )

        run1 = await make_engine(db_session).execute_discovery_run()
        changes_after_first = await count(db_session, ProductChange)
        run2 = await make_engine(db_session).execute_discovery_run()
        assert run1.status == run2.status == "complete"
        assert (run2.new_products, run2.updated_products, run2.unchanged_products) == (0, 0, 2)
        assert await count(db_session, ProductChange) == changes_after_first

    @pytest.mark.asyncio
    @respx.mock
    async def test_new_category_for_existing_product_logs_categories_updated(
        self, db_session: AsyncSession
    ) -> None:
        mock_catalog([A])
        await make_engine(db_session).execute_discovery_run()  # A only in root

        respx.get(ROOT).mock(
            return_value=httpx.Response(
                200,
                text=listing_html(
                    [A], total=1, subcategories=[("Amps", "/products/car-stereo/amplifiers")]
                ),
            )
        )
        respx.get(f"{BASE}/products/car-stereo/amplifiers").mock(
            return_value=httpx.Response(
                200,
                text=listing_html(
                    [A], total=1, title="Amps", base_path="/products/car-stereo/amplifiers"
                ),
            )
        )
        await make_engine(db_session).execute_discovery_run()
        types = [
            c.change_type
            for c in (
                await db_session.execute(select(ProductChange).order_by(ProductChange.seq))
            ).scalars()
        ]
        assert types == ["created", "categories_updated"]


class TestStockRuns:
    @pytest.mark.asyncio
    @respx.mock
    async def test_stock_run_updates_commerce_only_and_never_creates(
        self, db_session: AsyncSession
    ) -> None:
        mock_catalog([A, B])
        await make_engine(db_session).execute_discovery_run()
        prod_a = (
            await db_session.execute(select(Product).where(Product.sku == "ALPHA 1"))
        ).scalar_one()
        content_ts = prod_a.content_updated_at
        title = prod_a.title

        # price change on A, new product D appears, B unchanged
        D = Item("delta-new", "DELTA 4", "50,00 €", "In stock")
        respx.get(ROOT).mock(
            return_value=httpx.Response(
                200,
                text=listing_html(
                    [Item("alpha-amp", "ALPHA 1", "120,00 €", "In stock"), B, D], total=3
                ),
            )
        )
        run = await make_engine(db_session).execute_stock_run()
        assert run.type == "stock"
        assert (run.updated_products, run.unchanged_products, run.new_products) == (1, 1, 0)
        assert "not in the catalog yet" in (run.error_summary or "")
        await db_session.refresh(prod_a)
        assert prod_a.regular_price == Decimal("120.00")
        assert prod_a.content_updated_at == content_ts and prod_a.title == title
        assert await count(db_session, Product) == 2  # D was NOT created
        assert (
            await db_session.execute(select(Run.type).order_by(Run.started_at))
        ).scalars().all() == ["discovery", "stock"]

    @pytest.mark.asyncio
    @respx.mock
    async def test_stock_run_does_not_flip_flop_on_listing_vs_detail_wording(
        self, db_session: AsyncSession
    ) -> None:
        # Detail says "Laos" (in stock); the listing card says "In stock": same normalised status.
        respx.get(ROOT).mock(return_value=httpx.Response(200, text=listing_html([A], total=1)))
        respx.get(f"{BASE}{A.url_path}").mock(
            return_value=httpx.Response(200, text=detail_html(A, stock="Laos"))
        )
        await make_engine(db_session).execute_discovery_run()
        run = await make_engine(db_session).execute_stock_run()
        assert (run.updated_products, run.unchanged_products) == (0, 1)
        assert await count(db_session, ProductChange) == 1  # only the creation

    @pytest.mark.asyncio
    @respx.mock
    @pytest.mark.parametrize(
        ("card_text", "expected"),
        [
            ("Back order", "backorder"),
            ("Temporarily out of stock", "out_of_stock"),
            ("Discontinued product", "out_of_stock"),
        ],
    )
    async def test_listing_card_availability_wins_over_coarse_detail_flag(
        self, db_session: AsyncSession, card_text: str, expected: str
    ) -> None:
        # Live site: the detail page's schema.org availability says "InStock" even for back-order
        # items while the listing card has the real wording. Discovery and a following stock run
        # must agree, so no false commerce_updated event appears.
        item = Item("brax-x", "BRAX X", "4000,00 €", card_text)
        respx.get(ROOT).mock(return_value=httpx.Response(200, text=listing_html([item], total=1)))
        respx.get(f"{BASE}{item.url_path}").mock(
            return_value=httpx.Response(200, text=detail_html(item, stock="In stock"))
        )
        await make_engine(db_session).execute_discovery_run()
        prod = (await db_session.execute(select(Product))).scalar_one()
        assert prod.stock_status == expected
        assert prod.availability_text == card_text
        run = await make_engine(db_session).execute_stock_run()
        assert (run.updated_products, run.unchanged_products) == (0, 1)
        assert await count(db_session, ProductChange) == 1  # only the creation

    @pytest.mark.asyncio
    @respx.mock
    async def test_stock_status_change_is_detected(self, db_session: AsyncSession) -> None:
        mock_catalog([A])
        await make_engine(db_session).execute_discovery_run()
        respx.get(ROOT).mock(
            return_value=httpx.Response(
                200,
                text=listing_html(
                    [Item("alpha-amp", "ALPHA 1", "100,00 €", "Ajutiselt otsas")], total=1
                ),
            )
        )
        run = await make_engine(db_session).execute_stock_run()
        prod = (await db_session.execute(select(Product))).scalar_one()
        assert run.updated_products == 1
        assert prod.stock_status == "out_of_stock" and prod.stock_quantity is None


class TestVatBasis:
    @pytest.mark.asyncio
    @respx.mock
    async def test_vat_basis_only_taken_from_page_text(self, db_session: AsyncSession) -> None:
        respx.get(ROOT).mock(return_value=httpx.Response(200, text=listing_html([A, B], total=2)))
        respx.get(f"{BASE}{A.url_path}").mock(
            return_value=httpx.Response(200, text=detail_html(A, vat="Sisaldab käibemaksu"))
        )
        respx.get(f"{BASE}{B.url_path}").mock(  # no VAT text at all
            return_value=httpx.Response(200, text=detail_html(B))
        )
        await make_engine(db_session).execute_discovery_run()
        rows = {
            p.sku: p.price_tax_basis for p in (await db_session.execute(select(Product))).scalars()
        }
        assert rows["ALPHA 1"] == "incl_vat"
        assert rows["BETA/2"] == "unknown"  # never assumed


class TestRunLock:
    @pytest.mark.asyncio
    @respx.mock
    async def test_second_run_is_rejected_while_lock_is_held(
        self, db_session: AsyncSession, test_engine: AsyncEngine
    ) -> None:
        from app.db.lock import DIGISOUND_RUN_LOCK_KEY

        mock_catalog([A])
        async with test_engine.connect() as holder:
            await holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": DIGISOUND_RUN_LOCK_KEY})
            with pytest.raises(RunLockHeldError):
                await make_engine(db_session).execute_discovery_run()
            await holder.execute(
                text("SELECT pg_advisory_unlock(:k)"), {"k": DIGISOUND_RUN_LOCK_KEY}
            )
        assert await count(db_session, Run) == 0  # rejected before any run record was created

    @pytest.mark.asyncio
    @respx.mock
    async def test_lock_stays_held_through_commits_and_is_released_after(
        self, db_session: AsyncSession, test_engine: AsyncEngine
    ) -> None:
        """Regression: a session-level lock taken on a pooled AsyncSession leaks/never covers the run."""
        from app.db.lock import DIGISOUND_RUN_LOCK_KEY

        observed: list[bool] = []

        async def probe(_: dict) -> None:
            async with test_engine.connect() as other:
                got = (
                    await other.execute(
                        text("SELECT pg_try_advisory_lock(:k)"), {"k": DIGISOUND_RUN_LOCK_KEY}
                    )
                ).scalar()
                observed.append(bool(got))
                if got:
                    await other.execute(
                        text("SELECT pg_advisory_unlock(:k)"), {"k": DIGISOUND_RUN_LOCK_KEY}
                    )

        respx.get(ROOT, params={"start": 1}).mock(
            return_value=httpx.Response(200, text=listing_html([B], total=2))
        )
        respx.get(ROOT).mock(
            return_value=httpx.Response(200, text=listing_html([A], total=2, next_start=1))
        )
        for it in (A, B):
            respx.get(f"{BASE}{it.url_path}").mock(
                return_value=httpx.Response(200, text=detail_html(it))
            )

        await make_engine(db_session).execute_discovery_run(progress_callback=probe)
        assert observed == [False, False]  # other connections could not take it mid-run (2 commits)
        await probe({})
        assert observed[-1] is True  # and it is free again afterwards
        await asyncio.sleep(0)


class TestLiveMarkupEndToEnd:
    """Discovery over live-style markup records the real category path from breadcrumbs."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_breadcrumb_category_path_is_recorded(self, db_session: AsyncSession) -> None:
        import re
        from pathlib import Path

        fx = Path(__file__).parent / "fixtures"
        listing = (fx / "listing_live_markup.html").read_text(encoding="utf-8")
        # keep only the first card, a single page, and a consistent total
        listing = re.sub(
            r'<li class="products-list-item">.*?</li>', "", listing, count=1, flags=re.S
        )
        listing = re.sub(
            r'<div class="load-more-products-wrapper">.*?</div>', "", listing, flags=re.S
        )
        listing = listing.replace("2229 Products", "1 Products")
        listing = listing.replace("example-amp-4ch", "example-sub-10a")
        detail = (fx / "detail_live_markup.html").read_text(encoding="utf-8")
        respx.get(ROOT).mock(return_value=httpx.Response(200, text=listing))
        for cat_path in ("subwoofers-and-accessories", "subwoofers-and-accessories/subwoofers"):
            respx.get(f"{ROOT}/{cat_path}").mock(return_value=httpx.Response(200, text=listing))
        respx.get(f"{BASE}/products/car-stereo/example-sub-10a").mock(
            return_value=httpx.Response(200, text=detail)
        )

        run = await make_engine(db_session).execute_discovery_run()

        assert run.status == "complete", run.error_summary
        assert run.total_categories == 3  # root + two breadcrumb levels, each crawled
        product = (await db_session.execute(select(Product))).scalar_one()
        assert product.sku == "SUB 10A" and product.brand == "Example brand"
        assert sorted(product.category_paths, key=len) == [
            ["car-stereo"],
            ["car-stereo", "subwoofers-and-accessories"],
            ["car-stereo", "subwoofers-and-accessories", "subwoofers"],
        ]
        cats = {c.source_id: c for c in (await db_session.execute(select(Category))).scalars()}
        assert set(cats) == {
            "car-stereo",
            "car-stereo/subwoofers-and-accessories",
            "car-stereo/subwoofers-and-accessories/subwoofers",
        }
        leaf = cats["car-stereo/subwoofers-and-accessories/subwoofers"]
        parent = cats["car-stereo/subwoofers-and-accessories"]
        assert leaf.parent_id == parent.id
        assert leaf.source_reported_count == 1  # independent count read from its own listing


CAT_X = ("X", "/products/car-stereo/x")
CAT_Y = ("Y", "/products/car-stereo/y")
ROOT_CRUMB = ("CAR STEREO", "/products/car-stereo")


def mock_breadcrumb_world(item: Item, category: tuple[str, str], also_in: list[str]) -> None:
    """Root + category listings and a detail page whose breadcrumb names ``category``."""
    respx.reset()
    respx.get(ROOT).mock(return_value=httpx.Response(200, text=listing_html([item], 1)))
    for slug in {category[1].rsplit("/", 1)[-1], *also_in}:
        url = f"{ROOT}/{slug}"
        respx.get(url).mock(
            return_value=httpx.Response(
                200, text=listing_html([item], 1, base_path=f"/products/car-stereo/{slug}")
            )
        )
    respx.get(f"{BASE}{item.url_path}").mock(
        return_value=httpx.Response(200, text=detail_html(item, crumbs=[ROOT_CRUMB, category]))
    )


class TestCategoryMembershipDeltas:
    @pytest.mark.asyncio
    @respx.mock
    async def test_stable_recrawl_emits_no_events(self, db_session: AsyncSession) -> None:
        mock_breadcrumb_world(A, CAT_X, [])
        first = await make_engine(db_session).execute_discovery_run()
        assert first.status == "complete", first.error_summary
        events = await count(db_session, ProductChange)
        mock_breadcrumb_world(A, CAT_X, [])
        second = await make_engine(db_session).execute_discovery_run()
        assert second.status == "complete", second.error_summary
        assert await count(db_session, ProductChange) == events  # categories are not a false delta

    @pytest.mark.asyncio
    @respx.mock
    async def test_category_only_move_generates_delta_events(
        self, db_session: AsyncSession
    ) -> None:
        mock_breadcrumb_world(A, CAT_X, [])
        assert (await make_engine(db_session).execute_discovery_run()).status == "complete"
        product = (await db_session.execute(select(Product))).scalar_one()
        assert ["car-stereo", "x"] in product.category_paths
        before = int((await db_session.execute(select(func.max(ProductChange.seq)))).scalar_one())

        # Nothing about the product changes except its category: X -> Y
        mock_breadcrumb_world(A, CAT_Y, [])
        run = await make_engine(db_session).execute_discovery_run()
        assert run.status == "complete", run.error_summary

        await db_session.refresh(product)
        assert ["car-stereo", "y"] in product.category_paths
        assert ["car-stereo", "x"] not in product.category_paths
        new_changes = (
            (
                await db_session.execute(
                    select(ProductChange)
                    .where(ProductChange.seq > before)
                    .order_by(ProductChange.seq)
                )
            )
            .scalars()
            .all()
        )
        assert new_changes and {c.change_type for c in new_changes} == {"categories_updated"}
        assert any(c.new_data and c.new_data.get("removed") for c in new_changes)
        memberships = (
            (
                await db_session.execute(
                    select(Category.source_id)
                    .join(ProductCategory, ProductCategory.category_id == Category.id)
                    .where(ProductCategory.product_id == product.id)
                )
            )
            .scalars()
            .all()
        )
        assert sorted(memberships) == ["car-stereo", "car-stereo/y"]

    @pytest.mark.asyncio
    @respx.mock
    async def test_breadcrumb_category_missing_from_listing_is_incomplete(
        self, db_session: AsyncSession
    ) -> None:
        mock_breadcrumb_world(A, CAT_X, [])
        # X's own listing exists but does not contain the product its breadcrumb claims
        respx.get(f"{ROOT}/x").mock(
            return_value=httpx.Response(
                200,
                text=listing_html([B], 1, base_path="/products/car-stereo/x"),
            )
        )
        respx.get(f"{BASE}{B.url_path}").mock(
            return_value=httpx.Response(
                200, text=detail_html(B, crumbs=[ROOT_CRUMB, ("Z", "/products/car-stereo/z")])
            )
        )
        respx.get(f"{ROOT}/z").mock(
            return_value=httpx.Response(
                200, text=listing_html([B], 1, base_path="/products/car-stereo/z")
            )
        )
        run = await make_engine(db_session).execute_discovery_run()
        assert run.status == "incomplete"
        assert "missing from its listing" in (run.error_summary or "")
