"""SQLAlchemy models for Digisound Car Audio Scraper."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Sequence,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, utc_now

product_changes_seq = Sequence("product_changes_seq")


class Category(Base):
    """Category tree hierarchy model."""

    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    parent_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("categories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    ordered_path: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    source_reported_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    parent: Mapped[Optional["Category"]] = relationship(
        "Category", remote_side=[id], back_populates="children"
    )
    children: Mapped[list["Category"]] = relationship("Category", back_populates="parent")
    products: Mapped[list["ProductCategory"]] = relationship(
        "ProductCategory", back_populates="category", cascade="all, delete-orphan"
    )


class Product(Base):
    """Normalized product model."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(
        String(32), default="digisound", nullable=False, server_default="digisound"
    )
    source_product_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    sku: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    brand: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    ean_raw: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ean_valid: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ean_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    currency: Mapped[str] = mapped_column(
        String(10), default="EUR", nullable=False, server_default="EUR"
    )
    regular_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    sale_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    price_tax_basis: Mapped[str] = mapped_column(
        String(20), default="unknown", nullable=False, server_default="unknown"
    )
    stock_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stock_status: Mapped[str] = mapped_column(
        String(30), default="unknown", nullable=False, server_default="unknown"
    )
    availability_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    short_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    long_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    specifications: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    images: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    category_paths: Mapped[list[list[str]]] = mapped_column(JSONB, nullable=False, default=list)
    source_category_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)

    # Change tracking hashes
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    commerce_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # Timestamps
    content_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    stock_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    last_checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    last_seen_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("source", "source_product_id", name="uq_products_source_product_id"),
        Index("ix_products_source_sku", "source", "sku"),
    )

    categories: Mapped[list["ProductCategory"]] = relationship(
        "ProductCategory", back_populates="product", cascade="all, delete-orphan"
    )
    changes: Mapped[list["ProductChange"]] = relationship(
        "ProductChange", back_populates="product", cascade="all, delete-orphan"
    )


class ProductCategory(Base):
    """Many-to-many product category associations with path preservation."""

    __tablename__ = "product_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("categories.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ordered_path: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)

    __table_args__ = (
        UniqueConstraint(
            "product_id", "category_id", name="uq_product_categories_product_category"
        ),
    )

    product: Mapped["Product"] = relationship("Product", back_populates="categories")
    category: Mapped["Category"] = relationship("Category", back_populates="products")


class ProductChange(Base):
    """Append-only change log backing delta synchronization."""

    __tablename__ = "product_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    seq: Mapped[int] = mapped_column(
        BigInteger,
        product_changes_seq,
        server_default=product_changes_seq.next_value(),
        unique=True,
        nullable=False,
        index=True,
    )
    product_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    change_type: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # created, content_updated, commerce_updated, both_updated
    changed_fields: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    old_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    new_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )

    product: Mapped["Product"] = relationship("Product", back_populates="changes")


class Run(Base):
    """Execution run record for discovery or stock crawling."""

    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    type: Mapped[str] = mapped_column(String(32), nullable=False)  # discovery, stock
    status: Mapped[str] = mapped_column(
        String(32), default="running", nullable=False, index=True
    )  # running, complete, incomplete, failed
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    total_categories: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_categories: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_products_discovered: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    new_products: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_products: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unchanged_products: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_requests: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    category_progress: Mapped[list["CategoryProgress"]] = relationship(
        "CategoryProgress", back_populates="run", cascade="all, delete-orphan"
    )


class CategoryProgress(Base):
    """Per-category progress checkpoint during a run."""

    __tablename__ = "category_progress"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("categories.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False
    )  # pending, in_progress, complete, incomplete, failed
    next_start_offset: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pages_fetched: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_requests: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    distinct_products_seen: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source_reported_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discrepancy_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("run_id", "category_id", name="uq_category_progress_run_category"),
    )

    run: Mapped["Run"] = relationship("Run", back_populates="category_progress")
    category: Mapped["Category"] = relationship("Category")


class RunCategoryProduct(Base):
    """Products observed in a given category during a given run (exact, resumable membership)."""

    __tablename__ = "run_category_products"

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True
    )
    category_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("categories.id", ondelete="CASCADE"), primary_key=True
    )
    product_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("products.id", ondelete="CASCADE"), primary_key=True, index=True
    )


class Job(Base):
    """Worker job queue record."""

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)  # discovery, stock
    status: Mapped[str] = mapped_column(
        String(32), default="queued", nullable=False, index=True
    )  # queued, running, completed, failed
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True
    )
    lease_owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    result_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RequestLog(Base):
    """Diagnostics request log for failed or suspicious source requests."""

    __tablename__ = "request_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    method: Mapped[str] = mapped_column(String(10), default="GET", nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )
