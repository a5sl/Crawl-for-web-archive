"""Parser for Twitter API JSON responses archived by the Wayback Machine.

Modern Twitter/X is a SPA that loads data via API calls. The Wayback Machine
often captures these API responses directly (Content-Type: application/json)
instead of rendered HTML. This parser handles those JSON payloads.
"""

from __future__ import annotations

import json as json_mod
import logging
import re
from typing import Any

from wayback_crawler.models import Media, ParseResult, Tweet
from wayback_crawler.parsers.base import BaseParser

logger = logging.getLogger(__name__)

# These are the text markers we check to distinguish a JSON API response
# from a generic JSON page (e.g. error pages, config blobs).
TWITTER_API_MARKERS = [
    "created_at",
    "conversation_id",
    "public_metrics",
    "author_id",
    "entities",
    "full_text",         # Twitter v1.1 format
    "favorite_count",    # Twitter v1.1 format
    "extended_entities", # Twitter v1.1 format (media)
]


class TwitterJsonParser(BaseParser):
    """Parses Twitter API JSON responses archived by the Wayback Machine.

    Handles:
    - Twitter v2 API (Tweet detail / timeline endpoints)
    - Twitter v1.1 API (legacy ``tweet`` / ``legacy`` nesting)
    - Single-tweet and multi-tweet payloads
    """

    @property
    def domain(self) -> str:
        return "twitter.com"

    def can_parse(self, html: str, url: str) -> bool:
        """Return True if the content looks like a Twitter API JSON response."""
        stripped = html.strip()
        if not stripped.startswith("{"):
            return False

        try:
            data = json_mod.loads(stripped)
        except (json_mod.JSONDecodeError, ValueError):
            return False

        if not isinstance(data, dict):
            return False

        # Check for standard tweet fields at any nesting level
        all_keys = self._collect_keys(data)
        marker_hits = sum(1 for m in TWITTER_API_MARKERS if m in all_keys)
        return marker_hits >= 2

    def parse(self, html: str, url: str, snapshot_timestamp: str) -> ParseResult:
        result = ParseResult()
        stripped = html.strip()

        try:
            data = json_mod.loads(stripped)
        except (json_mod.JSONDecodeError, ValueError) as exc:
            result.errors.append(f"JSON parse error: {exc}")
            return result

        if not isinstance(data, dict):
            result.errors.append("JSON root is not an object")
            return result

        # Extract users lookup (used to resolve author_id -> handle/name)
        users_lookup = self._build_users_lookup(data)

        # Try to find tweet data in various locations
        tweet_entries = self._find_tweets(data)

        for entry in tweet_entries:
            tweet = self._parse_tweet_entry(entry, users_lookup, snapshot_timestamp)
            if tweet:
                result.tweets.append(tweet)

                # Extract media
                media_items = self._extract_media(entry, data)
                result.media.extend(media_items)

        if not result.tweets:
            result.errors.append("No tweet data found in JSON")

        return result

    # ── Internal helpers ──────────────────────────────────────────────

    def _collect_keys(self, obj: Any, depth: int = 0) -> set[str]:
        """Recursively collect all dict keys (up to depth 4)."""
        keys: set[str] = set()
        if depth > 4 or not isinstance(obj, dict):
            return keys
        for k, v in obj.items():
            keys.add(k)
            if isinstance(v, dict):
                keys.update(self._collect_keys(v, depth + 1))
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        keys.update(self._collect_keys(item, depth + 1))
        return keys

    def _build_users_lookup(self, data: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Build a map of user_id -> user_data from includes/users."""
        lookup: dict[str, dict[str, Any]] = {}

        includes = data.get("includes", {})
        users = includes.get("users", [])
        if isinstance(users, list):
            for u in users:
                if isinstance(u, dict):
                    uid = u.get("id") or u.get("id_str")
                    if uid:
                        lookup[str(uid)] = u

        # Also check globalObjects (v1.1 format)
        go = data.get("globalObjects", {})
        go_users = go.get("users", {})
        if isinstance(go_users, dict):
            for uid, udata in go_users.items():
                if isinstance(udata, dict):
                    lookup[str(uid)] = udata

        return lookup

    def _find_tweets(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        """Locate tweet objects in various JSON structures."""
        tweets: list[dict[str, Any]] = []

        # Twitter v2: data is the tweet itself
        tweet_data = data.get("data")
        if isinstance(tweet_data, dict):
            # Check if it looks like a tweet object
            if "text" in tweet_data or "legacy" in tweet_data:
                tweets.append(tweet_data)
            elif "tweet" in tweet_data:
                tweets.append(tweet_data["tweet"])

        # Twitter v1.1: globalObjects.tweets
        go = data.get("globalObjects", {})
        go_tweets = go.get("tweets", {})
        if isinstance(go_tweets, dict):
            for tid, tdata in go_tweets.items():
                if isinstance(tdata, dict):
                    tdata["__tweet_id__"] = str(tid)
                    tweets.append(tdata)

        # Threaded conversation v2
        conv = data.get("threaded_conversation_with_injections_v2", {})
        instructions = conv.get("instructions", [])
        for instr in instructions:
            if isinstance(instr, dict):
                entries = instr.get("entries") or []
                for entry in entries:
                    if isinstance(entry, dict):
                        content = entry.get("content", {})
                        for key in ("tweet", "item"):
                            inner = content.get(key, {})
                            result = inner.get("tweet_results", {})
                            tweet_obj = result.get("result", result)
                            if isinstance(tweet_obj, dict):
                                if "legacy" in tweet_obj or "text" in tweet_obj:
                                    tweets.append(tweet_obj)

        return tweets

    def _parse_tweet_entry(self, entry: dict[str, Any],
                           users_lookup: dict[str, dict[str, Any]],
                           snapshot_timestamp: str) -> Tweet | None:
        """Parse a single tweet dict into a Tweet model."""
        # Try v2 top-level format first, then legacy nesting
        tweet_id = (
            entry.get("id")
            or entry.get("id_str")
            or entry.get("__tweet_id__")
            or entry.get("rest_id")
        )
        if not tweet_id:
            return None
        tweet_id = str(tweet_id)

        legacy = entry.get("legacy", entry)
        if not isinstance(legacy, dict):
            legacy = {}

        text = entry.get("text") or legacy.get("full_text") or ""
        created_at = entry.get("created_at") or legacy.get("created_at")

        # Author lookup
        author_id = str(entry.get("author_id") or legacy.get("user_id_str") or "")
        author_handle = None
        author_display = None

        if author_id and author_id in users_lookup:
            u = users_lookup[author_id]
            author_handle = u.get("screen_name") or u.get("username") or ""
            author_display = u.get("name") or ""
        else:
            # Try core.user_results (v2 nested format)
            core = entry.get("core", {})
            user_results = core.get("user_results", {})
            user_result = user_results.get("result", user_results)
            user_legacy = user_result.get("legacy", user_result)
            if isinstance(user_legacy, dict):
                author_handle = user_legacy.get("screen_name") or ""
                author_display = user_legacy.get("name") or ""

        if author_handle and not author_handle.startswith("@"):
            author_handle = f"@{author_handle}"

        # Metrics
        metrics = entry.get("public_metrics") or legacy
        if not isinstance(metrics, dict):
            metrics = {}

        is_retweet = "retweeted_status_result" in legacy or "retweeted_status_id_str" in legacy
        is_reply = bool(legacy.get("in_reply_to_status_id_str"))

        return Tweet(
            snapshot_id=0,
            tweet_id=tweet_id,
            author_handle=author_handle or "@unknown",
            author_display=author_display or "",
            text=text,
            timestamp=created_at,
            reply_count=metrics.get("reply_count"),
            retweet_count=metrics.get("retweet_count"),
            like_count=metrics.get("favorite_count") or metrics.get("like_count"),
            quote_count=metrics.get("quote_count"),
            is_retweet=is_retweet,
            is_reply=is_reply,
            parsed_with="twitter_json",
        )

    def _extract_media(self, entry: dict[str, Any],
                       data: dict[str, Any]) -> list[Media]:
        """Extract media items from tweet entry and includes."""
        media_items: list[Media] = []

        # v2: includes.media keyed by media_key
        includes = data.get("includes", {})
        api_media = includes.get("media", [])

        # Find media keys referenced by this tweet
        attachments = entry.get("attachments", {})
        media_keys = set(attachments.get("media_keys", []))

        # Also from entities.urls
        entities = entry.get("entities", {})
        urls = entities.get("urls", entities.get("urls", []))
        for u in urls:
            if isinstance(u, dict) and u.get("media_key"):
                media_keys.add(u["media_key"])

        # Match media keys to actual media objects
        for m in api_media:
            if not isinstance(m, dict):
                continue
            m_key = m.get("media_key", "")
            if media_keys and m_key not in media_keys:
                continue
            m_type = m.get("type", "photo")

            # Video media: URLs are inside variants array
            if m_type in ("video", "animated_gif"):
                variants = m.get("variants", [])
                # Sort by bit_rate descending so highest quality downloads first
                mp4_variants = [
                    v for v in variants
                    if isinstance(v, dict) and v.get("url") and "mpegURL" not in v.get("content_type", "")
                ]
                mp4_variants.sort(key=lambda v: v.get("bit_rate", 0), reverse=True)
                for v in mp4_variants:
                    media_items.append(Media(
                        tweet_id=0,
                        url=v["url"],
                        media_type="video",
                    ))
                # Also add preview image
                preview = m.get("preview_image_url")
                if preview:
                    media_items.append(Media(
                        tweet_id=0,
                        url=preview,
                        media_type="image",
                    ))
            else:
                media_url = m.get("url") or m.get("media_url_https")
                if media_url:
                    media_items.append(Media(
                        tweet_id=0,
                        url=media_url,
                        media_type="image" if m_type in ("photo", "image") else "image",
                    ))

        # v1.1: extended_entities.media
        ext_entities = entry.get("extended_entities", {})
        legacy = entry.get("legacy", {})
        if not ext_entities and isinstance(legacy, dict):
            ext_entities = legacy.get("extended_entities", {})
        legacy_media = ext_entities.get("media", [])
        for m in legacy_media:
            if not isinstance(m, dict):
                continue
            m_type = m.get("type", "photo")

            if m_type in ("video", "animated_gif"):
                video_info = m.get("video_info", {})
                variants = video_info.get("variants", [])
                mp4_variants = [
                    v for v in variants
                    if isinstance(v, dict) and v.get("url") and "mpegURL" not in v.get("content_type", "")
                ]
                mp4_variants.sort(key=lambda v: v.get("bitrate", 0), reverse=True)
                for v in mp4_variants:
                    media_items.append(Media(
                        tweet_id=0,
                        url=v["url"],
                        media_type="video",
                    ))
            else:
                media_url = m.get("media_url_https") or m.get("media_url") or ""
                if media_url:
                    media_items.append(Media(
                        tweet_id=0,
                        url=media_url,
                        media_type="image",
                    ))

        return media_items
