"""FastAPI API routes covering System, Catalog, and Jobs endpoints."""

import base64
import json
import logging
import uuid
from typing import Annotated, Any
from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.api.deps import get_db, verify_job_api_key, verify_read_access
from app.api.schemas import (
    CategoryOut,
    CategoryProductsResponse,
    DeltaPageResponse,
    HealthResponse,
    JobOut,
    JobTriggerResponse,
    ProductChangeOut,
    ProductOut,
    ProductsPageResponse,
    StatsResponse,
)
from app.config import Settings, get_settings
from app.db.lock import lock_change_readers
from app.db.migrations import get_expected_alembic_head
from app.db.models import Category, Job, Product, ProductCategory, ProductChange, Run
from app.jobs.queue import JobQueue

logger = logging.getLogger(__name__)

router = APIRouter()


def encode_delta_cursor(from_seq: int, until_seq: int, last_seq: int) -> str:
    """Encode delta window cursor into an opaque base64 string."""
    raw = json.dumps({"from": from_seq, "until": until_seq, "last": last_seq})
    return base64.b64encode(raw.encode("utf-8")).decode("utf-8")


def decode_delta_cursor(cursor: str) -> tuple[int, int, int]:
    """Decode delta window cursor into (from_seq, until_seq, last_seq)."""
    try:
        raw = base64.b64decode(cursor).decode("utf-8")
        data = json.loads(raw)
        return int(data["from"]), int(data["until"]), int(data["last"])
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "code": "invalid_cursor",
                    "message": "Malformed delta cursor",
                    "details": {},
                }
            },
        ) from None


# ---------------- System Endpoints ----------------
@router.get(
    "/live",
    tags=["System"],
    summary="Liveness probe",
    description="Returns 200 whenever the web process is up. Does not touch the database; use it as the platform health-check path. Use /health for real readiness.",
)
async def liveness() -> dict[str, str]:
    return {"status": "alive"}


@router.get(
    "/health",
    response_model=HealthResponse,
    tags=["System"],
    summary="Readiness & Diagnostic Status",
    description="Reports readiness: database connectivity, deployed commit, applied vs. expected Alembic revision, and last successful discovery/stock runs, without any request to the source site. Returns HTTP 503 with status 'degraded' when the database is not configured, unreachable, or (in production) not migrated to the shipped head.",
)
async def health_check(
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HealthResponse:
    expected_head = get_expected_alembic_head()
    alembic_rev = "unknown"
    last_disc: Any = None
    last_stock: Any = None

    if not settings.database_configured:
        db_status = "not_configured"
    else:
        db_status = "ok"
        try:
            await db.execute(text("SELECT 1"))
        except Exception:
            logger.exception("Database health check failed")
            db_status = "unavailable"

    if db_status == "ok":
        try:
            rev_val = (
                await db.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))
            ).scalar()
            if rev_val:
                alembic_rev = str(rev_val)
        except Exception:
            await db.rollback()
        try:
            last_disc = (
                await db.execute(
                    select(Run.finished_at)
                    .where(Run.type == "discovery", Run.status == "complete")
                    .order_by(Run.finished_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            last_stock = (
                await db.execute(
                    select(Run.finished_at)
                    .where(Run.type == "stock", Run.status == "complete")
                    .order_by(Run.finished_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        except Exception:
            await db.rollback()

    migrations_current: bool | None = None
    if alembic_rev != "unknown" and expected_head:
        migrations_current = alembic_rev == expected_head

    degraded = db_status != "ok"
    if (
        db_status == "ok"
        and settings.ENVIRONMENT == "production"
        and migrations_current is not True
    ):
        degraded = True
    if migrations_current is False:
        degraded = True

    if degraded:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthResponse(
        status="degraded" if degraded else "ok",
        database=db_status,
        git_commit=settings.git_commit_short,
        alembic_revision=alembic_rev,
        expected_alembic_revision=expected_head,
        migrations_current=migrations_current,
        last_successful_discovery=last_disc,
        last_successful_stock=last_stock,
    )


@router.get(
    "/api/stats",
    response_model=StatsResponse,
    tags=["System"],
    dependencies=[Depends(verify_read_access)],
    summary="Catalog Summary Statistics",
    description="Returns aggregate catalog counts: total distinct products, categories, in-stock count, and latest crawl execution timestamps.",
)
async def get_stats(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StatsResponse:
    # Categories count
    cat_count = (await db.execute(select(func.count(Category.id)))).scalar() or 0

    # Products count
    prod_count = (
        await db.execute(select(func.count(Product.id)).where(Product.source == "digisound"))
    ).scalar() or 0

    # In-stock count
    in_stock_count = (
        await db.execute(
            select(func.count(Product.id)).where(
                Product.source == "digisound", Product.stock_status == "in_stock"
            )
        )
    ).scalar() or 0

    # Out-of-stock count
    out_stock_count = (
        await db.execute(
            select(func.count(Product.id)).where(
                Product.source == "digisound", Product.stock_status == "out_of_stock"
            )
        )
    ).scalar() or 0

    # Backorder count
    backorder_count = (
        await db.execute(
            select(func.count(Product.id)).where(
                Product.source == "digisound", Product.stock_status == "backorder"
            )
        )
    ).scalar() or 0

    # Latest runs
    last_disc = (
        await db.execute(
            select(Run.finished_at)
            .where(Run.type == "discovery")
            .order_by(Run.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    last_stock = (
        await db.execute(
            select(Run.finished_at)
            .where(Run.type == "stock")
            .order_by(Run.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    return StatsResponse(
        total_categories=cat_count,
        total_products=prod_count,
        in_stock_products=in_stock_count,
        out_of_stock_products=out_stock_count,
        backorder_products=backorder_count,
        last_discovery_run=last_disc,
        last_stock_run=last_stock,
    )


# ---------------- Catalog Endpoints ----------------
@router.get(
    "/api/categories",
    response_model=list[CategoryOut],
    tags=["Catalog"],
    dependencies=[Depends(verify_read_access)],
    summary="List Discovered Categories",
    description="Returns all verified categories discovered under CAR STEREO, preserving source ID, ordered path hierarchy, and source-reported counts.",
)
async def list_categories(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[Category]:
    query = select(Category).order_by(Category.id.asc())
    result = await db.execute(query)
    return list(result.scalars().all())


@router.get(
    "/api/category/{id}/products",
    response_model=CategoryProductsResponse,
    tags=["Catalog"],
    dependencies=[Depends(verify_read_access)],
    summary="Get Products in Category",
    description="Returns a bounded cursor-paginated list of products associated with the specified category ID.",
)
async def get_category_products(
    id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    cursor: Annotated[str | None, Query(description="Opaque cursor for pagination")] = None,
    limit: Annotated[int, Query(ge=1, le=200, description="Items per page (max 200)")] = 50,
) -> CategoryProductsResponse:
    # 1. Fetch category
    cat_query = select(Category).where(Category.id == id)
    cat = (await db.execute(cat_query)).scalar_one_or_none()
    if not cat:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": {"code": "not_found", "message": f"Category {id} not found", "details": {}}
            },
        )

    # 2. Decode cursor
    last_product_id = 0
    if cursor:
        try:
            last_product_id = int(base64.b64decode(cursor).decode("utf-8"))
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": {
                        "code": "invalid_cursor",
                        "message": "Malformed cursor",
                        "details": {},
                    }
                },
            ) from None

    # 3. Query items
    query = (
        select(Product)
        .join(ProductCategory, ProductCategory.product_id == Product.id)
        .where(
            ProductCategory.category_id == id,
            Product.id > last_product_id,
        )
        .order_by(Product.id.asc())
        .limit(limit + 1)
    )
    result = await db.execute(query)
    products = list(result.scalars().all())

    has_more = len(products) > limit
    items = products[:limit]

    next_cursor = None
    if has_more and items:
        next_cursor = base64.b64encode(str(items[-1].id).encode("utf-8")).decode("utf-8")

    return CategoryProductsResponse(
        category=CategoryOut.model_validate(cat),
        items=[ProductOut.model_validate(p) for p in items],
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.get(
    "/api/products",
    response_model=ProductsPageResponse,
    tags=["Catalog"],
    dependencies=[Depends(verify_read_access)],
    summary="List Catalog Products",
    description="Returns cursor-paginated catalog products with opaque cursor, next_cursor, and has_more indicator.",
)
async def list_products(
    db: Annotated[AsyncSession, Depends(get_db)],
    cursor: Annotated[str | None, Query(description="Opaque cursor for pagination")] = None,
    limit: Annotated[int, Query(ge=1, le=200, description="Items per page (max 200)")] = 50,
) -> ProductsPageResponse:
    last_id = 0
    if cursor:
        try:
            last_id = int(base64.b64decode(cursor).decode("utf-8"))
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": {
                        "code": "invalid_cursor",
                        "message": "Malformed cursor",
                        "details": {},
                    }
                },
            ) from None

    query = (
        select(Product)
        .where(Product.source == "digisound", Product.id > last_id)
        .order_by(Product.id.asc())
        .limit(limit + 1)
    )
    result = await db.execute(query)
    products = list(result.scalars().all())

    has_more = len(products) > limit
    items = products[:limit]

    next_cursor = None
    if has_more and items:
        next_cursor = base64.b64encode(str(items[-1].id).encode("utf-8")).decode("utf-8")

    return ProductsPageResponse(
        items=[ProductOut.model_validate(p) for p in items],
        next_cursor=next_cursor,
        has_more=has_more,
        total_count=None,
    )


@router.get(
    "/api/product/id/{source_product_id}",
    response_model=ProductOut,
    tags=["Catalog"],
    dependencies=[Depends(verify_read_access)],
    summary="Lookup Product by Supplier Product ID",
    description="Fetches a single product record by its unique supplier source_product_id. Returns 404 if not found.",
)
async def get_product_by_id(
    source_product_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Product:
    query = select(Product).where(
        Product.source == "digisound", Product.source_product_id == source_product_id
    )
    product = (await db.execute(query)).scalar_one_or_none()
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": {
                    "code": "not_found",
                    "message": f"Product with source ID {source_product_id!r} not found",
                    "details": {"source_product_id": source_product_id},
                }
            },
        )
    return product


@router.get(
    "/api/product/sku/{sku:path}",
    response_model=ProductOut,
    tags=["Catalog"],
    dependencies=[Depends(verify_read_access)],
    summary="Lookup Product by Exact SKU",
    description="""Matches SKU exactly as served (case, spaces, slashes, and punctuation).

URL-encoded input is supported for complex SKUs containing spaces and slashes:
- Example: `/api/product/sku/GZW%2010-30A`
- Example: `/api/product/sku/HEC%20HD-AUDIO%20USB%20-%20DSP%20PRO%20MK3`

Responses:
- HTTP 200: Single exact match.
- HTTP 404: No product matches the SKU.
- HTTP 409 `ambiguous_sku`: More than one product matches the SKU identically. The candidate list is returned so the caller can select by source_product_id. Arbitrary selection is never performed.""",
)
async def get_product_by_sku(
    sku: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Product:
    # Decode URL-encoded characters (e.g. %20 -> space, %2F -> /)
    decoded_sku = unquote(sku).strip()

    query = select(Product).where(Product.source == "digisound", Product.sku == decoded_sku)
    result = await db.execute(query)
    matches = list(result.scalars().all())

    if not matches:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": {
                    "code": "not_found",
                    "message": f"Product with SKU {decoded_sku!r} not found",
                    "details": {"sku": decoded_sku},
                }
            },
        )

    if len(matches) > 1:
        # Invariant 10: Never select match arbitrarily; return HTTP 409 ambiguous_sku
        candidates = [
            {
                "source_product_id": p.source_product_id,
                "title": p.title,
                "canonical_url": p.canonical_url,
            }
            for p in matches
        ]
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": {
                    "code": "ambiguous_sku",
                    "message": f"Multiple products match exact SKU {decoded_sku!r}",
                    "details": {
                        "sku": decoded_sku,
                        "candidate_count": len(candidates),
                        "candidates": candidates,
                    },
                }
            },
        )

    return matches[0]


