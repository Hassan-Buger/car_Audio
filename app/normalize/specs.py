"""Pure normalization functions for technical product specifications."""

import re
from typing import Any


def normalize_specification(name: str, value: str) -> dict[str, Any]:
    """Clean and normalize a specification name/value pair.

    Extracts optional common physical units (W, Ohm, Hz, mm, kg, etc.) if separable.
    """
    cleaned_name = name.strip()
    cleaned_value = value.strip()

    # Extract common unit if present at the end of the value
    # e.g., '100 W', '4 Ohm', '20 Hz - 20 kHz', '12 mm'
    unit_match = re.search(r"^([\d.,\s-]+)\s*([a-zA-ZΩμ]+)$", cleaned_value)
    unit = None
    if unit_match:
        val_part = unit_match.group(1).strip()
        unit_part = unit_match.group(2).strip()
        if unit_part.lower() in (
            "w",
            "ohm",
            "hz",
            "khz",
            "mm",
            "cm",
            "m",
            "kg",
            "g",
            "db",
            "v",
            "a",
            "rms",
        ):
            unit = unit_part
            cleaned_value = val_part

    return {
        "name": cleaned_name,
        "value": cleaned_value,
        "unit": unit,
    }


def normalize_specifications_list(raw_specs: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Normalize a list of raw specification dictionaries."""
    normalized: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str]] = set()

    for item in raw_specs:
        name = item.get("name", "").strip()
        val = item.get("value", "").strip()
        if not name and not val:
            continue
        spec = normalize_specification(name, val)
        # Avoid exact duplicate rows
        key = (spec["name"].lower(), str(spec["value"]).lower(), str(spec["unit"]))
        if key not in seen_keys:
            seen_keys.add(key)
            normalized.append(spec)

    return normalized
