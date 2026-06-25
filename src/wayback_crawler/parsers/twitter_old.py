"""Parser for pre-2017 classic Twitter archived pages."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from bs4 import BeautifulSoup, Tag

from wayback_crawler.models import Media, ParseResult, Tweet
from wayback_crawler.parsers.base import BaseParser
from wayback_crawler.utils import extract_original_url, parse_twitter_status_id

logger = logging.getLogger(__name__)

TWEET_CONTAINERS = [".tweet", ".js-tweet", "li.js-stream-item", ".permalink-tweet"]
TWEET_TEXT_SEL = [".tweet-text", ".js-tweet-text", ".TweetTextSize"]
AUTHOR_DISPLAY_SEL = [".fullname", ".username"]
AUTHOR_HANDLE_SEL = [".username > b", ".username > s", ".js-action-profile-name"]
TIMESTAMP_SEL = ["._timestamp", ".tweet-timestamp > span"]
MEDIA_SEL = [".AdaptiveMedia img", ".media img", ".tweet-media img"]
RETWEET_TEXT_SEL = [".js-retweet-text"]
REPLY_SEL = [".ReplyingToContextBelowAuthor", ".tweet-reply-context"]

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



class TwitterOldParser(BaseParser):
    """Parser for classic Twitter HTML (circa 2010–2017)."""

    @property
    def domain(self) -> str:
        return "twitter.com"

    def can_parse(self, html: str, url: str) -> bool:
        lower = html.lower()
        if any(p in lower for p in DELETED_PATTERNS):
            return True
        if any(m.lower() in html.lower() for m in LOGIN_FORM_MARKERS):
            return False
        has_old = any(selector.lstrip(".") for selector in TWEET_CONTAINERS
                      if f'class="{selector.lstrip(".")}"' in lower
                      or f"class='{selector.lstrip('.')}'" in lower)
        has_react = '<div id="react-root">' in lower
        return has_old and not has_react

    def parse(self, html: str, url: str, snapshot_timestamp: str) -> ParseResult:
        result = ParseResult()

        lower = html.lower()
        if any(p in lower for p in DELETED_PATTERNS):
            result.errors.append("Tweet deleted or unavailable")
            return result
        if any(m.lower() in html.lower() for m in LOGIN_FORM_MARKERS):
            result.errors.append("Login page — no tweet content available")
            return result

        soup = BeautifulSoup(html, "lxml")

        # Strip Wayback toolbar and script/style
        for tag in soup.find_all(["script", "style"]):
            tag.decompose()
        wm_toolbar = soup.find("div", id="wm-ipp-base")
        if wm_toolbar:
            wm_toolbar.decompose()

        containers = soup.select(", ".join(TWEET_CONTAINERS))
        if not containers:
            containers = soup.find_all("div", class_=re.compile(r"tweet|Tweet"))

        for container in containers:
            tweet = self._extract_tweet(container, snapshot_timestamp)
            if tweet:
                tweet.snapshot_id = 0  # caller will set
                result.tweets.append(tweet)
                media_items = self._extract_media(container)
                result.media.extend(media_items)

        return result

    def _extract_tweet(self, container: Tag, snapshot_timestamp: str) -> Tweet | None:
        tweet_id = self._extract_tweet_id(container)
        if not tweet_id:
            return None

        author_handle = self._extract_author_handle(container)
        author_display = self._extract_author_display(container)
        text = self._extract_text(container)
        timestamp = self._extract_timestamp(container)
        is_retweet = self._is_retweet(container)
        is_reply = self._is_reply(container)
        reply_count = self._extract_count(container, "reply")
        retweet_count = self._extract_count(container, "retweet")
        like_count = self._extract_count(container, "favorite")

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
            parsed_with="twitter_old",
        )

    def _extract_tweet_id(self, container: Tag) -> str | None:
        tid = container.get("data-tweet-id") or container.get("data-item-id")
        if tid:
            return str(tid)
        permalink = container.select_one("a[href*='/status/']")
        if permalink:
            href = permalink.get("href", "")
            return parse_twitter_status_id(href)
        return None

    def _extract_author_handle(self, container: Tag) -> str | None:
        for sel in AUTHOR_HANDLE_SEL:
            el = container.select_one(sel)
            if el:
                text = el.get_text(strip=True)
                if text and text != "@":
                    return text if text.startswith("@") else f"@{text}"
        username = container.get("data-screen-name")
        if username:
            return f"@{username}"
        return None

    def _extract_author_display(self, container: Tag) -> str | None:
        name = container.get("data-name")
        if name:
            return name
        for sel in AUTHOR_DISPLAY_SEL:
            el = container.select_one(sel)
            if el:
                return el.get_text(strip=True)
        return None

    def _extract_text(self, container: Tag) -> str | None:
        for sel in TWEET_TEXT_SEL:
            el = container.select_one(sel)
            if el:
                return el.get_text(separator="\n", strip=True)
        return None

    def _extract_timestamp(self, container: Tag) -> str | None:
        for sel in TIMESTAMP_SEL:
            el = container.select_one(sel)
            if el and el.get("data-time"):
                try:
                    ts = int(el["data-time"])
                    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                except (ValueError, OSError):
                    pass
            if el and el.get("title"):
                return el["title"]
        return None

    def _is_retweet(self, container: Tag) -> bool:
        if "retweeted" in (container.get("class", []) or []):
            return True
        for sel in RETWEET_TEXT_SEL:
            if container.select_one(sel):
                return True
        return False

    def _is_reply(self, container: Tag) -> bool:
        if "is-reply" in (container.get("class", []) or []):
            return True
        for sel in REPLY_SEL:
            if container.select_one(sel):
                return True
        return False

    def _extract_count(self, container: Tag, action: str) -> int | None:
        sel_map = {
            "reply": ".ProfileTweet-action--reply",
            "retweet": ".ProfileTweet-action--retweet",
            "favorite": ".ProfileTweet-action--favorite",
        }
        sel = sel_map.get(action)
        if not sel:
            return None
        el = container.select_one(f"{sel} .ProfileTweet-actionCount")
        if el:
            val = el.get("data-tweet-stat-count") or el.get_text(strip=True)
            try:
                return int(val.replace(",", ""))
            except (ValueError, AttributeError):
                return None
        return None

    def _extract_media(self, container: Tag) -> list[Media]:
        """Extract media items from a tweet HTML container.

        In Wayback Machine playback pages, media ``src`` attributes are
        rewritten to ``web.archive.org`` URLs.  We extract the original
        URL so the downloader can construct the correct ``im_`` URL.
        """
        media_items: list[Media] = []

        for img in container.select("img"):
            src = img.get("src", "")
            if src and not src.endswith((".svg", ".ico")):
                original = extract_original_url(src)
                if original:
                    media_items.append(Media(
                        tweet_id=0, url=original, media_type="image",
                    ))

        for video in container.select("video"):
            src = video.get("src", "")
            if src:
                original = extract_original_url(src)
                if original:
                    media_items.append(Media(
                        tweet_id=0, url=original, media_type="video",
                    ))

        for source in container.select("video source, source"):
            src = source.get("src", "")
            if src:
                original = extract_original_url(src)
                if original:
                    media_items.append(Media(
                        tweet_id=0, url=original, media_type="video",
                    ))

        return media_items