# ---------------- Jobs Endpoints ----------------
@router.get(
    "/api/jobs/{job_id}",
    response_model=JobOut,
    tags=["Jobs"],
    dependencies=[Depends(verify_read_access)],
    summary="Inspect Background Job Status",
    description="Returns the status, parameters, lease details, and result summary of an enqueued or finished background job.",
)
async def get_job_status(
    job_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Job:
    query = select(Job).where(Job.id == job_id)
    job = (await db.execute(query)).scalar_one_or_none()
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": {"code": "not_found", "message": f"Job {job_id} not found", "details": {}}
            },
        )
    return job


# ---------------- Delta Sync Endpoints ----------------
@router.get(
    "/api/products/delta",
    response_model=DeltaPageResponse,
    tags=["Sync"],
    dependencies=[Depends(verify_read_access)],
    summary="Incremental Product Changes Feed",
    description="""Cursor-paginated feed of product mutations (creations, content updates, stock changes).
Guarantees window stability: new mutations committed while paginating a window do not leak into the current window.
Passing the same cursor repeatedly returns the exact same page deterministically.""",
)
async def get_products_delta(
    db: Annotated[AsyncSession, Depends(get_db)],
    from_seq: Annotated[
        int, Query(ge=0, description="Sequence floor; changes will have seq > from_seq")
    ] = 0,
    cursor: Annotated[str | None, Query(description="Opaque base64 delta cursor")] = None,
    limit: Annotated[int, Query(ge=1, le=500, description="Page limit")] = 50,
) -> DeltaPageResponse:
    if cursor is not None:
        window_from, window_until, last_seq = decode_delta_cursor(cursor)
    else:
        window_from = from_seq
        # Wait for in-flight change writers so MAX(seq) is a safe window horizon: every lower
        # sequence is then committed or rolled back and can never appear later.
        await lock_change_readers(db)
        max_seq_res = await db.execute(select(func.max(ProductChange.seq)))
        current_max = max_seq_res.scalar_one_or_none()
        window_until = (
            current_max if current_max is not None and current_max >= window_from else window_from
        )
        last_seq = window_from

    query = (
        select(ProductChange)
        .options(joinedload(ProductChange.product))
        .where(
            ProductChange.seq > last_seq,
            ProductChange.seq <= window_until,
        )
        .order_by(ProductChange.seq.asc())
        .limit(limit + 1)
    )
    result = await db.execute(query)
    changes = list(result.scalars().all())

    has_more = len(changes) > limit
    page_items = changes[:limit]

    next_cursor = None
    if has_more and page_items:
        next_cursor = encode_delta_cursor(window_from, window_until, page_items[-1].seq)

    items_out = [
        ProductChangeOut(
            seq=ch.seq,
            product_id=ch.product_id,
            source_product_id=ch.product.source_product_id if ch.product else "",
            sku=ch.product.sku if ch.product else "",
            change_type=ch.change_type,
            changed_fields=ch.changed_fields,
            new_data=ch.new_data,
            recorded_at=ch.recorded_at,
        )
        for ch in page_items
    ]

    return DeltaPageResponse(
        items=items_out,
        window_from=window_from,
        window_until=window_until,
        has_more=has_more,
        next_cursor=next_cursor,
    )


