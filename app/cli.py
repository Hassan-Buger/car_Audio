"""Operator CLI for reporting, export, reconciliation, and manual job triggers."""

import asyncio
import csv
import io
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any

import typer
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings
from app.db.base import utc_now
from app.db.models import Category, CategoryProgress, Product, ProductCategory, Run
from app.jobs.queue import JobQueue
from app.sync.reconciliation import (
    CategoryReconciliationRecord,
    evaluate_category_completeness,
    generate_reconciliation_csv,
)

logger = logging.getLogger("app.cli")

cli_app = typer.Typer(
    name="digisound",
    help="Digisound Car Audio Scraper — CLI Operator Tools and Reports",
    no_args_is_help=True,
)


def run_sync(coro: Any) -> Any:
    """Execute coroutine safely whether or not an event loop is already running in current thread."""
    try:
        asyncio.get_running_loop()
        in_loop = True
    except RuntimeError:
        in_loop = False

    if in_loop:
        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


async def _get_cli_session() -> tuple[AsyncSession, AsyncEngine]:
    """Create isolated async session and engine for CLI command."""
    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL, future=True)
    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    session = session_factory()
    return session, engine


# ---------------- 1. Reconciliation CSV ----------------
async def _run_reconciliation_csv() -> str:
    session, engine = await _get_cli_session()
    try:
        cat_query = select(Category).order_by(Category.id.asc())
        categories = list((await session.execute(cat_query)).scalars().all())

        records: list[CategoryReconciliationRecord] = []
        for cat in categories:
            prog_query = (
                select(CategoryProgress)
                .where(CategoryProgress.category_id == cat.id)
                .order_by(CategoryProgress.updated_at.desc())
                .limit(1)
            )
            prog = (await session.execute(prog_query)).scalar_one_or_none()

            cnt_query = select(func.count(ProductCategory.product_id)).where(
                ProductCategory.category_id == cat.id
            )
            prod_count = (await session.execute(cnt_query)).scalar_one()

            failed = prog.failed_requests if prog else 0
            pag_complete = (prog.status == "complete") if prog else False
            is_complete, reason = evaluate_category_completeness(
                distinct_collected=prod_count,
                source_reported=cat.source_reported_count,
                failed_requests=failed,
                pagination_ended=pag_complete,
            )
            records.append(
                CategoryReconciliationRecord(
                    category_id=cat.id,
                    source_id=cat.source_id,
                    category_path=" > ".join(cat.ordered_path),
                    source_reported_count=cat.source_reported_count,
                    distinct_collected_count=prod_count,
                    pagination_complete=pag_complete,
                    failed_requests=failed,
                    discrepancy_reason=reason,
                )
            )

        return generate_reconciliation_csv(records)
    finally:
        await session.close()
        await engine.dispose()


@cli_app.command("reconciliation-csv")
def reconciliation_csv_cmd(
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="File path to save the reconciliation CSV"),
    ] = None,
) -> None:
    """Generate category-by-category RFC-4180 reconciliation CSV."""
    csv_text = run_sync(_run_reconciliation_csv())
    if output:
        output.write_text(csv_text, encoding="utf-8")
        typer.echo(f"Reconciliation CSV saved to {output}")
    else:
        typer.echo(csv_text)


# ---------------- 2. WordPress / Downstream Export ----------------
async def _run_wordpress_export(export_format: str) -> str:
    session, engine = await _get_cli_session()
    try:
        query = select(Product).where(Product.source == "digisound").order_by(Product.id.asc())
        products = list((await session.execute(query)).scalars().all())

        if export_format.lower() == "json":
            data = [
                {
                    "source_product_id": p.source_product_id,
                    "sku": p.sku,
                    "title": p.title,
                    "brand": p.brand,
                    "ean": p.ean_raw,
                    "ean_valid": p.ean_valid,
                    "canonical_url": p.canonical_url,
                    "regular_price": float(p.regular_price)
                    if p.regular_price is not None
                    else None,
                    "sale_price": float(p.sale_price) if p.sale_price is not None else None,
                    "price_tax_basis": p.price_tax_basis,
                    "currency": p.currency,
                    "stock_quantity": p.stock_quantity,
                    "stock_status": p.stock_status,
                    "availability_text": p.availability_text,
                    "categories": [">".join(path) for path in p.category_paths],
                    "images": p.images,
                    "specifications": p.specifications,
                    "content_hash": p.content_hash,
                    "commerce_hash": p.commerce_hash,
                    "last_seen_at": p.last_seen_at.isoformat(),
                }
                for p in products
            ]
            return json.dumps(data, indent=2, ensure_ascii=False)

        # Default CSV export
        output_buffer = io.StringIO()
        writer = csv.writer(output_buffer)
        writer.writerow(
            [
                "source_product_id",
                "sku",
                "title",
                "brand",
                "ean",
                "ean_valid",
                "canonical_url",
                "regular_price",
                "sale_price",
                "price_tax_basis",
                "currency",
                "stock_quantity",
                "stock_status",
                "availability_text",
                "categories",
                "images",
                "specifications",
                "last_seen_at",
            ]
        )
        for p in products:
            writer.writerow(
                [
                    p.source_product_id,
                    p.sku,
                    p.title,
                    p.brand or "",
                    p.ean_raw or "",
                    "true" if p.ean_valid else "false",
                    p.canonical_url,
                    str(p.regular_price) if p.regular_price is not None else "",
                    str(p.sale_price) if p.sale_price is not None else "",
                    p.price_tax_basis,
                    p.currency,
                    str(p.stock_quantity) if p.stock_quantity is not None else "",
                    p.stock_status,
                    p.availability_text or "",
                    "; ".join([" > ".join(path) for path in p.category_paths]),
                    "|".join(p.images),
                    json.dumps(p.specifications, ensure_ascii=False),
                    p.last_seen_at.isoformat(),
                ]
            )
        return output_buffer.getvalue()
    finally:
        await session.close()
        await engine.dispose()


@cli_app.command("wordpress-export")
def wordpress_export_cmd(
    format: Annotated[
        str,
        typer.Option("--format", "-f", help="Export format: 'csv' or 'json'"),
    ] = "csv",
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="File path to save the export"),
    ] = None,
) -> None:
    """Export product catalog formatted for downstream WordPress/WooCommerce reconciliation."""
    if format.lower() not in ("csv", "json"):
        typer.echo("Error: format must be 'csv' or 'json'", err=True)
        raise typer.Exit(code=1)

    result = run_sync(_run_wordpress_export(format))
    if output:
        output.write_text(result, encoding="utf-8")
        typer.echo(f"Export saved to {output} ({format.upper()})")
    else:
        typer.echo(result)


# ---------------- 3. Unseen Products Report ----------------
async def _run_unseen_report(days_threshold: int, export_format: str) -> tuple[str, int]:
    session, engine = await _get_cli_session()
    try:
        # Check latest completed discovery run
        latest_run_q = (
            select(Run)
            .where(Run.type == "discovery", Run.status == "complete")
            .order_by(Run.started_at.desc())
            .limit(1)
        )
        latest_run = (await session.execute(latest_run_q)).scalar_one_or_none()

        now = utc_now()
        if latest_run:
            unseen_q = (
                select(Product)
                .where(
                    Product.source == "digisound",
                    or_(
                        Product.last_seen_run_id != latest_run.id,
                        Product.last_seen_run_id.is_(None),
                    ),
                )
                .order_by(Product.last_seen_at.asc())
            )
        else:
            cutoff = now - timedelta(days=days_threshold)
            unseen_q = (
                select(Product)
                .where(Product.source == "digisound", Product.last_seen_at < cutoff)
                .order_by(Product.last_seen_at.asc())
            )

        products = list((await session.execute(unseen_q)).scalars().all())

        if export_format.lower() == "json":
            data = [
                {
                    "source_product_id": p.source_product_id,
                    "sku": p.sku,
                    "title": p.title,
                    "canonical_url": p.canonical_url,
                    "first_seen_at": p.first_seen_at.isoformat(),
                    "last_seen_at": p.last_seen_at.isoformat(),
                    "days_unseen": (now - p.last_seen_at).days,
                    "stock_status": p.stock_status,
                    "regular_price": float(p.regular_price)
                    if p.regular_price is not None
                    else None,
                }
                for p in products
            ]
            return json.dumps(data, indent=2, ensure_ascii=False), len(products)

        # Default CSV export
        output_buffer = io.StringIO()
        writer = csv.writer(output_buffer)
        writer.writerow(
            [
                "source_product_id",
                "sku",
                "title",
                "canonical_url",
                "first_seen_at",
                "last_seen_at",
                "days_unseen",
                "stock_status",
                "regular_price",
            ]
        )
        for p in products:
            writer.writerow(
                [
                    p.source_product_id,
                    p.sku,
                    p.title,
                    p.canonical_url,
                    p.first_seen_at.isoformat(),
                    p.last_seen_at.isoformat(),
                    (now - p.last_seen_at).days,
                    p.stock_status,
                    str(p.regular_price) if p.regular_price is not None else "",
                ]
            )
        return output_buffer.getvalue(), len(products)
    finally:
        await session.close()
        await engine.dispose()


