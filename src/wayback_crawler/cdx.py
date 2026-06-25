"""CDX API client for querying Wayback Machine snapshot indexes."""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator
from urllib.parse import urlencode

import httpx

from wayback_crawler.config import CdxConfig
from wayback_crawler.utils import jitter

logger = logging.getLogger(__name__)

CDX_HEADER_FIELDS = ["timestamp", "original"]


class CdxClient:
    """Async client for the Wayback Machine CDX API."""

    def __init__(self, config: CdxConfig, client: httpx.AsyncClient) -> None:
        self._config = config
        self._client = client
        self._last_request = 0.0

    async def _rate_limit(self) -> None:
        """Sleep until the configured rate limit has elapsed since last request."""
        now = asyncio.get_event_loop().time()
        delay = self._last_request + self._config.rate_limit - now
        if delay > 0:
            await asyncio.sleep(delay + jitter(0, 0.3))
        self._last_request = asyncio.get_event_loop().time()

    def _build_url(self, url_pattern: str, *, from_date: str | None = None,
                   to_date: str | None = None, limit: int | None = None) -> str:
        """Construct the full CDX API URL with all parameters.

        Omits ``matchType`` when the url_pattern contains a wildcard (``*``),
        because the wildcard syntax is incompatible with matchType=prefix.
        """
        params: dict[str, str | int] = {
            "url": url_pattern,
            "output": "json",
            "fl": "timestamp,original",
            "collapse": self._config.collapse,
        }
        # matchType=prefix treats the URL as a literal prefix, which
        # conflicts with the ``*`` wildcard. Only include matchType
        # when the user did NOT provide a wildcard pattern.
        if "*" not in url_pattern:
            params["matchType"] = self._config.match_type

        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        if limit is not None:
            params["limit"] = str(limit)

        # Use doseq=True and safe='*' so the wildcard survives percent-encoding
        query_string = urlencode(params, doseq=True, safe="*")
        return f"{self._config.base_url}?{query_string}"

    async def query(self, url_pattern: str, *, from_date: str | None = None,
                    to_date: str | None = None, limit: int | None = None,
                    max_retries: int = 3) -> list[dict[str, str]]:
        """Query the CDX API and return all results as a list.

        Each result is a dict with keys: 'timestamp', 'original'.
        """
        results: list[dict[str, str]] = []
        async for entry in self.query_iter(
            url_pattern, from_date=from_date, to_date=to_date,
            limit=limit, max_retries=max_retries,
        ):
            results.append(entry)
        return results

    async def query_iter(self, url_pattern: str, *, from_date: str | None = None,
                         to_date: str | None = None, limit: int | None = None,
                         max_retries: int = 3) -> AsyncIterator[dict[str, str]]:
        """Query the CDX API, yielding results one at a time as they arrive."""
        url = self._build_url(url_pattern, from_date=from_date,
                              to_date=to_date, limit=limit)
        logger.info("CDX query: %s", url)

        for attempt in range(max_retries + 1):
            try:
                await self._rate_limit()
                response = await self._client.get(url)
                response.raise_for_status()
                data = response.json()

                if not data or len(data) < 2:
                    logger.warning("CDX returned no results for %s", url_pattern)
                    return

                # First row is the header, subsequent rows are data
                for row in data[1:]:
                    # CDX may return empty strings for missing fields
                    if len(row) >= 2 and row[0] and row[1]:
                        yield {"timestamp": row[0], "original": row[1]}

                return  # success

            except httpx.HTTPStatusError as e:
                if e.response.status_code >= 500 and attempt < max_retries:
                    backoff = min(self._config.rate_limit * (2 ** attempt), 60)
                    logger.warning("CDX server error (attempt %d/%d), retrying in %.1fs",
                                   attempt + 1, max_retries + 1, backoff)
                    await asyncio.sleep(backoff + jitter(0, 0.5))
                else:
                    logger.error("CDX HTTP error: %s", e)
                    raise

            except (httpx.RequestError, httpx.TimeoutException) as e:
                if attempt < max_retries:
                    backoff = min(2.0 * (2 ** attempt), 30)
                    logger.warning("CDX request failed (attempt %d/%d): %s, retrying in %.1fs",
                                   attempt + 1, max_retries + 1, e, backoff)
                    await asyncio.sleep(backoff + jitter(0, 0.5))
                else:
                    logger.error("CDX request failed after %d retries: %s", max_retries, e)
                    raise
