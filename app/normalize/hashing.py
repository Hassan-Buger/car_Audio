"""Deterministic content and commerce hashing for change detection."""

import hashlib
import json
from decimal import Decimal
from typing import Any


def _serialize_for_hash(obj: Any) -> Any:
    """Recursively convert data structures (Decimals, sets, etc.) to canonical JSON-serializable types."""
    if isinstance(obj, Decimal):
        return f"{obj:.2f}"
    if isinstance(obj, (set, tuple)):
        return [_serialize_for_hash(item) for item in obj]
    if isinstance(obj, list):
        return [_serialize_for_hash(item) for item in obj]
    if isinstance(obj, dict):
        return {k: _serialize_for_hash(v) for k, v in sorted(obj.items())}
    return obj


def compute_content_hash(
    source_product_id: str,
    sku: str,
    title: str,
    brand: str | None,
    ean_raw: str | None,
    canonical_url: str,
    short_description: str | None,
    long_description: str | None,
    specifications: list[dict[str, Any]],
    images: list[str],
    category_paths: list[list[str]],
) -> str:
    """Compute SHA-256 hash over product descriptive/content fields.

    Only changes to these content fields will trigger a content hash update.
    """
    content_payload = {
        "source_product_id": source_product_id,
        "sku": sku,
        "title": title,
        "brand": brand or "",
        "ean_raw": ean_raw or "",
        "canonical_url": canonical_url,
        "short_description": short_description or "",
        "long_description": long_description or "",
        "specifications": specifications,
        "images": images,
        "category_paths": category_paths,
    }

    serialized = json.dumps(
        _serialize_for_hash(content_payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def compute_commerce_hash(
    currency: str,
    regular_price: Decimal | None,
    sale_price: Decimal | None,
    price_tax_basis: str,
    stock_quantity: int | None,
    stock_status: str,
    availability_text: str | None,
) -> str:
    """Compute SHA-256 hash over commerce and inventory fields.

    Only changes to price or stock data will trigger a commerce hash update.
    """
    commerce_payload = {
        "currency": currency,
        "regular_price": regular_price,
        "sale_price": sale_price,
        "price_tax_basis": price_tax_basis,
        "stock_quantity": stock_quantity,
        "stock_status": stock_status,
        "availability_text": availability_text or "",
    }

    serialized = json.dumps(
        _serialize_for_hash(commerce_payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
