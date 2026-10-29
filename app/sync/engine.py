"""Resumable synchronization engine for discovery and stock crawl jobs."""

import logging
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.lock import run_advisory_lock
from app.db.models import Run
from app.db.repo import Repository
from app.normalize.ean import validate_ean
from app.normalize.hashing import compute_commerce_hash, compute_content_hash
from app.normalize.images import normalize_image_urls
from app.normalize.price import extract_currency, normalize_tax_basis, parse_price
from app.normalize.sku import normalize_sku
from app.normalize.specs import normalize_specifications_list
from app.normalize.stock import normalize_stock_status, parse_stock_quantity
from app.scraper.client import ScraperClient
from app.scraper.detail_parser import parse_detail_page
from app.scraper.listing_parser import parse_listing_page
from app.scraper.validators import (
    ChallengeDetectedError,
    SourceParseError,
    validate_listing_response,
)
from app.sync.reconciliation import evaluate_category_completeness

logger = logging.getLogger(__name__)


class SyncEngine:
    """Manages discovery and stock synchronization runs with transactional checkpointing."""

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings | None = None,
        scraper_client: ScraperClient | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.scraper_client = scraper_client or ScraperClient(self.settings)
        self.repo = Repository(self.session)

    async def execute_discovery_run(
        self,
        run_id: uuid.UUID | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> Run:
        """Execute a full or resumed discovery run."""
        async with run_advisory_lock(self.session) as acquired:
            if not acquired:
                raise RuntimeError(
                    "Could not acquire global run advisory lock; another run is active."
                )

            # Create or resume run
            run: Run | None = None
            if run_id:
                run = await self.repo.get_run(run_id)
            if run is None:
                run = await self.repo.create_run(run_type="discovery")
                await self.session.commit()

            active_run_id: uuid.UUID = run.id
            logger.info("Starting discovery run %s", active_run_id)

            # Discover and upsert root category CAR STEREO
            root_cat = await self.repo.get_or_create_category(
                source_id="car-stereo",
                name="CAR STEREO",
                slug="car-stereo",
                canonical_url=f"{self.settings.SOURCE_BASE_URL}/products/car-stereo",
                parent_id=None,
                ordered_path=["car-stereo"],
                source_reported_count=2229,
            )
            await self.session.commit()

            categories = [root_cat]
            total_categories = len(categories)
            completed_categories = 0
            distinct_collected_ids: set[str] = set()

            total_new = 0
            total_updated = 0
            total_unchanged = 0
            failed_requests = 0
            discrepancy_reasons: list[str] = []

            for cat in categories:
                progress = await self.repo.get_or_create_category_progress(active_run_id, cat.id)
                current_offset = progress.next_start_offset
                pages_fetched = progress.pages_fetched
                cat_product_ids: set[str] = set()
                previous_page_ids: list[str] | None = None
                pagination_ended = False

                logger.info(
                    "Processing category %s (start_offset=%s, pages_fetched=%s)",
                    cat.name,
                    current_offset,
                    pages_fetched,
                )

                while not pagination_ended:
                    url = cat.canonical_url
                    params = {"start": current_offset} if current_offset > 0 else None

                    try:
                        response = await self.scraper_client.get(url, params=params)
                        html_text = response.text

                        # Parse listing
                        parsed_page = parse_listing_page(
                            html_text, base_url=self.settings.SOURCE_BASE_URL
                        )
                        current_ids = [p.source_product_id for p in parsed_page.products]

                        # Validate listing
                        validate_listing_response(
                            status_code=response.status_code,
                            html_text=html_text,
                            current_offset=current_offset,
                            reported_total=parsed_page.reported_total or cat.source_reported_count,
                            current_product_ids=current_ids,
                            previous_product_ids=previous_page_ids,
                        )

                        # Persist products transactionally with checkpoint
                        for item in parsed_page.products:
                            cat_product_ids.add(item.source_product_id)
                            distinct_collected_ids.add(item.source_product_id)

                            # Fetch product detail
                            detail_html = html_text
                            try:
                                detail_resp = await self.scraper_client.get(item.url)
                                detail_html = detail_resp.text
                            except ChallengeDetectedError:
                                raise
                            except Exception as e:
                                logger.warning("Failed detail fetch for %s: %s", item.url, e)
                                failed_requests += 1

                            detail = parse_detail_page(
                                detail_html,
                                fallback_url=item.url,
                                base_url=self.settings.SOURCE_BASE_URL,
                            )

                            # Normalize fields
                            sku_clean = normalize_sku(detail.sku or item.sku)
                            ean_val, ean_err = validate_ean(detail.ean_raw)
                            reg_price = parse_price(detail.regular_price_raw or item.price_raw)
                            sale_price = parse_price(detail.sale_price_raw)
                            curr = extract_currency(detail.regular_price_raw or item.price_raw)
                            tax_basis = normalize_tax_basis(detail.price_tax_text)
                            stock_stat = normalize_stock_status(
                                detail.stock_text or item.stock_text
                            )
                            stock_qty = parse_stock_quantity(detail.stock_text or item.stock_text)
                            spec_norm = normalize_specifications_list(detail.specifications)
                            imgs_norm = normalize_image_urls(
                                detail.images or ([item.image_url] if item.image_url else []),
                                base_url=self.settings.SOURCE_BASE_URL,
                            )

                            content_h = compute_content_hash(
                                source_product_id=item.source_product_id,
                                sku=sku_clean,
                                title=detail.title or item.title,
                                brand=detail.brand or item.brand,
                                ean_raw=detail.ean_raw,
                                canonical_url=detail.canonical_url or item.url,
                                short_description=detail.short_description or item.spec_summary,
                                long_description=detail.long_description,
                                specifications=spec_norm,
                                images=imgs_norm,
                                category_paths=[cat.ordered_path],
                            )
                            comm_h = compute_commerce_hash(
                                currency=curr,
                                regular_price=reg_price,
                                sale_price=sale_price,
                                price_tax_basis=tax_basis,
                                stock_quantity=stock_qty,
                                stock_status=stock_stat,
                                availability_text=detail.availability_text or item.stock_text,
                            )

                            prod, change_status = await self.repo.upsert_product(
                                source_product_id=item.source_product_id,
                                sku=sku_clean,
                                title=detail.title or item.title,
                                brand=detail.brand or item.brand,
                                ean_raw=detail.ean_raw,
                                ean_valid=ean_val,
                                ean_error=ean_err,
                                canonical_url=detail.canonical_url or item.url,
                                currency=curr,
                                regular_price=reg_price,
                                sale_price=sale_price,
                                price_tax_basis=tax_basis,
                                stock_quantity=stock_qty,
                                stock_status=stock_stat,
                                availability_text=detail.availability_text or item.stock_text,
                                short_description=detail.short_description or item.spec_summary,
                                long_description=detail.long_description,
                                specifications=spec_norm,
                                images=imgs_norm,
                                category_path=cat.ordered_path,
                                source_category_id=cat.source_id,
                                content_hash=content_h,
                                commerce_hash=comm_h,
                                run_id=active_run_id,
                            )
                            await self.repo.associate_product_category(
                                prod.id, cat.id, ordered_path=cat.ordered_path
                            )

                            if change_status == "created":
                                total_new += 1
                            elif change_status == "unchanged":
                                total_unchanged += 1
                            else:
                                total_updated += 1

                        pages_fetched += 1
                        previous_page_ids = current_ids

                        # Checkpoint page offset
                        if parsed_page.has_next_page and parsed_page.next_start:
                            current_offset = parsed_page.next_start
                            await self.repo.update_category_checkpoint(
                                run_id=active_run_id,
                                category_id=cat.id,
                                next_start_offset=current_offset,
                                pages_fetched=pages_fetched,
                                distinct_products_seen=len(cat_product_ids),
                            )
                            await self.session.commit()
                        else:
                            pagination_ended = True

                    except ChallengeDetectedError as e:
                        logger.error("Challenge detected in category %s: %s", cat.name, e)
                        failed_requests += 1
                        discrepancy_reasons.append(f"Challenge blocked: {e}")
                        pagination_ended = True
                        break
                    except SourceParseError as e:
                        logger.error("Validation error in category %s: %s", cat.name, e)
                        failed_requests += 1
                        discrepancy_reasons.append(f"Parse error: {e}")
                        pagination_ended = True
                        break
                    except Exception as e:
                        logger.exception("Unexpected error in category %s: %s", cat.name, e)
                        failed_requests += 1
                        discrepancy_reasons.append(f"Error: {e}")
                        pagination_ended = True
                        break

                # Finalize category progress
                cat_complete, cat_reason = evaluate_category_completeness(
                    distinct_collected=len(cat_product_ids),
                    source_reported=cat.source_reported_count,
                    failed_requests=failed_requests,
                    pagination_ended=pagination_ended,
                )
                if cat_complete:
                    completed_categories += 1
                    status_str = "complete"
                else:
                    status_str = "incomplete"
                    if cat_reason:
                        discrepancy_reasons.append(cat_reason)

                await self.repo.finalize_category_progress(
                    run_id=active_run_id,
                    category_id=cat.id,
                    status=status_str,
                    distinct_count=len(cat_product_ids),
                    source_reported_count=cat.source_reported_count,
                    discrepancy_note=cat_reason,
                )
                await self.session.commit()

            # Determine final run status
            if completed_categories == total_categories and failed_requests == 0:
                final_status = "complete"
            elif failed_requests > 0 and len(distinct_collected_ids) == 0:
                final_status = "failed"
            else:
                final_status = "incomplete"

            error_summary = "; ".join(discrepancy_reasons) if discrepancy_reasons else None
            final_run = await self.repo.close_run(
                run_id=active_run_id,
                status=final_status,
                total_categories=total_categories,
                completed_categories=completed_categories,
                total_products=len(distinct_collected_ids),
                new_products=total_new,
                updated_products=total_updated,
                unchanged_products=total_unchanged,
                failed_requests=failed_requests,
                error_summary=error_summary,
            )
            await self.session.commit()
            assert final_run is not None
            logger.info("Discovery run %s completed with status=%s", active_run_id, final_status)
            return final_run