@cli_app.command("unseen-report")
def unseen_report_cmd(
    days_threshold: Annotated[
        int,
        typer.Option("--days", "-d", help="Days threshold if no complete run recorded"),
    ] = 7,
    format: Annotated[
        str,
        typer.Option("--format", "-f", help="Report format: 'csv' or 'json'"),
    ] = "csv",
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="File path to save the unseen report"),
    ] = None,
) -> None:
    """Generate report of products unseen during the latest discovery run.

    INVARIANT 13: Unseen products are NEVER automatically deleted or deactivated.
    This report is for operator review only.
    """
    content, count = run_sync(_run_unseen_report(days_threshold, format))

    banner = (
        f"# [INVARIANT 13 REVIEW] Unseen Products Report ({count} products)\n"
        "# NOTE: Products are preserved in the database and never automatically removed.\n"
    )

    if output:
        output.write_text(banner + content, encoding="utf-8")
        typer.echo(f"Unseen products report ({count} items) saved to {output}")
    else:
        typer.echo(banner + content)


# ---------------- 4. Job Triggers ----------------
async def _run_trigger(job_type: str) -> tuple[str, str]:
    session, engine = await _get_cli_session()
    try:
        queue = JobQueue(session)
        job = await queue.enqueue_job(job_type)
        return str(job.id), job.status
    finally:
        await session.close()
        await engine.dispose()


@cli_app.command("trigger")
def trigger_cmd(
    job_type: Annotated[
        str,
        typer.Argument(help="Job type to enqueue: 'discovery' or 'stock'"),
    ],
) -> None:
    """Manually enqueue a background crawl or stock refresh job."""
    normalized_type = job_type.lower().strip()
    if normalized_type not in ("discovery", "stock"):
        typer.echo("Error: job_type must be 'discovery' or 'stock'", err=True)
        raise typer.Exit(code=1)

    job_id, status = run_sync(_run_trigger(normalized_type))
    typer.echo(f"Job enqueued: id={job_id} type={normalized_type} status={status}")


# ---------------- 5. Catalog Stats ----------------
async def _run_stats() -> dict[str, Any]:
    session, engine = await _get_cli_session()
    try:
        total_cats = (await session.execute(select(func.count(Category.id)))).scalar_one()
        total_prods = (
            await session.execute(
                select(func.count(Product.id)).where(Product.source == "digisound")
            )
        ).scalar_one()
        in_stock = (
            await session.execute(
                select(func.count(Product.id)).where(
                    Product.source == "digisound", Product.stock_status == "in_stock"
                )
            )
        ).scalar_one()
        out_of_stock = (
            await session.execute(
                select(func.count(Product.id)).where(
                    Product.source == "digisound", Product.stock_status == "out_of_stock"
                )
            )
        ).scalar_one()
        backorder = (
            await session.execute(
                select(func.count(Product.id)).where(
                    Product.source == "digisound", Product.stock_status == "backorder"
                )
            )
        ).scalar_one()

        last_discovery = (
            await session.execute(
                select(Run.started_at)
                .where(Run.type == "discovery", Run.status == "complete")
                .order_by(Run.started_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        last_stock = (
            await session.execute(
                select(Run.started_at)
                .where(Run.type == "stock", Run.status == "complete")
                .order_by(Run.started_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        return {
            "total_categories": total_cats,
            "total_products": total_prods,
            "in_stock": in_stock,
            "out_of_stock": out_of_stock,
            "backorder": backorder,
            "last_discovery_run": last_discovery.isoformat() if last_discovery else "none",
            "last_stock_run": last_stock.isoformat() if last_stock else "none",
        }
    finally:
        await session.close()
        await engine.dispose()


@cli_app.command("stats")
def stats_cmd() -> None:
    """Print catalog and crawl statistics."""
    stats = run_sync(_run_stats())
    typer.echo("=== DIGISOUND CATALOG STATISTICS ===")
    for k, v in stats.items():
        typer.echo(f"  {k}: {v}")


# ---------------- 6. Worker Runner ----------------
@cli_app.command("run-worker")
def run_worker_cmd() -> None:
    """Start the background worker process loop."""
    from app.worker import main as worker_main

    run_sync(worker_main())


def main() -> None:
    """CLI entrypoint."""
    cli_app()


if __name__ == "__main__":
    main()
