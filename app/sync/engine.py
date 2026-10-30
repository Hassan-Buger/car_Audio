"""Synchronisation engine: category discovery, product crawl and stock refresh.

Run types
---------
``discovery``
    Walks the category tree below the CAR STEREO root (only explicit category navigation is
    followed - manufacturer/model/attribute facets are never treated as categories), pages through
    every category listing, fetches each product detail page once per run, normalises the data and
    upserts it. Per-category progress is checkpointed after every page so a restarted worker
    resumes exactly where the previous one stopped.

``stock``
    Pages through the root listing only and refreshes price/stock of products that already exist.
    It never creates products and never touches descriptive content.

Safety properties
-----------------
* A single run holds a PostgreSQL session advisory lock on a dedicated connection for its whole
  duration (see ``app.db.lock``); a second concurrent run raises ``RunLockHeldError``.
* A product is only written when its **detail page** was fetched successfully. A failed detail
  request is counted, logged to ``request_logs`` and leaves the product untouched; it is never
  replaced by half-known listing data.
* HTTP 200 alone is not trusted: challenge pages, empty pages and repeated pages abort the
  category (or, for access blocks, the whole run) and the run is reported ``incomplete``/``failed``.
* A run is only ``complete`` when every category paged to its end with zero failed requests and
  the distinct product count equals the count the source itself reported for that category.
* Nothing is ever deleted or deactivated.
"""

import inspect
import logging
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from app.config import Settings, get_settings
from app.db.lock import run_advisory_lock
from app.db.models import Category, CategoryProgress, Product, Run
from app.db.repo import Repository
from app.db.session import get_engine
from app.normalize.ean import validate_ean
from app.normalize.hashing import compute_commerce_hash, compute_content_hash
from app.normalize.images import normalize_image_urls
from app.normalize.price import extract_currency, normalize_tax_basis, parse_price
from app.normalize.sku import normalize_sku
from app.normalize.specs import normalize_specifications_list
from app.normalize.stock import normalize_stock_status, parse_stock_quantity
from app.scraper.client import ScraperClient, TransientHttpError
from app.scraper.detail_parser import parse_detail_page
from app.scraper.listing_parser import parse_listing_page
from app.scraper.models import ParsedListingPage, ParsedProductDetail, ParsedProductSummary
from app.scraper.validators import (
    ChallengeDetectedError,
    SourceParseError,
    is_challenge_page,
    validate_listing_response,
)
from app.sync.reconciliation import evaluate_category_completeness

logger = logging.getLogger(__name__)

ROOT_SOURCE_ID = "car-stereo"
MAX_CATEGORY_PASSES = 6
ROOT_URL_PATH = "/products/car-stereo"
ROOT_DEFAULT_NAME = "CAR STEREO"

ProgressCallback = Callable[[dict[str, Any]], Awaitable[None] | None]
RunCreatedCallback = Callable[[Run], Awaitable[None]]


class RunLockHeldError(RuntimeError):
    """Raised when another process already holds the single-run advisory lock."""


class RunInterruptedError(Exception):
    """Raised at a page boundary when a graceful stop was requested; the run stays resumable."""


class DetailFetchError(Exception):
    """A product detail request failed; the product must not be written from listing data."""


@dataclass
class RunStats:
    """Mutable counters for one run (initialised from the persisted run row on resume)."""

    discovered: int = 0
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: int = 0
    unknown_in_stock_run: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass
class CategoryTarget:
    """A category to crawl, with its (already fetched) first listing page when available."""

    category: Category
    url: str
    ordered_path: list[str]
    page0: ParsedListingPage | None = None
    page0_error: str | None = None


@dataclass(frozen=True)
class NormalizedProduct:
    """Product fields merged from listing + detail data and normalised for storage."""

    source_product_id: str
    sku: str
    title: str
    brand: str | None
    ean_raw: str | None
    ean_valid: bool
    ean_error: str | None
    canonical_url: str
    currency: str
    regular_price: Decimal | None
    sale_price: Decimal | None
    price_tax_basis: str
    stock_quantity: int | None
    stock_status: str
    availability_text: str | None
    short_description: str | None
    long_description: str | None
    specifications: list[dict[str, Any]]
    images: list[str]
    content_hash: str
    commerce_hash: str


