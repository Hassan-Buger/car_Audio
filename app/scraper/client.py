"""Bounded asynchronous HTTP client with concurrency limits, timeouts, and retry."""

import asyncio
import logging
import random
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.config import Settings, get_settings
from app.scraper.validators import ChallengeDetectedError, is_challenge_page

logger = logging.getLogger(__name__)


class TransientHttpError(Exception):
    """Raised for retryable HTTP errors (429, 5xx, timeouts)."""

    pass


class ScraperClient:
    """Asynchronous HTTP client tailored for polite, bounded scraping."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.semaphore = asyncio.Semaphore(self.settings.HTTP_CONCURRENCY)
        self._client: httpx.AsyncClient | None = None

    async def get_client(self) -> httpx.AsyncClient:
        """Get or initialize the underlying httpx AsyncClient."""
        if self._client is None or self._client.is_closed:
            headers = {
                "User-Agent": self.settings.USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "et,en-US;q=0.9,en;q=0.8",
            }
            timeout = httpx.Timeout(
                connect=10.0,
                read=self.settings.HTTP_TIMEOUT_SECONDS,
                write=10.0,
                pool=10.0,
            )
            limits = httpx.Limits(
                max_connections=self.settings.HTTP_CONCURRENCY * 2,
                max_keepalive_connections=self.settings.HTTP_CONCURRENCY,
            )
            self._client = httpx.AsyncClient(
                headers=headers,
                timeout=timeout,
                limits=limits,
                follow_redirects=True,
            )
        return self._client

    async def get(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """Fetch a URL with concurrency bounding, delay jitter, and transient retry."""
        client = await self.get_client()

        # Tenacity retry configurator
        retrying = AsyncRetrying(
            retry=retry_if_exception_type((httpx.RequestError, TransientHttpError)),
            stop=stop_after_attempt(4),
            wait=wait_exponential_jitter(initial=1, max=10, jitter=0.5),
            reraise=True,
        )

        async with self.semaphore:
            # Jittered delay for polite request pacing
            if self.settings.REQUEST_DELAY_MS > 0:
                jitter = random.uniform(0.8, 1.2)
                delay_sec = (self.settings.REQUEST_DELAY_MS * jitter) / 1000.0
                await asyncio.sleep(delay_sec)

            async for attempt in retrying:
                with attempt:
                    try:
                        response = await client.get(url, params=params)
                    except (httpx.ConnectError, httpx.TimeoutException) as exc:
                        logger.warning("Transient connection error on %s: %s", url, exc)
                        raise

                    # Check for challenge or forbidden (must NOT retry)
                    if is_challenge_page(response.status_code, response.text):
                        if response.status_code == 403:
                            logger.error("Forbidden / access restricted on %s (HTTP 403)", url)
                            raise ChallengeDetectedError(f"Access restricted (HTTP 403) on {url}")

                    # Transient status codes eligible for retry
                    if response.status_code == 429:
                        retry_after = response.headers.get("Retry-After")
                        wait_time = (
                            float(retry_after) if retry_after and retry_after.isdigit() else 2.0
                        )
                        logger.warning(
                            "Rate limited (429) on %s; sleeping %s seconds", url, wait_time
                        )
                        await asyncio.sleep(wait_time)
                        raise TransientHttpError(f"Rate limited (429) on {url}")

                    if response.status_code in (500, 502, 503, 504):
                        logger.warning("Server error (%s) on %s", response.status_code, url)
                        raise TransientHttpError(f"Server error ({response.status_code}) on {url}")

                    return response

        raise RuntimeError("Unreachable request loop")

    async def close(self) -> None:
        """Close the underlying HTTP client cleanly."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def __aenter__(self) -> "ScraperClient":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()
