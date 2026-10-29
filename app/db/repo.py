"""Transactional repository layer for database models."""

import logging
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utc_now
from app.db.models import (
    Category,
    CategoryProgress,
    Product,
    ProductCategory,
    ProductChange,
    Run,
)

logger = logging.getLogger(__name__)


class Repository:
    """Encapsulates transactional operations on categories, products, runs, and checkpoints."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ---------------- Category Methods ----------------
    async def get_or_create_category(
        self,
        source_id: str,
        name: str,
        slug: str,
        canonical_url: str,
        parent_id: int | None = None,
        ordered_path: list[str] | None = None,
        source_reported_count: int | None = None,
    ) -> Category:
        """Find an existing category by source_id or create a new one."""
        query = select(Category).where(Category.source_id == source_id)
        result = await self.session.execute(query)
        cat = result.scalar_one_or_none()

        if cat is None:
            cat = Category(
                source_id=source_id,
                name=name,
                slug=slug,
                canonical_url=canonical_url,
                parent_id=parent_id,
                ordered_path=ordered_path or [slug],
                source_reported_count=source_reported_count,
            )
            self.session.add(cat)
            await self.session.flush()
        else:
            cat.name = name
            cat.slug = slug
            cat.canonical_url = canonical_url
            if ordered_path:
                cat.ordered_path = ordered_path
            if source_reported_count is not None:
                cat.source_reported_count = source_reported_count
            cat.last_seen_at = utc_now()
            await self.session.flush()

        return cat

    async def get_all_categories(self) -> list[Category]:
        """Fetch all categories ordered by parent and name."""
        query = select(Category).order_by(Category.id)
        result = await self.session.execute(query)
        return list(result.scalars().all())

    # ---------------- Run Methods ----------------
    async def create_run(self, run_type: str = "discovery") -> Run:
        """Create a new run record."""
        run = Run(
            type=run_type,
            status="running",
            started_at=utc_now(),
        )
        self.session.add(run)
        await self.session.flush()
        return run

    async def get_run(self, run_id: uuid.UUID) -> Run | None:
        """Fetch run by ID."""
        query = select(Run).where(Run.id == run_id)
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def get_active_run(self) -> Run | None:
        """Fetch currently running run if any."""
        query = select(Run).where(Run.status == "running").order_by(Run.started_at.desc())
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def close_run(
        self,
        run_id: uuid.UUID,
        status: str,
        total_categories: int,
        completed_categories: int,
        total_products: int,
        new_products: int,
        updated_products: int,
        unchanged_products: int,
        failed_requests: int = 0,
        error_summary: str | None = None,
    ) -> Run | None:
        """Close a run and record summary metrics."""
        run = await self.get_run(run_id)
        if run:
            run.status = status
            run.finished_at = utc_now()
            run.total_categories = total_categories
            run.completed_categories = completed_categories
            run.total_products_discovered = total_products
            run.new_products = new_products
            run.updated_products = updated_products
            run.unchanged_products = unchanged_products
            run.failed_requests = failed_requests
            run.error_summary = error_summary
            await self.session.flush()
        return run

    # ---------------- Checkpoint & Progress Methods ----------------
    async def get_or_create_category_progress(
        self, run_id: uuid.UUID, category_id: int
    ) -> CategoryProgress:
        """Get or initialize category progress checkpoint for a run."""
        query = select(CategoryProgress).where(
            CategoryProgress.run_id == run_id, CategoryProgress.category_id == category_id
        )
        result = await self.session.execute(query)
        progress = result.scalar_one_or_none()

        if progress is None:
            progress = CategoryProgress(
                run_id=run_id,
                category_id=category_id,
                status="in_progress",
                next_start_offset=0,
                pages_fetched=0,
                failed_requests=0,
                distinct_products_seen=0,
            )
            self.session.add(progress)
            await self.session.flush()

        return progress

    async def update_category_checkpoint(
        self,
        run_id: uuid.UUID,
        category_id: int,
        next_start_offset: int,
        pages_fetched: int,
        distinct_products_seen: int,
    ) -> None:
        """Checkpoint page offset and distinct count."""
        progress = await self.get_or_create_category_progress(run_id, category_id)
        progress.next_start_offset = next_start_offset
        progress.pages_fetched = pages_fetched
        progress.distinct_products_seen = distinct_products_seen
        progress.updated_at = utc_now()
        await self.session.flush()

    async def finalize_category_progress(
        self,
        run_id: uuid.UUID,
        category_id: int,
        status: str,
        distinct_count: int,
        source_reported_count: int | None = None,
        discrepancy_note: str | None = None,
    ) -> None:
        """Mark category progress with final status and reconciliation note."""
        progress = await self.get_or_create_category_progress(run_id, category_id)
        progress.status = status
        progress.distinct_products_seen = distinct_count
        progress.source_reported_count = source_reported_count
        progress.discrepancy_note = discrepancy_note
        progress.updated_at = utc_now()
        await self.session.flush()

    # ---------------- Product Methods ----------------
    async def upsert_product(
        self,
        source_product_id: str,
        sku: str,
        title: str,
        brand: str | None,
        ean_raw: str | None,
        ean_valid: bool,
        ean_error: str | None,
        canonical_url: str,
        currency: str,
        regular_price: Decimal | None,
        sale_price: Decimal | None,
        price_tax_basis: str,
        stock_quantity: int | None,
        stock_status: str,
        availability_text: str | None,
        short_description: str | None,
        long_description: str | None,
        specifications: list[dict[str, Any]],
        images: list[str],
        category_path: list[str],
        source_category_id: str,
        content_hash: str,
        commerce_hash: str,
        run_id: uuid.UUID,
    ) -> tuple[Product, str]:
        """Upsert product record and log delta changes.

        Returns: (product, change_status)
        change_status: 'created', 'content_updated', 'commerce_updated', 'both_updated', 'unchanged'
        """
        now = utc_now()
        query = select(Product).where(
            Product.source == "digisound", Product.source_product_id == source_product_id
        )
        result = await self.session.execute(query)
        product = result.scalar_one_or_none()

        if product is None:
            # New product
            product = Product(
                source="digisound",
                source_product_id=source_product_id,
                sku=sku,
                title=title,
                brand=brand,
                ean_raw=ean_raw,
                ean_valid=ean_valid,
                ean_error=ean_error,
                canonical_url=canonical_url,
                currency=currency,
                regular_price=regular_price,
                sale_price=sale_price,
                price_tax_basis=price_tax_basis,
                stock_quantity=stock_quantity,
                stock_status=stock_status,
                availability_text=availability_text,
                short_description=short_description,
                long_description=long_description,
                specifications=specifications,
                images=images,
                category_paths=[category_path],
                source_category_ids=[source_category_id],
                content_hash=content_hash,
                commerce_hash=commerce_hash,
                content_updated_at=now,
                stock_updated_at=now,
                first_seen_at=now,
                last_seen_at=now,
                last_checked_at=now,
                last_seen_run_id=run_id,
            )
            self.session.add(product)
            await self.session.flush()

            # Record creation change
            change = ProductChange(
                product_id=product.id,
                change_type="created",
                changed_fields=["all"],
                old_data=None,
                new_data={"sku": sku, "title": title, "price": str(regular_price)},
                recorded_at=now,
            )
            self.session.add(change)
            await self.session.flush()
            return product, "created"

        # Existing product: compare hashes
        content_changed = product.content_hash != content_hash
        commerce_changed = product.commerce_hash != commerce_hash

        # Update last seen timestamps regardless
        product.last_seen_at = now
        product.last_checked_at = now
        product.last_seen_run_id = run_id

        # Merge category path if not present
        if category_path not in product.category_paths:
            product.category_paths = list(product.category_paths) + [category_path]
        if source_category_id not in product.source_category_ids:
            product.source_category_ids = list(product.source_category_ids) + [source_category_id]

        if not content_changed and not commerce_changed:
            # Invariant 17: Unchanged records do NOT update update timestamps or log changes
            await self.session.flush()
            return product, "unchanged"

        changed_fields: list[str] = []
        old_data: dict[str, Any] = {}
        new_data: dict[str, Any] = {}
        change_type = "unchanged"

        if content_changed:
            changed_fields.extend(["title", "sku", "specifications", "images", "descriptions"])
            old_data["content_hash"] = product.content_hash
            new_data["content_hash"] = content_hash
            product.title = title
            product.sku = sku
            product.brand = brand
            product.ean_raw = ean_raw
            product.ean_valid = ean_valid
            product.ean_error = ean_error
            product.canonical_url = canonical_url
            product.short_description = short_description
            product.long_description = long_description
            product.specifications = specifications
            product.images = images
            product.content_hash = content_hash
            product.content_updated_at = now

        if commerce_changed:
            changed_fields.extend(["regular_price", "sale_price", "stock_status", "stock_quantity"])
            old_data["commerce_hash"] = product.commerce_hash
            new_data["commerce_hash"] = commerce_hash
            product.currency = currency
            product.regular_price = regular_price
            product.sale_price = sale_price
            product.price_tax_basis = price_tax_basis
            product.stock_quantity = stock_quantity
            product.stock_status = stock_status
            product.availability_text = availability_text
            product.commerce_hash = commerce_hash
            product.stock_updated_at = now

        if content_changed and commerce_changed:
            change_type = "both_updated"
        elif content_changed:
            change_type = "content_updated"
        else:
            change_type = "commerce_updated"

        change = ProductChange(
            product_id=product.id,
            change_type=change_type,
            changed_fields=changed_fields,
            old_data=old_data,
            new_data=new_data,
            recorded_at=now,
        )
        self.session.add(change)
        await self.session.flush()

        return product, change_type

    async def associate_product_category(
        self, product_id: int, category_id: int, ordered_path: list[str]
    ) -> None:
        """Associate a product with a category."""
        query = select(ProductCategory).where(
            ProductCategory.product_id == product_id,
            ProductCategory.category_id == category_id,
        )
        result = await self.session.execute(query)
        assoc = result.scalar_one_or_none()
        if assoc is None:
            assoc = ProductCategory(
                product_id=product_id,
                category_id=category_id,
                ordered_path=ordered_path,
            )
            self.session.add(assoc)
            await self.session.flush()

    async def get_unseen_products(self, completed_run_id: uuid.UUID) -> list[Product]:
        """Fetch all digisound products not seen in the given complete run (for manual review)."""
        query = select(Product).where(
            Product.source == "digisound",
            (Product.last_seen_run_id != completed_run_id) | (Product.last_seen_run_id.is_(None)),
        )
        result = await self.session.execute(query)
        return list(result.scalars().all())
