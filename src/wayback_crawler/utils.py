"""Utility functions for the Wayback crawler."""

from __future__ import annotations

import logging
import random
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


def setup_logging(level: str = "INFO", log_file: str | None = None,
                  fmt: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s") -> None:
    """Configure stdout and optional file logging."""
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=fmt,
        handlers=handlers,
    )


def extract_domain(url: str) -> str:
    """Return the normalized domain from a URL (lowercase, no www prefix)."""
    hostname = urlparse(url).hostname or url
    hostname = hostname.lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return hostname


def normalize_domain(url: str) -> str:
    """Normalize a URL's domain for parser lookup (strip mobile. prefix too)."""
    domain = extract_domain(url)
    domain = re.sub(r"^(m\.|mobile\.)", "", domain)
    return domain


def compute_wayback_url(timestamp: str, original_url: str) -> str:
    """Build the full Wayback Machine playback URL for a snapshot.

    Uses ``id_`` (identity) flag to get the raw archived bytes — for
    Twitter this is usually a JSON API response containing tweet data
    and original media URLs.  Media is then downloaded separately
    through the ``im_`` (image) flag.
    """
    return f"https://web.archive.org/web/{timestamp}id_/{original_url}"


_WAYBACK_RE = re.compile(
    r"https?://web\.archive\.org/web/\d+/?[a-zA-Z_]*/?(https?://.+)"
)


def extract_original_url(url: str) -> str:
    """If *url* is a Wayback Machine playback URL, return the original URL.

    Handles ``/web/<timestamp>/<url>``, ``/web/<timestamp>im_/<url>``,
    ``/web/<timestamp>if_/<url>``, and ``/web/<timestamp>id_/<url>``.
    Returns *url* unchanged if it isn't a Wayback URL.
    """
    m = _WAYBACK_RE.match(url)
    return m.group(1) if m else url


def jitter(base: float, factor: float = 0.5) -> float:
    """Return a random value in [base, base * (1 + factor)]."""
    return base + random.random() * base * factor


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_twitter_status_id(url: str) -> str | None:
    """Extract the Twitter status ID from a tweet URL."""
    m = re.search(r"/status(?:es)?/(\d+)", url)
    return m.group(1) if m else None


def extract_handle(url_pattern: str) -> str:
    """Extract the Twitter handle from a CDX URL pattern.

    ``twitter.com/youzaimeimei/*`` → ``youzaimeimei``
    """
    m = re.search(r"twitter\.com/([^/*]+)", url_pattern)
    return m.group(1) if m else "unknown"


def media_output_dir(base_dir: str | Path, url_pattern: str) -> Path:
    """Return the media output directory for a crawl target.

    ``./media`` + ``twitter.com/youzaimeimei/*`` → ``./media/youzaimeimei/``
    """
    return Path(base_dir) / extract_handle(url_pattern)
