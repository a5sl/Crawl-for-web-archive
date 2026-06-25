"""SQLite repository for the Wayback crawler — async via aiosqlite."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

from wayback_crawler.models import CrawlRun, Media, Snapshot, Tweet

logger = logging.getLogger(__name__)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS crawl_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    url_pattern         TEXT NOT NULL,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    total_snapshots     INTEGER NOT NULL DEFAULT 0,
    fetched_snapshots   INTEGER NOT NULL DEFAULT 0,
    failed_snapshots    INTEGER NOT NULL DEFAULT 0,
    tweets_extracted    INTEGER NOT NULL DEFAULT 0,
    config_snapshot     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    crawl_run_id    INTEGER NOT NULL REFERENCES crawl_runs(id) ON DELETE CASCADE,
    timestamp       TEXT NOT NULL,
    original_url    TEXT NOT NULL,
    wayback_url     TEXT NOT NULL,
    fetch_status    TEXT NOT NULL DEFAULT 'pending',
    retry_count     INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(crawl_run_id, timestamp, original_url)
);

CREATE TABLE IF NOT EXISTS tweets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id     INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    tweet_id        TEXT NOT NULL,
    author_handle   TEXT NOT NULL,
    author_display  TEXT,
    text            TEXT NOT NULL,
    timestamp       TEXT,
    reply_count     INTEGER,
    retweet_count   INTEGER,
    like_count      INTEGER,
    quote_count     INTEGER,
    is_retweet      INTEGER NOT NULL DEFAULT 0,
    is_reply        INTEGER NOT NULL DEFAULT 0,
    parsed_with     TEXT NOT NULL,
    raw_metadata    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(snapshot_id, tweet_id)
);

CREATE TABLE IF NOT EXISTS media (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tweet_id    INTEGER NOT NULL REFERENCES tweets(id) ON DELETE CASCADE,
    url         TEXT NOT NULL,
    media_type  TEXT NOT NULL,
    wayback_url TEXT,
    downloaded  INTEGER NOT NULL DEFAULT 0,
    local_path  TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_snapshots_run_status
    ON snapshots(crawl_run_id, fetch_status);
CREATE INDEX IF NOT EXISTS idx_snapshots_timestamp_url
    ON snapshots(timestamp, original_url);
CREATE INDEX IF NOT EXISTS idx_tweets_snapshot ON tweets(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_tweets_author ON tweets(author_handle);
CREATE INDEX IF NOT EXISTS idx_tweets_tweet_id ON tweets(tweet_id);
CREATE INDEX IF NOT EXISTS idx_media_tweet ON media(tweet_id);
"""


