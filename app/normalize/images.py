"""Pure normalization functions for product images and URLs."""

from urllib.parse import urljoin


def resolve_image_url(url: str, base_url: str = "https://www.digisound.ee") -> str:
    """Resolve a relative or protocol-relative image URL against base URL."""
    cleaned = url.strip()
    if not cleaned:
        return ""

    if cleaned.startswith("//"):
        return "https:" + cleaned
    if cleaned.startswith("http://") or cleaned.startswith("https://"):
        return cleaned

    return urljoin(base_url, cleaned)


def is_placeholder_image(url: str) -> bool:
    """Check if an image URL is a placeholder or loading tracker."""
    lower = url.lower()
    placeholders = [
        "placeholder",
        "blank.gif",
        "loading.gif",
        "spacer.gif",
        "no-image",
        "1x1",
        "data:image",
    ]
    return any(p in lower for p in placeholders)


def normalize_image_urls(
    raw_urls: list[str], base_url: str = "https://www.digisound.ee"
) -> list[str]:
    """Deduplicate and resolve a list of image URLs while preserving original order."""
    resolved_urls: list[str] = []
    seen: set[str] = set()

    for raw in raw_urls:
        if not raw:
            continue
        abs_url = resolve_image_url(raw, base_url=base_url)
        if not abs_url:
            continue
        if is_placeholder_image(abs_url):
            continue
        if abs_url not in seen:
            seen.add(abs_url)
            resolved_urls.append(abs_url)

    return resolved_urls