# ---------------- Protected Job Trigger Endpoints ----------------
@router.post(
    "/api/jobs/discovery",
    response_model=JobTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["Jobs"],
    dependencies=[Depends(verify_job_api_key)],
    summary="Trigger Discovery Crawl Job",
    description="Enqueues a background discovery crawl. Protected by secret API_KEY. Returns HTTP 202 with job_id. Deduplicates active jobs.",
)
async def trigger_discovery_job(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JobTriggerResponse:
    queue = JobQueue(db)
    job = await queue.enqueue_job("discovery")
    msg = (
        "Discovery job enqueued"
        if job.status == "queued"
        else "Active discovery job already running"
    )
    return JobTriggerResponse(
        job_id=job.id,
        type=job.type,
        status=job.status,
        message=msg,
    )


@router.post(
    "/api/jobs/stock",
    response_model=JobTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["Jobs"],
    dependencies=[Depends(verify_job_api_key)],
    summary="Trigger Stock Refresh Job",
    description="Enqueues a background stock refresh run. Protected by secret API_KEY. Returns HTTP 202 with job_id. Deduplicates active jobs.",
)
async def trigger_stock_job(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JobTriggerResponse:
    queue = JobQueue(db)
    job = await queue.enqueue_job("stock")
    msg = (
        "Stock refresh job enqueued"
        if job.status == "queued"
        else "Active stock job already running"
    )
    return JobTriggerResponse(
        job_id=job.id,
        type=job.type,
        status=job.status,
        message=msg,
    )