class Storage:
    """Async SQLite repository for crawl data."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """Open the connection and create schema. Migrates existing DBs."""
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)

        # Migration: add local_path column if upgrading from older schema
        try:
            await self._conn.execute(
                "ALTER TABLE media ADD COLUMN local_path TEXT"
            )
        except aiosqlite.OperationalError:
            pass  # column already exists

        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Storage not initialized — call initialize() first")
        return self._conn

    # ── CrawlRun ──────────────────────────────────────────────────────

    async def create_crawl_run(self, run: CrawlRun) -> CrawlRun:
        cursor = await self.conn.execute(
            """INSERT INTO crawl_runs (url_pattern, started_at, config_snapshot)
               VALUES (?, ?, ?)""",
            (run.url_pattern, run.started_at, run.config_snapshot),
        )
        await self.conn.commit()
        run.id = cursor.lastrowid
        return run

    async def update_crawl_run(self, run: CrawlRun) -> None:
        await self.conn.execute(
            """UPDATE crawl_runs SET
               finished_at=?, total_snapshots=?, fetched_snapshots=?,
               failed_snapshots=?, tweets_extracted=?
               WHERE id=?""",
            (run.finished_at, run.total_snapshots, run.fetched_snapshots,
             run.failed_snapshots, run.tweets_extracted, run.id),
        )
        await self.conn.commit()

    async def get_crawl_run(self, run_id: int) -> CrawlRun | None:
        cursor = await self.conn.execute(
            "SELECT * FROM crawl_runs WHERE id=?", (run_id,)
        )
        row = await cursor.fetchone()
        return _row_to_crawl_run(row) if row else None

    async def get_latest_run(self) -> CrawlRun | None:
        cursor = await self.conn.execute(
            "SELECT * FROM crawl_runs ORDER BY id DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        return _row_to_crawl_run(row) if row else None

    async def list_runs(self, limit: int = 20) -> list[CrawlRun]:
        cursor = await self.conn.execute(
            "SELECT * FROM crawl_runs ORDER BY id DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [_row_to_crawl_run(r) for r in rows]

    # ── Snapshots ─────────────────────────────────────────────────────

    async def insert_snapshot(self, snap: Snapshot) -> Snapshot:
        cursor = await self.conn.execute(
            """INSERT OR IGNORE INTO snapshots
               (crawl_run_id, timestamp, original_url, wayback_url)
               VALUES (?, ?, ?, ?)""",
            (snap.crawl_run_id, snap.timestamp, snap.original_url, snap.wayback_url),
        )
        await self.conn.commit()
        if cursor.rowcount and cursor.rowcount > 0:
            snap.id = cursor.lastrowid
        return snap

    async def insert_snapshots_batch(self, snaps: list[Snapshot]) -> int:
        """Insert a batch of snapshots. Returns count of actually inserted rows."""
        inserted = 0
        for snap in snaps:
            await self.insert_snapshot(snap)
            if snap.id is not None:
                inserted += 1
        return inserted

    async def get_pending_snapshots(self, run_id: int,
                                    limit: int | None = None,
                                    include_failed: bool = False) -> list[Snapshot]:
        if include_failed:
            sql = """SELECT * FROM snapshots
                     WHERE crawl_run_id=? AND fetch_status IN ('pending','failed')
                     ORDER BY timestamp ASC"""
        else:
            sql = """SELECT * FROM snapshots
                     WHERE crawl_run_id=? AND fetch_status='pending'
                     ORDER BY timestamp ASC"""
        if limit is not None:
            sql += " LIMIT ?"
            cursor = await self.conn.execute(sql, (run_id, limit))
        else:
            cursor = await self.conn.execute(sql, (run_id,))
        rows = await cursor.fetchall()
        return [_row_to_snapshot(r) for r in rows]

    async def update_snapshot_status(self, snap_id: int, status: str,
                                     error: str | None = None) -> None:
        await self.conn.execute(
            """UPDATE snapshots SET fetch_status=?, last_error=?,
               retry_count=retry_count+1, updated_at=datetime('now')
               WHERE id=?""",
            (status, error, snap_id),
        )
        await self.conn.commit()

    async def count_snapshots_by_status(self, run_id: int) -> dict[str, int]:
        cursor = await self.conn.execute(
            """SELECT fetch_status, COUNT(*) as cnt FROM snapshots
               WHERE crawl_run_id=? GROUP BY fetch_status""",
            (run_id,),
        )
        rows = await cursor.fetchall()
        return {r["fetch_status"]: r["cnt"] for r in rows}

    # ── Tweets ────────────────────────────────────────────────────────

    async def insert_tweet(self, tweet: Tweet) -> Tweet:
        cursor = await self.conn.execute(
            """INSERT OR IGNORE INTO tweets
               (snapshot_id, tweet_id, author_handle, author_display, text,
                timestamp, reply_count, retweet_count, like_count, quote_count,
                is_retweet, is_reply, parsed_with, raw_metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (tweet.snapshot_id, tweet.tweet_id, tweet.author_handle,
             tweet.author_display, tweet.text, tweet.timestamp,
             tweet.reply_count, tweet.retweet_count, tweet.like_count,
             tweet.quote_count, int(tweet.is_retweet), int(tweet.is_reply),
             tweet.parsed_with, tweet.raw_metadata),
        )
        await self.conn.commit()
        if cursor.rowcount and cursor.rowcount > 0:
            tweet.id = cursor.lastrowid
        return tweet

    async def insert_media_batch(self, media_items: list[Media]) -> None:
        for m in media_items:
            cursor = await self.conn.execute(
                """INSERT INTO media (tweet_id, url, media_type, wayback_url, downloaded, local_path)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (m.tweet_id, m.url, m.media_type, m.wayback_url,
                 int(m.downloaded), m.local_path),
            )
            if cursor.rowcount and cursor.rowcount > 0:
                m.id = cursor.lastrowid
        await self.conn.commit()

    async def update_media_download(self, media_id: int, local_path: str,
                                    wayback_url: str | None = None) -> None:
        """Mark a media item as downloaded with its local file path."""
        await self.conn.execute(
            """UPDATE media SET downloaded=1, local_path=?,
               wayback_url=COALESCE(?, wayback_url) WHERE id=?""",
            (local_path, wayback_url, media_id),
        )
        await self.conn.commit()

    async def get_undownloaded_media(self, run_id: int) -> list[dict]:
        """Get all media items for a run that haven't been downloaded yet."""
        cursor = await self.conn.execute(
            """SELECT m.* FROM media m
               JOIN tweets t ON m.tweet_id = t.id
               JOIN snapshots s ON t.snapshot_id = s.id
               WHERE s.crawl_run_id = ? AND m.downloaded = 0""",
            (run_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_tweets_by_run(self, run_id: int) -> list[dict]:
        cursor = await self.conn.execute(
            """SELECT t.* FROM tweets t
               JOIN snapshots s ON t.snapshot_id = s.id
               WHERE s.crawl_run_id = ?
               ORDER BY t.timestamp DESC""",
            (run_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_tweet_count(self, run_id: int) -> int:
        cursor = await self.conn.execute(
            """SELECT COUNT(*) as cnt FROM tweets t
               JOIN snapshots s ON t.snapshot_id = s.id
               WHERE s.crawl_run_id = ?""",
            (run_id,),
        )
        row = await cursor.fetchone()
        return row["cnt"] if row else 0

    # ── Export ────────────────────────────────────────────────────────

    async def export_tweets(self, run_id: int) -> list[dict]:
        cursor = await self.conn.execute(
            """SELECT t.*, s.timestamp as snapshot_timestamp, s.original_url
               FROM tweets t
               JOIN snapshots s ON t.snapshot_id = s.id
               WHERE s.crawl_run_id = ?
               ORDER BY t.timestamp DESC""",
            (run_id,),
        )
        rows = await cursor.fetchall()
        results: list[dict] = []
        for row in rows:
            d = dict(row)
            d["is_retweet"] = bool(d["is_retweet"])
            d["is_reply"] = bool(d["is_reply"])
            results.append(d)
        return results

    async def export_media(self, run_id: int) -> list[dict]:
        cursor = await self.conn.execute(
            """SELECT m.* FROM media m
               JOIN tweets t ON m.tweet_id = t.id
               JOIN snapshots s ON t.snapshot_id = s.id
               WHERE s.crawl_run_id = ?""",
            (run_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


# ── Row mappers ───────────────────────────────────────────────────────

def _row_to_crawl_run(row: aiosqlite.Row) -> CrawlRun:
    return CrawlRun(
        id=row["id"],
        url_pattern=row["url_pattern"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        total_snapshots=row["total_snapshots"],
        fetched_snapshots=row["fetched_snapshots"],
        failed_snapshots=row["failed_snapshots"],
        tweets_extracted=row["tweets_extracted"],
        config_snapshot=row["config_snapshot"],
    )


def _row_to_snapshot(row: aiosqlite.Row) -> Snapshot:
    return Snapshot(
        id=row["id"],
        crawl_run_id=row["crawl_run_id"],
        timestamp=row["timestamp"],
        original_url=row["original_url"],
        wayback_url=row["wayback_url"],
        fetch_status=row["fetch_status"],
        retry_count=row["retry_count"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
