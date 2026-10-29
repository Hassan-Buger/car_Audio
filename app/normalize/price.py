"""Pure normalization functions for pricing and tax basis."""

import re
from decimal import Decimal, InvalidOperation


def parse_price(raw_price: str | None) -> Decimal | None:
    """Parse a localized price string into a Decimal.

    Handles formats such as:
    - '249,00 €' -> Decimal('249.00')
    - '1 249,00' -> Decimal('1249.00')
    - '1.249,00 €' -> Decimal('1249.00')
    - '1249.99' -> Decimal('1249.99')
    - '249 €' -> Decimal('249.00')

    Returns None if raw_price is None or empty.
    Raises ValueError on unparseable/ambiguous strings.
    """
    if raw_price is None:
        return None

    cleaned = raw_price.strip()
    if not cleaned:
        return None

    # Remove currency symbols and letters
    # Preserve digits, commas, dots, minus sign, and spaces
    cleaned = re.sub(r"[^\d.,\s-]", "", cleaned).strip()
    if not cleaned:
        return None

    # Remove internal spaces used as thousands separators (e.g. '1 249,00')
    cleaned = cleaned.replace(" ", "")

    # Handle European decimal comma with dot thousands separator (e.g. '1.249,00')
    if "." in cleaned and "," in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            # Dot is thousands separator, comma is decimal separator
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            # Comma is thousands separator, dot is decimal separator
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        # Single comma -> decimal separator
        cleaned = cleaned.replace(",", ".")

    try:
        val = Decimal(cleaned)
        return val.quantize(Decimal("0.01"))
    except InvalidOperation as e:
        raise ValueError(f"Unable to parse price from string: {raw_price!r}") from e


def extract_currency(raw_price: str | None, default: str = "EUR") -> str:
    """Extract currency code from raw price string."""
    if not raw_price:
        return default

    text = raw_price.upper()
    if "€" in text or "EUR" in text:
        return "EUR"
    if "$" in text or "USD" in text:
        return "USD"
    if "£" in text or "GBP" in text:
        return "GBP"

    return default


def normalize_tax_basis(raw_tax_text: str | None) -> str:
    """Normalize tax basis to 'incl_vat', 'excl_vat', or 'unknown'."""
    if not raw_tax_text:
        return "unknown"

    lower = raw_tax_text.lower()
    if "koos km" in lower or "sisaldab km" in lower or "incl. vat" in lower or "incl vat" in lower:
        return "incl_vat"
    if (
        "ilma km" in lower
        or "ei sisalda km" in lower
        or "excl. vat" in lower
        or "excl vat" in lower
    ):
        return "excl_vat"

    return "unknown"
