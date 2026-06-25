"""Parser for post-2017 modern Twitter/X archived pages."""

from __future__ import annotations

import json as json_mod
import logging
import re
from datetime import datetime, timezone
from typing import Any

from bs4 import BeautifulSoup, Tag

from wayback_crawler.models import Media, ParseResult, Tweet
from wayback_crawler.parsers.base import BaseParser
from wayback_crawler.utils import parse_twitter_status_id

logger = logging.getLogger(__name__)

DELETED_PATTERNS = [
    "this tweet is unavailable",
    "this tweet is not available",
    "this tweet has been deleted",
    "hmm...this page doesn't exist",
]

# Only reject content that is definitively a login page — a login FORM,
# not just footer text mentioning sign-up. Modern public tweet pages
# routinely reference "Sign up" in navigation without being login walls.
LOGIN_FORM_MARKERS = [
    'action="/login"',
    '<form action="https://twitter.com/login',
    "<title>Login on X</title>",
    "<title>Log in to Twitter</title>",
    "<title>Login to X</title>",
]

OUTER_CONTAINER_SEL = "article[data-testid='tweet']"
TEXT_SEL = "div[data-testid='tweetText']"
USER_SEL = "div[data-testid='User-Name']"
PHOTO_SEL = "div[data-testid='tweetPhoto'] img"
VIDEO_SEL = "div[data-testid='tweetPhoto'] video"
REPLY_BUTTON_SEL = "button[data-testid='reply']"
RETWEET_BUTTON_SEL = "button[data-testid='retweet'], button[data-testid='unretweet']"
LIKE_BUTTON_SEL = "button[data-testid='like'], button[data-testid='unlike']"


