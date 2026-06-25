"""Async snapshot fetcher with rate limiting and exponential backoff."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from wayback_crawler.config import FetcherConfig
from wayback_crawler.models import Snapshot
from wayback_crawler.utils import jitter, now_iso

logger = logging.getLogger(__name__)

# Callback signature: async (snapshot, html_or_none) -> None
ResultCallback = Callable[[Snapshot, str | None], Awaitable[Any]]


class RateLimiter:
    """Token-bucket style rate limiter for async requests."""

    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = asyncio.get_event_loop().time()
            wait = self._last + self._min_interval - now
            if wait > 0:
                await asyncio.sleep(wait + jitter(0, 0.3))
            self._last = asyncio.get_event_loop().time()


class SnapshotFetcher:
    """Fetches archived page HTML from the Wayback Machine."""

    def __init__(self, config: FetcherConfig, client: httpx.AsyncClient) -> None:
        self._config = config
        self._client = client
        self._rate_limiter = RateLimiter(config.rate_limit)
        self._semaphore = asyncio.Semaphore(config.concurrency)

    async def fetch_one(self, snapshot: Snapshot) -> tuple[Snapshot, str | None]:
        """Fetch a single snapshot. Returns (updated_snapshot, html_or_none)."""
        for attempt in range(self._config.max_retries + 1):
            try:
                snapshot.fetch_status = "fetching"
                snapshot.updated_at = now_iso()

                await self._rate_limiter.acquire()

                response = await self._client.get(
                    snapshot.wayback_url,
                    headers={
                        "User-Agent": self._config.user_agent,
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "en-US,en;q=0.5",
                    },
                )
                async with self._semaphore:
                    pass  # purely for signaling

                response.raise_for_status()
                html = response.text

                if _is_wayback_error_page(html):
                    snapshot.fetch_status = "skipped"
                    snapshot.last_error = "Wayback Machine error page (not found at archive)"
                    return snapshot, None

                snapshot.fetch_status = "fetched"
                snapshot.retry_count = attempt
                snapshot.updated_at = now_iso()
                return snapshot, html

            except httpx.HTTPStatusError as e:
                status_code = e.response.status_code
                if status_code == 429 and attempt < self._config.max_retries:
                    backoff = min(self._config.backoff_base * (2 ** attempt),
                                  self._config.backoff_max)
                    logger.debug("Rate limited (429) for %s, retrying in %.1fs",
                                 snapshot.wayback_url[:80], backoff)
                    await asyncio.sleep(backoff + jitter(0, 1.0))
                elif 500 <= status_code < 600 and attempt < self._config.max_retries:
                    backoff = min(self._config.backoff_base * (2 ** attempt),
                                  self._config.backoff_max)
                    logger.debug("Server error %d for %s, retrying in %.1fs",
                                 status_code, snapshot.wayback_url[:80], backoff)
                    await asyncio.sleep(backoff + jitter(0, 0.5))
                else:
                    snapshot.fetch_status = "failed"
                    snapshot.last_error = f"HTTP {status_code}: {e}"
                    snapshot.retry_count = attempt
                    snapshot.updated_at = now_iso()
                    return snapshot, None

            except (httpx.RequestError, httpx.TimeoutException) as e:
                if attempt < self._config.max_retries:
                    backoff = min(self._config.backoff_base * (2 ** attempt),
                                  self._config.backoff_max)
                    logger.debug("Request error for %s: %s, retrying in %.1fs",
                                 snapshot.wayback_url[:80], e, backoff)
                    await asyncio.sleep(backoff + jitter(0, 0.5))
                else:
                    snapshot.fetch_status = "failed"
                    snapshot.last_error = str(e)
                    snapshot.retry_count = attempt
                    snapshot.updated_at = now_iso()
                    return snapshot, None

        snapshot.fetch_status = "failed"
        snapshot.last_error = "Max retries exceeded"
        snapshot.updated_at = now_iso()
        return snapshot, None

    async def fetch_many(self, snapshots: list[Snapshot],
                         on_result: ResultCallback | None = None) -> list[tuple[Snapshot, str | None]]:
        """Fetch multiple snapshots with concurrency control.

        Args:
            snapshots: List of snapshots to fetch.
            on_result: Optional async callback invoked for each completed fetch.
                       Called as ``await on_result(snapshot, html_or_none)``.

        Returns:
            List of (snapshot, html_or_none) tuples in completion order.
        """
        sem = asyncio.Semaphore(self._config.concurrency)
        results: list[tuple[Snapshot, str | None]] = []

        async def _fetch_one(snap: Snapshot) -> None:
            async with sem:
                result = await self.fetch_one(snap)
                results.append(result)
                if on_result:
                    try:
                        await on_result(result[0], result[1])
                    except Exception as exc:
                        logger.error("on_result callback failed for %s: %s",
                                     snap.wayback_url[:80], exc)

        tasks = [asyncio.create_task(_fetch_one(s)) for s in snapshots]
        await asyncio.gather(*tasks, return_exceptions=True)
        return results


def _is_wayback_error_page(html: str) -> bool:
    """Check if the Wayback Machine returned an error/not-found page."""
    lower = html.lower()
    if "<title>wayback machine</title>" in lower and "not found" in lower:
        return True
    if "the wayback machine has not archived that url" in lower:
        return True
    if "wayback machine doesn't have that page archived" in lower:
        return True
    return False
