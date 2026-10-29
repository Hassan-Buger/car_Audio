"""Listing page and detail page response validation functions."""


class SourceParseError(Exception):
    """Base exception for source parsing and validation failures."""

    pass


class ChallengeDetectedError(SourceParseError):
    """Raised when access is blocked by Apache 403, Cloudflare, or anti-bot challenge."""

    pass


class RepeatedPageError(SourceParseError):
    """Raised when a paginated listing returns items identical to the previous page."""

    pass


class UnexpectedEmptyPageError(SourceParseError):
    """Raised when a listing page contains no items while more are expected."""

    pass


class InvalidStatusCodeError(SourceParseError):
    """Raised when the server returns an unexpected HTTP error code."""

    pass


def is_challenge_page(status_code: int, html_text: str) -> bool:
    """Detect whether a response is an access restriction or challenge page."""
    if status_code in (403, 429, 503):
        return True

    lower = html_text.lower()
    challenge_indicators = [
        "403 forbidden",
        "you don't have permission to access",
        "access denied",
        "cf-browser-verification",
        "attention required! | cloudflare",
        "just a moment...",
        "ddos protection by cloudflare",
        "security check to access",
        "bot detection",
    ]
    return any(ind in lower for ind in challenge_indicators)


def validate_listing_response(
    status_code: int,
    html_text: str,
    current_offset: int = 0,
    reported_total: int | None = None,
    current_product_ids: list[str] | None = None,
    previous_product_ids: list[str] | None = None,
) -> None:
    """Validate a listing response strictly.

    HTTP 200 alone is never sufficient evidence of success.
    Enforces:
    1. HTTP status code is 200.
    2. Response is not a challenge, access blocked, or error page.
    3. Not unexpectedly empty if the offset is less than reported total.
    4. Not a repetition of the preceding page.
    """
    if is_challenge_page(status_code, html_text):
        raise ChallengeDetectedError(
            f"Source returned challenge or blocked response (HTTP {status_code})"
        )

    if status_code != 200:
        raise InvalidStatusCodeError(f"Unexpected HTTP status code: {status_code}")

    # Check non-empty when expected
    item_count = len(current_product_ids or [])
    if reported_total is not None and current_offset < reported_total and item_count == 0:
        raise UnexpectedEmptyPageError(
            f"Listing page at offset {current_offset} returned 0 items but reported total is {reported_total}"
        )

    # Check non-repetition
    if (
        current_product_ids
        and previous_product_ids
        and set(current_product_ids) == set(previous_product_ids)
    ):
        raise RepeatedPageError(
            f"Listing page at offset {current_offset} repeated identical product IDs from preceding page"
        )