class TwitterNewParser(BaseParser):
    """Parser for modern Twitter/X React-rendered HTML (2017–present).

    Uses a layered approach:
    1. Attempt to extract from ``window.__INITIAL_STATE__`` JSON.
    2. Fall back to HTML scraping with ``data-testid`` selectors.
    """

    def __init__(self, prefer_json: bool = True) -> None:
        self._prefer_json = prefer_json

    @property
    def domain(self) -> str:
        return "twitter.com"

    def can_parse(self, html: str, url: str) -> bool:
        lower = html.lower()
        if any(p in lower for p in DELETED_PATTERNS):
            return True
        if any(m.lower() in html.lower() for m in LOGIN_FORM_MARKERS):
            return False
        has_react = '<div id="react-root">' in lower
        has_article = 'data-testid="tweet"' in lower
        has_cell = 'data-testid="cellInnerDiv"' in lower
        return has_react or has_article or has_cell

    def parse(self, html: str, url: str, snapshot_timestamp: str) -> ParseResult:
        result = ParseResult()

        lower = html.lower()
        if any(p in lower for p in DELETED_PATTERNS):
            result.errors.append("Tweet deleted or unavailable")
            return result
        if any(m.lower() in html.lower() for m in LOGIN_FORM_MARKERS):
            result.errors.append("Login page — no tweet content available")
            return result

        # Try JSON extraction first
        if self._prefer_json:
            json_result = self._parse_from_initial_state(html, snapshot_timestamp)
            if json_result.tweets:
                return json_result
            if json_result.errors:
                result.errors.extend(json_result.errors)
            if json_result.media:
                result.media.extend(json_result.media)

        # Fall back to HTML scraping
        html_result = self._parse_from_html(html, url, snapshot_timestamp)
        result.tweets.extend(html_result.tweets)
        result.media.extend(html_result.media)
        result.errors.extend(html_result.errors)
        return result

    # ── JSON extraction ───────────────────────────────────────────────

    def _parse_from_initial_state(self, html: str,
                                  snapshot_timestamp: str) -> ParseResult:
        """Attempt to extract tweets from ``__INITIAL_STATE__`` JSON."""
        result = ParseResult()
        json_data = self._extract_initial_state(html)
        if not json_data:
            result.errors.append("No __INITIAL_STATE__ found")
            return result

        try:
            # The exact path varies; try common locations
            entries = self._find_timeline_entries(json_data)
            if not entries:
                result.errors.append("No timeline entries in __INITIAL_STATE__")
                return result

            for entry in entries:
                tweet = self._parse_initial_state_entry(entry, snapshot_timestamp)
                if tweet:
                    result.tweets.append(tweet)
                    media_items = self._extract_media_from_initial_state(entry)
                    result.media.extend(media_items)
        except Exception as exc:
            logger.debug("Error parsing __INITIAL_STATE__: %s", exc)
            result.errors.append(f"JSON parse error: {exc}")

        return result

    def _extract_initial_state(self, html: str) -> dict[str, Any] | None:
        """Find and parse the __INITIAL_STATE__ JSON blob."""
        # Match both ``window.__INITIAL_STATE__ = {...};`` and ``__INITIAL_STATE__={...}``
        match = re.search(
            r"__INITIAL_STATE__\s*=\s*(\{.+?\});\s*(?:</script>|window\.|document\.|</?script)",
            html, re.DOTALL,
        )
        if not match:
            match = re.search(
                r'__INITIAL_STATE__\s*=\s*(\{.+?\});',
                html, re.DOTALL,
            )
        if not match:
            return None

        json_str = match.group(1)
        # Fix common encoding issues
        json_str = json_str.replace(r"\"", "\"")
        try:
            return json_mod.loads(json_str)
        except json_mod.JSONDecodeError:
            # Try extracting a smaller valid JSON segment
            try:
                # Sometimes there's extra content after the JSON object
                depth = 0
                end = 0
                for i, ch in enumerate(json_str):
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            end = i + 1
                            break
                if end > 0:
                    return json_mod.loads(json_str[:end])
            except json_mod.JSONDecodeError:
                pass
            return None

    def _find_timeline_entries(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        """Navigate the __INITIAL_STATE__ tree to find tweet entries."""
        entries: list[dict[str, Any]] = []

        # Try direct timeline path
        for key in ("homeTimeline", "profileTimeline", "timeline"):
            timeline = _deep_get(data, key)
            if timeline:
                instructions = _deep_get(timeline, "instructions") or []
                for instr in instructions:
                    if isinstance(instr, dict):
                        instr_entries = instr.get("entries") or []
                        entries.extend(instr_entries)

        # Try entities path
        if not entries:
            entities = _deep_get(data, "entities", "tweets")
            if isinstance(entities, dict):
                for tweet_id, tweet_data in entities.items():
                    if isinstance(tweet_data, dict):
                        tweet_data["__tweet_id"] = tweet_id
                        entries.append(tweet_data)

        # Try tweetResult
        if not entries:
            tweet_result = _deep_get(data, "tweetResult")
            if isinstance(tweet_result, dict):
                entries.append(tweet_result)

        # Try threaded_conversation
        if not entries:
            conv = _deep_get(data, "threaded_conversation_with_injections_v2")
            if isinstance(conv, dict):
                conv_instructions = _deep_get(conv, "instructions") or []
                for instr in conv_instructions:
                    if isinstance(instr, dict):
                        instr_entries = instr.get("entries") or []
                        entries.extend(instr_entries)

        return entries

    def _parse_initial_state_entry(self, entry: dict[str, Any],
                                   snapshot_timestamp: str) -> Tweet | None:
        """Parse a single tweet entry from __INITIAL_STATE__."""
        content = entry.get("content", entry)

        # Drill down to the tweet data
        tweet_data = content.get("tweet", content)
        if isinstance(tweet_data, dict):
            inner = tweet_data.get("tweet", tweet_data)
            inner = inner.get("tweet", inner)  # up to 2 levels of wrapping
            tweet_data = inner if isinstance(inner, dict) else tweet_data

        legacy = tweet_data.get("legacy", tweet_data)
        if not isinstance(legacy, dict):
            return None

        # Core data lives under "core"
        core = tweet_data.get("core", {})
        user_results = core.get("user_results", core)
        user_legacy = user_results.get("result", user_results)
        user_legacy = user_legacy.get("legacy", user_legacy)

        tweet_id = tweet_data.get("__tweet_id") or legacy.get("id_str")
        if not tweet_id:
            tweet_id = str(entry.get("entryId", "").replace("tweet-", ""))

        if not tweet_id:
            return None

        author_handle = user_legacy.get("screen_name", "")
        if author_handle and not author_handle.startswith("@"):
            author_handle = f"@{author_handle}"

        text = legacy.get("full_text") or ""
        created_at = legacy.get("created_at")
        if created_at:
            try:
                created_at = _parse_twitter_date(created_at)
            except ValueError:
                created_at = None

        is_retweet = "retweeted_status_result" in legacy
        is_reply = bool(legacy.get("in_reply_to_status_id_str"))

        return Tweet(
            snapshot_id=0,
            tweet_id=tweet_id,
            author_handle=author_handle or "@unknown",
            author_display=user_legacy.get("name"),
            text=text,
            timestamp=created_at,
            reply_count=legacy.get("reply_count"),
            retweet_count=legacy.get("retweet_count"),
            like_count=legacy.get("favorite_count"),
            quote_count=legacy.get("quote_count"),
            is_retweet=is_retweet,
            is_reply=is_reply,
            parsed_with="twitter_new",
        )

    def _extract_media_from_initial_state(self, entry: dict[str, Any]) -> list[Media]:
        """Extract media items from an __INITIAL_STATE__ tweet entry."""
        media_items: list[Media] = []

        content = entry.get("content", entry)
        tweet_data = content.get("tweet", content)
        if isinstance(tweet_data, dict):
            tweet_data = tweet_data.get("tweet", tweet_data)
            tweet_data = tweet_data.get("tweet", tweet_data)
            tweet_data = tweet_data if isinstance(tweet_data, dict) else content

        legacy = tweet_data.get("legacy", tweet_data)
        if not isinstance(legacy, dict):
            return media_items

        ext_entities = legacy.get("extended_entities", {})
        for m in ext_entities.get("media", []):
            if not isinstance(m, dict):
                continue
            media_url = m.get("media_url_https") or m.get("media_url") or ""
            if not media_url:
                continue
            m_type = m.get("type", "photo")
            if m_type in ("photo", "image"):
                media_type = "image"
            elif m_type in ("video", "animated_gif"):
                media_type = "video"
            else:
                media_type = "image"
            media_items.append(Media(tweet_id=0, url=media_url, media_type=media_type))

        return media_items

    # ── HTML scraping ─────────────────────────────────────────────────

    def _parse_from_html(self, html: str, url: str,
                         snapshot_timestamp: str) -> ParseResult:
        result = ParseResult()
        soup = BeautifulSoup(html, "lxml")

        # Strip Wayback toolbar and script/style
        for tag in soup.find_all(["script", "style"]):
            tag.decompose()
        wm_toolbar = soup.find("div", id="wm-ipp-base")
        if wm_toolbar:
            wm_toolbar.decompose()

        containers = soup.select(OUTER_CONTAINER_SEL)
        if not containers:
            containers = soup.find_all("article")

        for container in containers:
            tweet = self._extract_tweet_html(container, url, snapshot_timestamp)
            if tweet:
                result.tweets.append(tweet)
                media_items = self._extract_media_from_html(container)
                result.media.extend(media_items)

        return result

    def _extract_tweet_html(self, container: Tag, url: str,
                            snapshot_timestamp: str) -> Tweet | None:
        tweet_id = self._extract_tweet_id_html(container, url)
        if not tweet_id:
            return None

        author_handle = self._extract_author_html(container)
        author_display = self._extract_display_html(container)
        text = self._extract_text_html(container)
        timestamp = self._extract_timestamp_html(container)
        is_retweet = self._is_retweet_html(container)
        is_reply = self._is_reply_html(container)
        reply_count = self._extract_aria_count(container, REPLY_BUTTON_SEL)
        retweet_count = self._extract_aria_count(container, RETWEET_BUTTON_SEL)
        like_count = self._extract_aria_count(container, LIKE_BUTTON_SEL)

        if not text and not author_handle:
            return None

        return Tweet(
            snapshot_id=0,
            tweet_id=tweet_id,
            author_handle=author_handle or "@unknown",
            author_display=author_display,
            text=text or "",
            timestamp=timestamp,
            reply_count=reply_count,
            retweet_count=retweet_count,
            like_count=like_count,
            is_retweet=is_retweet,
            is_reply=is_reply,
            parsed_with="twitter_new",
        )

    def _extract_tweet_id_html(self, container: Tag, url: str) -> str | None:
        status_link = container.select_one("a[href*='/status/']")
        if status_link:
            tid = parse_twitter_status_id(status_link.get("href", ""))
            if tid:
                return tid
        return parse_twitter_status_id(url)

    def _extract_author_html(self, container: Tag) -> str | None:
        user_el = container.select_one(USER_SEL)
        if user_el:
            text = user_el.get_text(strip=True)
            m = re.search(r"@(\w+)", text)
            if m:
                return f"@{m.group(1)}"
        # Fallback: look for author link
        link = container.select_one("a[href^='/']")
        if link:
            href = link.get("href", "")
            m = re.match(r"/(\w+)(?:/|$)", href)
            if m and m.group(1) not in ("hashtag", "search", "home", "explore",
                                         "notifications", "messages", "i"):
                return f"@{m.group(1)}"
        return None

    def _extract_display_html(self, container: Tag) -> str | None:
        user_el = container.select_one(USER_SEL)
        if user_el:
            spans = user_el.find_all("span")
            for span in spans:
                text = span.get_text(strip=True)
                if text and not text.startswith("@"):
                    return text
        return None

    def _extract_text_html(self, container: Tag) -> str | None:
        text_el = container.select_one(TEXT_SEL)
        if text_el:
            return text_el.get_text(separator="\n", strip=True)
        # Fallback: any div with lang attribute
        lang_el = container.select_one("div[lang]")
        if lang_el:
            return lang_el.get_text(separator="\n", strip=True)
        return None

    def _extract_timestamp_html(self, container: Tag) -> str | None:
        time_el = container.select_one("time")
        if time_el and time_el.get("datetime"):
            return time_el["datetime"]
        return None

    def _is_retweet_html(self, container: Tag) -> bool:
        text = container.get_text().lower()
        return "reposted" in text or "retweeted" in text

    def _is_reply_html(self, container: Tag) -> bool:
        text = container.get_text().lower()
        return "replying to" in text

    def _extract_aria_count(self, container: Tag, selector: str) -> int | None:
        el = container.select_one(selector)
        if el:
            label = el.get("aria-label", "")
            m = re.search(r"(\d[\d,]*)\s", label)
            if m:
                try:
                    return int(m.group(1).replace(",", ""))
                except ValueError:
                    return None
            # Try text content as fallback
            text = el.get_text(strip=True)
            try:
                return int(text.replace(",", ""))
            except ValueError:
                return None
        return None

    def _extract_media_from_html(self, container: Tag) -> list[Media]:
        """Extract media items from a tweet HTML container.

        In Wayback Machine playback pages, media ``src`` attributes are
        rewritten to full ``web.archive.org`` URLs.  We store the full
        URL so the downloader can use it directly without reconstruction.
        """
        media_items: list[Media] = []

        for img in container.select("img"):
            src = img.get("src", "")
            if src and not src.endswith((".svg", ".ico")):
                media_items.append(Media(
                    tweet_id=0, url=src, wayback_url=src,
                    media_type="image",
                ))

        for video in container.select("video"):
            poster = video.get("poster", "")
            if poster:
                media_items.append(Media(
                    tweet_id=0, url=poster, wayback_url=poster,
                    media_type="image",
                ))
            src = video.get("src", "")
            if src:
                media_items.append(Media(
                    tweet_id=0, url=src, wayback_url=src,
                    media_type="video",
                ))

        for source in container.select("video source, source"):
            src = source.get("src", "")
            if src:
                media_items.append(Media(
                    tweet_id=0, url=src, wayback_url=src,
                    media_type="video",
                ))

        return media_items


# ── Helpers ───────────────────────────────────────────────────────────

def _deep_get(d: dict[str, Any], *keys: str) -> Any:
    """Safely traverse a nested dict by keys."""
    for key in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(key, {})
    return d if d else None


def _parse_twitter_date(date_str: str) -> str | None:
    """Parse Twitter's date format to ISO 8601."""
    from datetime import datetime as dt
    fmt = "%a %b %d %H:%M:%S %z %Y"
    return dt.strptime(date_str, fmt).isoformat()
