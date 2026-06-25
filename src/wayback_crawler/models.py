"""Pure dataclass models for the Wayback crawler."""

from dataclasses import dataclass, field
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CrawlRun:
    """Metadata about a single crawl session."""

    url_pattern: str
    config_snapshot: str  # JSON dump of the Config used
    id: int | None = None
    started_at: str = field(default_factory=_now_iso)
    finished_at: str | None = None
    total_snapshots: int = 0
    fetched_snapshots: int = 0
    failed_snapshots: int = 0
    tweets_extracted: int = 0


@dataclass
class Snapshot:
    """A single CDX record — an available Wayback Machine snapshot."""

    crawl_run_id: int
    timestamp: str         # "20230615120000"
    original_url: str
    wayback_url: str
    id: int | None = None
    fetch_status: str = "pending"
    retry_count: int = 0
    last_error: str | None = None
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)


@dataclass
class Tweet:
    """Structured tweet data extracted from a snapshot page."""

    snapshot_id: int
    tweet_id: str           # Twitter's numeric status ID
    author_handle: str      # "@username"
    text: str               # full tweet text
    parsed_with: str        # "twitter_old" | "twitter_new"
    id: int | None = None
    author_display: str | None = None
    timestamp: str | None = None   # ISO 8601 of tweet post time
    reply_count: int | None = None
    retweet_count: int | None = None
    like_count: int | None = None
    quote_count: int | None = None
    is_retweet: bool = False
    is_reply: bool = False
    raw_metadata: str | None = None  # JSON blob for unexpected fields
    created_at: str = field(default_factory=_now_iso)


@dataclass
class Media:
    """Media items referenced in a tweet."""

    tweet_id: int
    url: str
    media_type: str         # "image", "video", "gif"
    id: int | None = None
    wayback_url: str | None = None
    downloaded: bool = False
    local_path: str | None = None  # local file path if downloaded
    created_at: str = field(default_factory=_now_iso)


@dataclass
class ParseResult:
    """Output from a parser for a single snapshot."""

    tweets: list[Tweet] = field(default_factory=list)
    media: list[Media] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
