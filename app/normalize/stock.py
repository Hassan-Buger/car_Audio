"""Pure normalization functions for stock status and numeric quantity."""

import re

STOCK_STATUS_IN_STOCK = "in_stock"
STOCK_STATUS_OUT_OF_STOCK = "out_of_stock"
STOCK_STATUS_BACKORDER = "backorder"
STOCK_STATUS_UNKNOWN = "unknown"


def normalize_stock_status(raw_status: str | None) -> str:
    """Map raw stock / availability text into standard statuses.

    Distinctly separates backorder from out of stock.
    Returns: 'in_stock', 'out_of_stock', 'backorder', or 'unknown'.
    """
    if not raw_status:
        return STOCK_STATUS_UNKNOWN

    lower = raw_status.strip().lower()
    if not lower:
        return STOCK_STATUS_UNKNOWN

    # Backorder checks (must be checked before generic out of stock)
    backorder_indicators = [
        "backorder",
        "back order",
        "back-order",
        "tellimisel",
        "järeltellimus",
        "pre-order",
        "preorder",
        "tellitav",
        "telli",
    ]
    if any(ind in lower for ind in backorder_indicators):
        return STOCK_STATUS_BACKORDER

    # Out of stock checks
    out_of_stock_indicators = [
        "out of stock",
        "temporarily out of stock",
        "ajutiselt otsas",
        "otsas",
        "läbimüüdud",
        "ei ole laos",
        "pole laos",
        "sold out",
        "unavailable",
        "not available",
        "discontinued",
        "lõpetatud",
    ]
    if any(ind in lower for ind in out_of_stock_indicators):
        return STOCK_STATUS_OUT_OF_STOCK

    # In stock checks
    in_stock_indicators = [
        "in stock",
        "laos",
        "saadaval",
        "kohe olemas",
        "available",
        "olemas",
    ]
    if any(ind in lower for ind in in_stock_indicators):
        return STOCK_STATUS_IN_STOCK

    return STOCK_STATUS_UNKNOWN


def parse_stock_quantity(raw_text: str | None) -> int | None:
    """Extract numeric stock quantity if explicitly stated in text.

    Returns:
    - Integer quantity if stated (e.g. '5 tk', 'Laos 3 tk' -> 3).
    - None if quantity is not explicitly numbered (Invariant: NULL, not 0).
    """
    if not raw_text:
        return None

    cleaned = raw_text.strip()
    if not cleaned:
        return None

    # Matches patterns like '5 tk', '3tk', 'laos 12 tk', 'Qty: 4', '15 pcs'
    match = re.search(r"(\d+)\s*(?:tk|pcs|items?|unit|tk\.)", cleaned, re.IGNORECASE)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None

    # If the string is purely a number (e.g. '12')
    if cleaned.isdigit():
        return int(cleaned)

    # If text states "in stock" or "out of stock" without numbers, return None (NULL in DB)
    return None
