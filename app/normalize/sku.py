"""Pure normalization functions for SKU / model codes."""


def normalize_sku(raw_sku: str | None) -> str:
    """Normalize and preserve exact SKU identity.

    Invariant 10: SKU identity is exact, including spaces, case, slashes,
    and punctuation (e.g. 'GZW 10-30A', 'HEC HD-AUDIO USB - DSP PRO MK3').

    Only strips surrounding whitespace artifacts (leading/trailing newlines or spaces).
    Does NOT modify internal spaces, case, or punctuation.
    """
    if raw_sku is None:
        return ""

    return raw_sku.strip()
