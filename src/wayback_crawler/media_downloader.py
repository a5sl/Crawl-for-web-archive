"""Media downloader — downloads archived media from the Wayback Machine.

All downloads go through web.archive.org. The URL format for archived media is:
``https://web.archive.org/web/{timestamp}im_/{original_url}``

The ``im_`` flag tells Wayback Machine to return the raw binary without
toolbar injection, which is required for image/video downloads.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


async def download_media(
    client: httpx.AsyncClient,
    media_url: str,
    snapshot_timestamp: str,
    output_dir: str | Path,
    user_agent: str,
    timeout: float = 30.0,
    max_retries: int = 2,
    wayback_url: str | None = None,
) -> str | None:
    """Download a single media file from the Wayback Machine.

    Args:
        client: Shared httpx async client.
        media_url: Original media URL (e.g. https://pbs.twimg.com/media/xxx.jpg).
        snapshot_timestamp: Wayback timestamp from the parent snapshot.
        output_dir: Local directory to save downloaded files.
        user_agent: User-Agent header.
        timeout: Request timeout in seconds.
        max_retries: Maximum retry count.
        wayback_url: If provided, download from this full Wayback URL directly
            instead of constructing one from the timestamp and media_url.

    Returns:
        Local file path on success, None on failure.
    """
    url = wayback_url
    if not url:
        if "web.archive.org" in media_url:
            # Already a Wayback URL — insert im_ after the timestamp
            url = re.sub(
                r"(https?://web\.archive\.org/web/\d+)/",
                r"\1im_/", media_url, count=1,
            )
        else:
            url = f"https://web.archive.org/web/{snapshot_timestamp}im_/{media_url}"
    ext = _guess_extension(media_url)
    filename = f"{snapshot_timestamp}_{_safe_filename(media_url)}{ext}"

    output_path = Path(output_dir) / filename
    if output_path.exists():
        logger.debug("Media already downloaded: %s", filename)
        return str(output_path)

    for attempt in range(max_retries + 1):
        try:
            response = await client.get(
                url,
                headers={"User-Agent": user_agent},
            )
            response.raise_for_status()

            # Verify we got binary content, not an error page
            content_type = response.headers.get("content-type", "")
            if "text/html" in content_type and len(response.content) < 1000:
                logger.debug("Wayback returned HTML instead of media for %s", media_url[:80])
                if attempt < max_retries:
                    continue

            os.makedirs(output_dir, exist_ok=True)
            output_path.write_bytes(response.content)
            logger.info("Downloaded: %s → %s", filename, output_path)
            return str(output_path)

        except httpx.HTTPStatusError as e:
            logger.debug("HTTP %d for media %s (attempt %d/%d)",
                         e.response.status_code, media_url[:80],
                         attempt + 1, max_retries + 1)
        except (httpx.RequestError, httpx.TimeoutException, OSError) as e:
            logger.debug("Download failed for media %s (attempt %d/%d): %s",
                         media_url[:80], attempt + 1, max_retries + 1, e)

    logger.warning("Failed to download media after %d attempts: %s",
                   max_retries + 1, media_url[:80])
    return None


def _guess_extension(url: str) -> str:
    """Extract file extension from URL, defaulting to .jpg."""
    path = url.split("?")[0]
    _, ext = os.path.splitext(path)
    valid = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".mov"}
    return ext.lower() if ext.lower() in valid else ".jpg"


def _safe_filename(url: str) -> str:
    """Create a safe filename from a media URL."""
    safe = url.split("?")[0].split("/")[-1]
    if not safe or len(safe) > 100:
        safe = url.rsplit("/", 1)[-1].split("?")[0][:80]
    if not safe:
        safe = "media"
    # Remove characters unsafe for filenames
    safe = "".join(c for c in safe if c.isalnum() or c in "._-")
    return safe or "media"
