"""Pure validation functions for EAN and GTIN barcodes."""


def validate_ean(raw_ean: str | None) -> tuple[bool, str | None]:
    """Validate EAN / GTIN barcode format and check digit using GS1 standard algorithm.

    Supported lengths: 8 (EAN-8), 12 (UPC-A), 13 (EAN-13), 14 (GTIN-14).
    Returns:
    - (True, None) if valid.
    - (False, error_reason) if missing, invalid length, non-digit, or checksum fails.
    """
    if raw_ean is None:
        return False, "Missing EAN"

    cleaned = raw_ean.strip()
    if not cleaned:
        return False, "Empty EAN"

    if not cleaned.isdigit():
        return False, f"EAN contains non-digit characters: {cleaned!r}"

    length = len(cleaned)
    if length not in (8, 12, 13, 14):
        return False, f"Invalid EAN length {length} (expected 8, 12, 13, or 14)"

    digits = [int(c) for c in cleaned]
    payload = digits[:-1]
    check_digit = digits[-1]

    # GS1 standard algorithm:
    # Starting from right-to-left before check digit, multiply alternately by 3 and 1
    total = 0
    multiply_by_three = True
    for d in reversed(payload):
        total += d * (3 if multiply_by_three else 1)
        multiply_by_three = not multiply_by_three

    calculated_check_digit = (10 - (total % 10)) % 10

    if check_digit != calculated_check_digit:
        return (
            False,
            f"Checksum mismatch: expected {calculated_check_digit}, got {check_digit}",
        )

    return True, None
