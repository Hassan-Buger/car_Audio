"""Pydantic v2 schemas for API requests and responses."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ErrorDetail(BaseModel):
    """Standard error response shape."""

    code: str = Field(description="Machine-readable error code")
    message: str = Field(description="Human-readable error description")
    details: dict[str, Any] = Field(
        default_factory=dict, description="Additional context or fields"
    )


class ErrorResponse(BaseModel):
    """Envelope for all API error responses."""

    error: ErrorDetail


class CategoryOut(BaseModel):
    """Category tree node schema."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    source_id: str
    name: str
    slug: str
    canonical_url: str
    parent_id: int | None = None
    ordered_path: list[str]
    source_reported_count: int | None = None
    discovered_at: datetime
    last_seen_at: datetime


class ProductOut(BaseModel):
    """Normalized product schema."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str = "digisound"
    source_product_id: str
    sku: str
    title: str
    brand: str | None = None
    ean_raw: str | None = None
    ean_valid: bool
    ean_error: str | None = None
    canonical_url: str
    currency: str = "EUR"
    regular_price: Decimal | None = None
    sale_price: Decimal | None = None
    price_tax_basis: str
    stock_quantity: int | None = None
    stock_status: str
    availability_text: str | None = None
    short_description: str | None = None
    long_description: str | None = None
    specifications: list[dict[str, Any]]
    images: list[str]
    category_paths: list[list[str]]
    source_category_ids: list[str]
    content_hash: str
    commerce_hash: str
    content_updated_at: datetime
    stock_updated_at: datetime
    first_seen_at: datetime
    last_seen_at: datetime
    last_checked_at: datetime


class ProductsPageResponse(BaseModel):
    """Cursor-paginated product catalog response."""

    items: list[ProductOut]
    next_cursor: str | None = Field(None, description="Opaque cursor to fetch next page")
    has_more: bool = Field(description="Whether additional products exist")
    total_count: int | None = Field(None, description="Total matching items if known")


class CategoryProductsResponse(BaseModel):
    """Paginated products in a category."""

    category: CategoryOut
    items: list[ProductOut]
    next_cursor: str | None = None
    has_more: bool


class HealthResponse(BaseModel):
    """Service liveness and diagnostic health status."""

    status: str = Field("ok", description="Service liveness indicator")
    database: str = Field(description="PostgreSQL connectivity status")
    git_commit: str = Field(description="Deployed git commit short SHA")
    alembic_revision: str = Field(description="Active Alembic migration revision")
    last_successful_discovery: datetime | None = Field(
        None, description="Timestamp of last complete discovery run"
    )
    last_successful_stock: datetime | None = Field(
        None, description="Timestamp of last complete stock refresh run"
    )


class StatsResponse(BaseModel):
    """Catalog summary statistics."""

    total_categories: int
    total_products: int
    in_stock_products: int
    out_of_stock_products: int
    backorder_products: int
    last_discovery_run: datetime | None = None
    last_stock_run: datetime | None = None


class JobOut(BaseModel):
    """Worker background job schema."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: str
    status: str
    run_id: uuid.UUID | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    params: dict[str, Any]
    result_summary: dict[str, Any] | None = None
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class JobTriggerResponse(BaseModel):
    """Response returned upon enqueuing a job."""

    job_id: uuid.UUID
    type: str
    status: str
    message: str


class AmbiguousSkuCandidate(BaseModel):
    """Candidate product matching an ambiguous SKU."""

    source_product_id: str
    title: str
    canonical_url: str


class AmbiguousSkuErrorDetail(BaseModel):
    """Details for ambiguous SKU response."""

    sku: str
    candidates: list[AmbiguousSkuCandidate]


class ProductChangeOut(BaseModel):
    """Single change log entry in delta synchronization."""

    model_config = ConfigDict(from_attributes=True)

    seq: int
    product_id: int
    source_product_id: str
    sku: str
    change_type: str
    changed_fields: list[str]
    new_data: dict[str, Any]
    recorded_at: datetime


class DeltaPageResponse(BaseModel):
    """Stable-window cursor response for incremental change synchronization."""

    items: list[ProductChangeOut]
    window_from: int = Field(description="Lower sequence bound (exclusive)")
    window_until: int = Field(
        description="Upper sequence bound snapshotted for this window (inclusive)"
    )
    has_more: bool = Field(description="Whether additional change items remain within this window")
    next_cursor: str | None = Field(
        None, description="Opaque cursor to fetch next change page in window"
    )