def _safe_price(raw: str | None) -> Decimal | None:
    try:
        return parse_price(raw)
    except ValueError:
        logger.warning("Unparseable price %r; storing NULL", raw)
        return None


def build_normalized_product(
    item: ParsedProductSummary,
    detail: ParsedProductDetail,
    base_url: str = "https://www.digisound.ee",
) -> NormalizedProduct:
    """Merge a listing card and a successfully fetched detail page into normalised fields.

    The detail page is authoritative; listing data only fills fields the detail page did not
    provide. VAT basis is taken from the page text only - it is never assumed.
    """
    detail_sku = (detail.sku or "").strip()
    sku = normalize_sku(
        item.sku if detail_sku in ("", detail.source_product_id) and item.sku else detail_sku
    )
    title = (
        detail.title if detail.title and detail.title != detail.source_product_id else item.title
    )

    price_raw = detail.regular_price_raw or item.price_raw
    regular_price = _safe_price(price_raw)
    sale_price = _safe_price(detail.sale_price_raw)
    currency = extract_currency(price_raw)
    tax_basis = normalize_tax_basis(detail.price_tax_text)

    # Availability: the listing card carries the real wording ("In stock", "Back order",
    # "Temporarily out of stock", "Discontinued product"). The detail page's schema.org
    # availability is a coarse flag that reports "InStock" even for back-order items, so it is only
    # a fallback. Using the card in both discovery and stock runs keeps them consistent and avoids
    # false commerce changes between the two.
    stock_text = item.stock_text or detail.stock_text
    stock_status = normalize_stock_status(stock_text)
    stock_quantity = parse_stock_quantity(item.stock_text)
    if stock_quantity is None:
        stock_quantity = parse_stock_quantity(detail.stock_text)
    availability_text = item.stock_text or detail.availability_text

    ean_valid, ean_error = validate_ean(detail.ean_raw)
    raw_images = list(detail.images)
    if not raw_images and item.image_url:
        raw_images = [item.image_url]
    images = normalize_image_urls(raw_images, base_url=base_url)
    specifications = normalize_specifications_list(detail.specifications)
    short_description = detail.short_description or item.spec_summary
    brand = detail.brand or item.brand

    content_hash = compute_content_hash(
        source_product_id=item.source_product_id,
        sku=sku,
        title=title,
        brand=brand,
        ean_raw=detail.ean_raw,
        canonical_url=detail.canonical_url,
        short_description=short_description,
        long_description=detail.long_description,
        specifications=specifications,
        images=images,
    )
    commerce_hash = compute_commerce_hash(
        currency=currency,
        regular_price=regular_price,
        sale_price=sale_price,
        price_tax_basis=tax_basis,
        stock_quantity=stock_quantity,
        stock_status=stock_status,
        availability_text=availability_text,
    )
    return NormalizedProduct(
        source_product_id=item.source_product_id,
        sku=sku,
        title=title,
        brand=brand,
        ean_raw=detail.ean_raw,
        ean_valid=ean_valid,
        ean_error=ean_error,
        canonical_url=detail.canonical_url,
        currency=currency,
        regular_price=regular_price,
        sale_price=sale_price,
        price_tax_basis=tax_basis,
        stock_quantity=stock_quantity,
        stock_status=stock_status,
        availability_text=availability_text,
        short_description=short_description,
        long_description=detail.long_description,
        specifications=specifications,
        images=images,
        content_hash=content_hash,
        commerce_hash=commerce_hash,
    )


def apply_listing_price(
    product: Product, listing_price: Decimal
) -> tuple[Decimal | None, Decimal | None]:
    """Decide (regular, sale) after a listing shows the currently payable price.

    The listing shows one price. If it equals what we already treat as the current price nothing
    changes; if the product has no sale price the listing price becomes the regular price; if a
    sale price exists and the listing is lower than the regular price it is the new sale price,
    otherwise the sale ended and the listing price is the regular price.
    """
    regular, sale = product.regular_price, product.sale_price
    current = sale if sale is not None else regular
    if current is not None and current == listing_price:
        return regular, sale
    if sale is None:
        return listing_price, None
    if regular is not None and listing_price < regular:
        return regular, listing_price
    return listing_price, None


class SyncEngine:
    """Runs discovery and stock-refresh crawls against the source."""

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings | None = None,
        scraper_client: ScraperClient | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self._scraper_client = scraper_client
        self._should_stop = should_stop
        self.repo = Repository(session)
        self.base_url = self.settings.SOURCE_BASE_URL.rstrip("/")
        self.root_url = f"{self.base_url}{ROOT_URL_PATH}"

    # ------------------------------------------------------------------ public API
    async def execute_discovery_run(
        self,
        run_id: uuid.UUID | None = None,
        progress_callback: ProgressCallback | None = None,
        run_created_callback: RunCreatedCallback | None = None,
    ) -> Run:
        """Run (or resume) a full discovery crawl."""
        return await self._execute("discovery", run_id, progress_callback, run_created_callback)

    async def execute_stock_run(
        self,
        run_id: uuid.UUID | None = None,
        progress_callback: ProgressCallback | None = None,
        run_created_callback: RunCreatedCallback | None = None,
    ) -> Run:
        """Run (or resume) a stock/price refresh over the root listing."""
        return await self._execute("stock", run_id, progress_callback, run_created_callback)

    # ------------------------------------------------------------------ orchestration
    def _lock_bind(self) -> AsyncEngine:
        bind = self.session.bind
        if isinstance(bind, AsyncEngine):
            return bind
        if isinstance(bind, AsyncConnection):
            return bind.engine
        return get_engine()

    async def _execute(
        self,
        run_type: str,
        run_id: uuid.UUID | None,
        progress_callback: ProgressCallback | None,
        run_created_callback: RunCreatedCallback | None,
    ) -> Run:
        async with run_advisory_lock(self._lock_bind()) as acquired:
            if not acquired:
                raise RunLockHeldError("Another discovery/stock run is already active")

            client = self._scraper_client or ScraperClient(self.settings)
            owns_client = self._scraper_client is None
            run = await self._start_run(run_type, run_id, run_created_callback)
            stats = RunStats(
                discovered=run.total_products_discovered,
                new=run.new_products,
                updated=run.updated_products,
                unchanged=run.unchanged_products,
                failed=run.failed_requests,
            )
            run_pk = run.id
            try:
                if run_type == "discovery":
                    status, summary, total, done = await self._run_discovery(
                        client, run, stats, progress_callback
                    )
                else:
                    status, summary, total, done = await self._run_stock(
                        client, run, stats, progress_callback
                    )
                return await self._close(run_pk, status, total, done, stats, summary)
            except RunInterruptedError:
                logger.info("Run %s interrupted by shutdown; it will resume on restart", run_pk)
                await self.session.commit()
                interrupted = await self.repo.get_run(run_pk)
                assert interrupted is not None
                return interrupted
            except ChallengeDetectedError as exc:
                logger.error("Run %s aborted: source access restricted: %s", run_pk, exc)
                await self.session.rollback()
                return await self._close(
                    run_pk, "failed", 0, 0, stats, f"Source access restricted: {exc}"
                )
            except Exception as exc:
                logger.exception("Run %s failed unexpectedly", run_pk)
                await self.session.rollback()
                return await self._close(
                    run_pk, "failed", 0, 0, stats, f"{type(exc).__name__}: {exc}"[:2000]
                )
            finally:
                if owns_client:
                    await client.close()

    async def _start_run(
        self,
        run_type: str,
        run_id: uuid.UUID | None,
        run_created_callback: RunCreatedCallback | None,
    ) -> Run:
        run: Run | None = None
        if run_id is not None:
            run = await self.repo.get_run(run_id)
            if run is not None and (run.status != "running" or run.type != run_type):
                run = None
        if run is None:
            run = await self.repo.get_resumable_run(run_type)
        if run is None:
            run = await self.repo.create_run(run_type)
            logger.info("Created %s run %s", run_type, run.id)
        else:
            logger.info("Resuming interrupted %s run %s", run_type, run.id)
        await self.session.commit()
        if run_created_callback is not None:
            await run_created_callback(run)
        return run

    async def _close(
        self,
        run_id: uuid.UUID,
        status: str,
        total_categories: int,
        completed_categories: int,
        stats: RunStats,
        summary: str | None,
    ) -> Run:
        run = await self.repo.close_run(
            run_id=run_id,
            status=status,
            total_categories=total_categories,
            completed_categories=completed_categories,
            total_products=stats.discovered,
            new_products=stats.new,
            updated_products=stats.updated,
            unchanged_products=stats.unchanged,
            failed_requests=stats.failed,
            error_summary=summary,
        )
        await self.session.commit()
        assert run is not None
        logger.info(
            "Run %s closed status=%s categories=%s/%s products=%s new=%s updated=%s unchanged=%s "
            "failed_requests=%s",
            run.id,
            status,
            completed_categories,
            total_categories,
            stats.discovered,
            stats.new,
            stats.updated,
            stats.unchanged,
            stats.failed,
        )
        return run

    # ------------------------------------------------------------------ run bodies
    async def _run_discovery(
        self,
        client: ScraperClient,
        run: Run,
        stats: RunStats,
        callback: ProgressCallback | None,
    ) -> tuple[str, str | None, int, int]:
        targets = await self._discover_categories(client, run, stats)
        for target in targets:
            await self._crawl_category(client, run, target, stats, "discovery", callback)
        # The site publishes its category hierarchy only in product breadcrumbs. Every category
        # named by a breadcrumb is crawled through its own listing as well, so membership and
        # counts are checked independently of the breadcrumbs. New products found this way can
        # reveal further categories, hence the bounded fix-point loop.
        await self._crawl_breadcrumb_categories(client, run, targets, stats, callback)
        await self._verify_memberships(run, targets)
        status, summary, total, done = await self._summarise(run, targets, stats)
        if status == "complete":
            shrunk = await self.repo.reconcile_product_categories(run.id)
            await self.session.commit()
            if shrunk:
                summary = (
                    f"{summary}; " if summary else ""
                ) + f"{shrunk} products lost a category membership (logged as categories_updated)"
        return status, summary, total, done

    async def _crawl_breadcrumb_categories(
        self,
        client: ScraperClient,
        run: Run,
        targets: list[CategoryTarget],
        stats: RunStats,
        callback: ProgressCallback | None,
    ) -> None:
        crawled = {t.category.source_id for t in targets}
        for _ in range(MAX_CATEGORY_PASSES):
            pending = await self.repo.get_categories_seen_since(
                run.started_at, ROOT_SOURCE_ID, crawled
            )
            if not pending:
                return
            for category in pending:
                target = CategoryTarget(
                    category=category,
                    url=category.canonical_url,
                    ordered_path=list(category.ordered_path),
                )
                targets.append(target)
                crawled.add(category.source_id)
                await self._crawl_category(client, run, target, stats, "discovery", callback)
        stats.notes.append(
            f"category discovery stopped after {MAX_CATEGORY_PASSES} passes; "
            "further breadcrumb categories may exist"
        )

    async def _verify_memberships(self, run: Run, targets: list[CategoryTarget]) -> None:
        """Cross-check breadcrumb membership against each category's own listing."""
        for target in targets:
            progress = await self.repo.get_or_create_category_progress(run.id, target.category.id)
            if progress.status != "complete":
                continue
            crumb_only, listing_only = await self.repo.membership_differences(
                run.id, target.category.id
            )
            notes: list[str] = []
            if crumb_only:
                progress.status = "incomplete"
                notes.append(
                    f"{crumb_only} products name this category in their breadcrumb but are "
                    "missing from its listing"
                )
            if listing_only:
                notes.append(
                    f"{listing_only} listed products have a breadcrumb elsewhere "
                    "(secondary membership)"
                )
            if notes:
                progress.discrepancy_note = "; ".join(
                    [n for n in [progress.discrepancy_note, *notes] if n]
                )
        await self.session.commit()

    async def _run_stock(
        self,
        client: ScraperClient,
        run: Run,
        stats: RunStats,
        callback: ProgressCallback | None,
    ) -> tuple[str, str | None, int, int]:
        root = await self.repo.get_category_by_source_id(ROOT_SOURCE_ID)
        if root is None:
            raise RuntimeError("Stock run requires a discovered root category; run discovery first")
        target = CategoryTarget(
            category=root, url=root.canonical_url, ordered_path=list(root.ordered_path)
        )
        await self._crawl_category(client, run, target, stats, "stock", callback)
        status, summary, total, done = await self._summarise(run, [target], stats)
        if stats.unknown_in_stock_run:
            note = (
                f"{stats.unknown_in_stock_run} listed products are not in the catalog yet "
                "(stock runs never create products; the next discovery run will add them)"
            )
            summary = f"{summary}; {note}" if summary else note
        return status, summary, total, done

    async def _summarise(
        self, run: Run, targets: list[CategoryTarget], stats: RunStats
    ) -> tuple[str, str | None, int, int]:
        completed = 0
        problems: list[str] = []
        failed_total = 0
        for target in targets:
            progress = await self.repo.get_or_create_category_progress(run.id, target.category.id)
            failed_total += progress.failed_requests
            if progress.status == "complete":
                completed += 1
            else:
                problems.append(
                    f"{target.category.source_id}: {progress.discrepancy_note or progress.status}"
                )
        stats.failed = max(stats.failed, failed_total)
        notes = list(stats.notes)
        if not problems and stats.failed == 0:
            return "complete", ("; ".join(notes)[:2000] if notes else None), len(targets), completed
        summary = "; ".join(problems + notes)[:2000]
        return "incomplete", summary, len(targets), completed

    # ------------------------------------------------------------------ category discovery
    async def _discover_categories(
        self, client: ScraperClient, run: Run, stats: RunStats
    ) -> list[CategoryTarget]:
        """Breadth-first walk of the category tree starting at the root listing page."""
        targets: list[CategoryTarget] = []
        visited: set[str] = set()
        # (url, ordered slug path, name hint from the parent's navigation, depth, parent id)
        queue: deque[tuple[str, list[str], str | None, int, int | None]] = deque()
        queue.append((self.root_url, [ROOT_SOURCE_ID], None, 0, None))

        while queue:
            url, path, name_hint, depth, parent_id = queue.popleft()
            if url in visited:
                continue
            visited.add(url)
            is_root = depth == 0
            source_id = "/".join(path)

            page: ParsedListingPage | None = None
            page_error: str | None = None
            try:
                response = await client.get(url)
                page = parse_listing_page(response.text, base_url=self.base_url, category_url=url)
                validate_listing_response(
                    response.status_code,
                    response.text,
                    0,
                    page.reported_total,
                    [p.source_product_id for p in page.products],
                    None,
                )
            except ChallengeDetectedError:
                raise
            except (SourceParseError, httpx.RequestError, TransientHttpError) as exc:
                if is_root:
                    raise
                page, page_error = None, f"{type(exc).__name__}: {exc}"
                await self.repo.record_request_failure(run.id, url, None, page_error)

            name = (page.title if page and page.title else None) or name_hint
            if not name:
                name = ROOT_DEFAULT_NAME if is_root else path[-1]
            category = await self.repo.get_or_create_category(
                source_id=source_id,
                name=name,
                slug=path[-1],
                canonical_url=url,
                parent_id=parent_id,
                ordered_path=list(path),
                source_reported_count=page.reported_total if page else None,
            )
            targets.append(
                CategoryTarget(
                    category=category,
                    url=url,
                    ordered_path=list(path),
                    page0=page,
                    page0_error=page_error,
                )
            )

            if page is None:
                continue
            if depth >= self.settings.MAX_CATEGORY_DEPTH:
                if page.subcategories:
                    stats.notes.append(
                        f"{source_id}: category depth limit {self.settings.MAX_CATEGORY_DEPTH} "
                        "reached; deeper categories not followed"
                    )
                continue
            for link in page.subcategories:
                queue.append((link.url, [*path, link.slug], link.name, depth + 1, category.id))

        await self.session.commit()
        return targets

    # ------------------------------------------------------------------ category crawl
    async def _crawl_category(
        self,
        client: ScraperClient,
        run: Run,
        target: CategoryTarget,
        stats: RunStats,
        mode: str,
        callback: ProgressCallback | None,
    ) -> CategoryProgress:
        repo = self.repo
        cat = target.category
        progress = await repo.get_or_create_category_progress(run.id, cat.id)
        if progress.status == "complete":
            logger.info("Category %s already complete in run %s; skipping", cat.source_id, run.id)
            return progress

        if target.page0 is None and target.page0_error is not None:
            progress.failed_requests += 1
            await repo.finalize_category_progress(
                run.id,
                cat.id,
                "failed",
                await repo.count_run_category_members(run.id, cat.id),
                cat.source_reported_count,
                f"first page could not be fetched: {target.page0_error}",
            )
            progress.failed_requests = max(progress.failed_requests, 1)
            await self.session.commit()
            return progress

        offset = progress.next_start_offset
        pages = progress.pages_fetched
        failures = progress.failed_requests
        reported = cat.source_reported_count
        previous_ids: list[str] | None = None
        page: ParsedListingPage | None = target.page0 if offset == 0 else None
        pagination_ended = False
        note: str | None = None

        while True:
            if page is None:
                try:
                    response = await client.get(
                        target.url, params={"start": offset} if offset else None
                    )
                    page = parse_listing_page(response.text, base_url=self.base_url)
                    if page.reported_total is not None:
                        reported = page.reported_total
                    validate_listing_response(
                        response.status_code,
                        response.text,
                        offset,
                        reported,
                        [p.source_product_id for p in page.products],
                        previous_ids,
                    )
                except ChallengeDetectedError:
                    raise
                except (SourceParseError, httpx.RequestError, TransientHttpError) as exc:
                    failures += 1
                    note = f"pagination stopped at offset {offset}: {type(exc).__name__}: {exc}"
                    logger.error("Category %s: %s", cat.source_id, note)
                    await repo.record_request_failure(
                        run.id, f"{target.url}?start={offset}", None, note
                    )
                    break
            elif page.reported_total is not None:
                reported = page.reported_total

            for item in page.products:
                ok = await self._process_item(client, run, cat, target, item, stats, mode)
                if not ok:
                    failures += 1
                    stats.failed += 1

            pages += 1
            ids = [p.source_product_id for p in page.products]
            distinct = await repo.count_run_category_members(run.id, cat.id)
            next_offset = page.next_start if page.has_next_page and page.next_start else offset
            await repo.update_category_checkpoint(
                run.id,
                cat.id,
                next_start_offset=next_offset,
                pages_fetched=pages,
                distinct_products_seen=distinct,
            )
            progress.failed_requests = failures
            run.total_products_discovered = stats.discovered
            run.new_products = stats.new
            run.updated_products = stats.updated
            run.unchanged_products = stats.unchanged
            run.failed_requests = stats.failed
            await self.session.commit()
            await self._notify(
                callback,
                {
                    "run_id": str(run.id),
                    "category": cat.source_id,
                    "offset": offset,
                    "pages": pages,
                    "distinct_in_category": distinct,
                    "failed_requests": failures,
                },
            )

            if self._should_stop is not None and self._should_stop():
                raise RunInterruptedError()
            if not page.has_next_page:
                pagination_ended = True
                break
            if page.next_start is None or page.next_start <= offset:
                failures += 1
                note = f"pagination did not advance (next_start={page.next_start}, offset={offset})"
                break
            previous_ids = ids
            offset = page.next_start
            page = None

        distinct = await repo.count_run_category_members(run.id, cat.id)
        if reported is not None and reported != cat.source_reported_count:
            cat.source_reported_count = reported
        complete, reason = evaluate_category_completeness(
            distinct_collected=distinct,
            source_reported=reported,
            failed_requests=failures,
            pagination_ended=pagination_ended,
        )
        discrepancy = note or reason
        await repo.finalize_category_progress(
            run.id,
            cat.id,
            "complete" if complete else "incomplete",
            distinct,
            reported,
            discrepancy,
        )
        progress.failed_requests = failures
        await self.session.commit()
        return progress

    @staticmethod
    async def _notify(callback: ProgressCallback | None, payload: dict[str, Any]) -> None:
        if callback is None:
            return
        result = callback(payload)
        if inspect.isawaitable(result):
            await result

    # ------------------------------------------------------------------ per-item processing
    async def _process_item(
        self,
        client: ScraperClient,
        run: Run,
        cat: Category,
        target: CategoryTarget,
        item: ParsedProductSummary,
        stats: RunStats,
        mode: str,
    ) -> bool:
        """Process one listing card. Returns False when the item could not be resolved."""
        if mode == "stock":
            return await self._process_stock_item(run, cat, item, stats)
        return await self._process_discovery_item(client, run, cat, target, item, stats)

    async def _process_discovery_item(
        self,
        client: ScraperClient,
        run: Run,
        cat: Category,
        target: CategoryTarget,
        item: ParsedProductSummary,
        stats: RunStats,
    ) -> bool:
        repo = self.repo
        crumbs: list[tuple[str, str]] = []
        product = await repo.get_product(item.source_product_id)
        if product is None or product.last_seen_run_id != run.id:
            try:
                detail = await self._fetch_detail(client, item)
            except DetailFetchError as exc:
                logger.warning("Detail fetch failed for %s: %s", item.url, exc)
                await repo.record_request_failure(run.id, item.url, None, str(exc))
                return False
            crumbs = list(detail.category_crumbs)
            norm = build_normalized_product(item, detail, self.base_url)
            product, change = await repo.upsert_product(
                source_product_id=norm.source_product_id,
                sku=norm.sku,
                title=norm.title,
                brand=norm.brand,
                ean_raw=norm.ean_raw,
                ean_valid=norm.ean_valid,
                ean_error=norm.ean_error,
                canonical_url=norm.canonical_url,
                currency=norm.currency,
                regular_price=norm.regular_price,
                sale_price=norm.sale_price,
                price_tax_basis=norm.price_tax_basis,
                stock_quantity=norm.stock_quantity,
                stock_status=norm.stock_status,
                availability_text=norm.availability_text,
                short_description=norm.short_description,
                long_description=norm.long_description,
                specifications=norm.specifications,
                images=norm.images,
                category_path=list(target.ordered_path),
                source_category_id=cat.source_id,
                content_hash=norm.content_hash,
                commerce_hash=norm.commerce_hash,
                run_id=run.id,
            )
            stats.discovered += 1
            if change == "created":
                stats.new += 1
            elif change == "unchanged":
                stats.unchanged += 1
            else:
                stats.updated += 1

        created_in_this_run = product.first_seen_at >= run.started_at
        memberships = [(cat.id, list(target.ordered_path), cat.source_id)]
        await repo.add_run_category_member(run.id, cat.id, product.id, "listing")
        for crumb_cat in await self._breadcrumb_categories(crumbs):
            memberships.append((crumb_cat.id, list(crumb_cat.ordered_path), crumb_cat.source_id))
            await repo.add_run_category_member(run.id, crumb_cat.id, product.id, "breadcrumb")
        await repo.associate_product_categories(
            product.id, memberships, log_change=not created_in_this_run
        )
        return True

    async def _breadcrumb_categories(self, crumbs: list[tuple[str, str]]) -> list[Category]:
        """Resolve (creating as needed) every category level named by a detail breadcrumb.

        Each level becomes a category whose source id is the URL path below ``/products/``.
        Categories created this way are crawled later through their own listing.
        """
        root_path = urlparse(self.root_url).path.rstrip("/")
        base_path = root_path.rsplit("/", 1)[0]
        parent_id: int | None = None
        resolved: list[Category] = []
        for name, url in crumbs:
            crumb_path = urlparse(url).path.rstrip("/")
            if crumb_path != root_path and not crumb_path.startswith(root_path + "/"):
                continue
            rel = crumb_path[len(base_path) :].strip("/")
            slugs = [part for part in rel.split("/") if part]
            if not slugs:
                continue
            if rel == ROOT_SOURCE_ID:
                category = await self.repo.get_category_by_source_id(ROOT_SOURCE_ID)
                if category is None:
                    continue
            else:
                category = await self.repo.get_or_create_category(
                    source_id=rel,
                    name=name,
                    slug=slugs[-1],
                    canonical_url=f"{self.base_url}{crumb_path}",
                    parent_id=parent_id,
                    ordered_path=slugs,
                )
            parent_id = category.id
            resolved.append(category)
        return resolved

    async def _fetch_detail(
        self, client: ScraperClient, item: ParsedProductSummary
    ) -> ParsedProductDetail:
        try:
            response = await client.get(item.url)
        except ChallengeDetectedError:
            raise
        except (httpx.RequestError, TransientHttpError) as exc:
            raise DetailFetchError(f"{type(exc).__name__}: {exc}") from exc
        if is_challenge_page(response.status_code, response.text):
            raise ChallengeDetectedError(f"Challenge page on product detail {item.url}")
        if response.status_code != 200:
            raise DetailFetchError(f"HTTP {response.status_code}")
        return parse_detail_page(response.text, fallback_url=item.url, base_url=self.base_url)

    async def _process_stock_item(
        self, run: Run, cat: Category, item: ParsedProductSummary, stats: RunStats
    ) -> bool:
        repo = self.repo
        product = await repo.get_product(item.source_product_id)
        if product is None:
            stats.unknown_in_stock_run += 1
            return True

        listing_price = _safe_price(item.price_raw)
        regular, sale = product.regular_price, product.sale_price
        if listing_price is not None:
            regular, sale = apply_listing_price(product, listing_price)
        currency = extract_currency(item.price_raw, default=product.currency)

        stock_status = product.stock_status
        stock_quantity = product.stock_quantity
        availability_text = product.availability_text
        if item.stock_text:
            # Discovery and stock runs both take availability from the listing card, so the card
            # wording is stored as is ("In stock (outgoing model)", "Arriving in stock: <date>"):
            # a wording change is a real, visible change and cannot flip-flop between run types.
            listed_status = normalize_stock_status(item.stock_text)
            listed_quantity = parse_stock_quantity(item.stock_text)
            status_changed = listed_status != product.stock_status
            if status_changed or listed_quantity is not None:
                stock_quantity = listed_quantity
            stock_status = listed_status
            availability_text = item.stock_text

        commerce_hash = compute_commerce_hash(
            currency=currency,
            regular_price=regular,
            sale_price=sale,
            price_tax_basis=product.price_tax_basis,
            stock_quantity=stock_quantity,
            stock_status=stock_status,
            availability_text=availability_text,
        )
        outcome = await repo.update_commerce(
            product,
            currency=currency,
            regular_price=regular,
            sale_price=sale,
            stock_quantity=stock_quantity,
            stock_status=stock_status,
            availability_text=availability_text,
            commerce_hash=commerce_hash,
        )
        stats.discovered += 1
        if outcome == "unchanged":
            stats.unchanged += 1
        else:
            stats.updated += 1
        await repo.add_run_category_member(run.id, cat.id, product.id)
        return True
