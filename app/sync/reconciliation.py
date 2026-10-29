"""Reconciliation and completeness evaluation logic."""

import csv
import io
from dataclasses import dataclass


@dataclass(frozen=True)
class CategoryReconciliationRecord:
    """Record describing category-by-category reconciliation result."""

    category_id: int
    source_id: str
    category_path: str
    source_reported_count: int | None
    distinct_collected_count: int
    pagination_complete: bool
    failed_requests: int
    discrepancy_reason: str | None


def evaluate_category_completeness(
    distinct_collected: int,
    source_reported: int | None,
    failed_requests: int,
    pagination_ended: bool,
) -> tuple[bool, str | None]:
    """Evaluate whether a category is complete.

    Invariant 11: Completeness is never claimed while any category,
    pagination, or product request remains unresolved.
    """
    if failed_requests > 0:
        return False, f"{failed_requests} failed requests during crawl"

    if not pagination_ended:
        return False, "Pagination was interrupted before reaching end-of-catalogue"

    if source_reported is not None:
        if distinct_collected == source_reported:
            return True, None
        return (
            False,
            f"Count discrepancy: collected {distinct_collected} distinct items vs reported {source_reported}",
        )

    # If source did not state a total count but pagination validated cleanly to the end
    return True, None


def generate_reconciliation_csv(records: list[CategoryReconciliationRecord]) -> str:
    """Format category reconciliation records into RFC-4180 CSV text."""
    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow(
        [
            "category_id",
            "source_id",
            "category_path",
            "source_reported_count",
            "distinct_collected_count",
            "pagination_complete",
            "failed_requests",
            "discrepancy_reason",
        ]
    )

    for r in records:
        writer.writerow(
            [
                r.category_id,
                r.source_id,
                r.category_path,
                r.source_reported_count if r.source_reported_count is not None else "",
                r.distinct_collected_count,
                "true" if r.pagination_complete else "false",
                r.failed_requests,
                r.discrepancy_reason or "",
            ]
        )

    return output.getvalue()
