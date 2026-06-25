"""One-click crawl test — queries, fetches, downloads media, and reports."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

# Ensure src is on path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from wayback_crawler.config import Config, load_config
from wayback_crawler.cdx import CdxClient
from wayback_crawler.fetcher import SnapshotFetcher
from wayback_crawler.media_downloader import download_media
from wayback_crawler.models import CrawlRun, Snapshot
from wayback_crawler.parser import ParserDispatcher
from wayback_crawler.storage import Storage
from wayback_crawler.utils import compute_wayback_url, now_iso, setup_logging, extract_handle

# ── Config ────────────────────────────────────────────────────────────

TARGET_HANDLE = "youzaibaobao"
URL_PATTERN = f"twitter.com/{TARGET_HANDLE}/*"
MEDIA_DIR = Path("media") / TARGET_HANDLE  # ./media/{username}/


async def main() -> None:
    cfg = load_config()
    setup_logging("WARNING")  # quieter output

    # ── 1. Init database ────────────────────────────────────────────
    db_path = f"twitter.db"
    print(f"[1/5] Database initialized: {db_path}")

    db = Storage(db_path)
    await db.initialize()

    try:
        # ── 2. Query CDX ───────────────────────────────────────────
        print(f"[2/5] Querying CDX for {URL_PATTERN} ...")
        async with httpx.AsyncClient(
            timeout=cfg.fetcher.timeout,
            headers={"User-Agent": cfg.fetcher.user_agent},
        ) as client:
            cdx = CdxClient(cfg.cdx, client)
            entries = await cdx.query(URL_PATTERN)

        print(f"      Found {len(entries)} snapshots")

        run = CrawlRun(url_pattern=URL_PATTERN, config_snapshot=cfg.to_json())
        run = await db.create_crawl_run(run)

        snapshots = [
            Snapshot(
                crawl_run_id=run.id,
                timestamp=e["timestamp"],
                original_url=e["original"],
                wayback_url=compute_wayback_url(e["timestamp"], e["original"]),
            )
            for e in entries
        ]
        await db.insert_snapshots_batch(snapshots)
        run.total_snapshots = len(snapshots)
        await db.update_crawl_run(run)

        # ── 3. Fetch & parse ────────────────────────────────────────
        pending = await db.get_pending_snapshots(run.id)
        print(f"[3/5] Fetching & parsing {len(pending)} snapshots ...")

        MEDIA_DIR.mkdir(exist_ok=True)
        dispatcher = ParserDispatcher()
        tweet_count = 0
        download_count = 0
        lock = asyncio.Lock()

        async def on_result(snapshot: Snapshot, html: str | None) -> None:
            nonlocal tweet_count, download_count
            if html is None:
                await db.update_snapshot_status(snapshot.id, snapshot.fetch_status,
                                                 snapshot.last_error)
                return
            await db.update_snapshot_status(snapshot.id, "fetched")
            result = dispatcher.parse(snapshot, html)
            for tweet in result.tweets:
                tweet.snapshot_id = snapshot.id
                stored = await db.insert_tweet(tweet)
                if stored.id:
                    async with lock:
                        tweet_count += 1
                    if result.media:
                        for m in result.media:
                            m.tweet_id = stored.id
                        await db.insert_media_batch(result.media)
                        for m in result.media:
                            local_path = await download_media(
                                fetch_client, m.url, snapshot.timestamp,
                                str(MEDIA_DIR), cfg.fetcher.user_agent,
                                timeout=cfg.fetcher.timeout, max_retries=0,
                            )
                            if local_path and m.id:
                                await db.update_media_download(m.id, local_path)
                                async with lock:
                                    download_count += 1

        async with httpx.AsyncClient(
            timeout=cfg.fetcher.timeout,
            headers={"User-Agent": cfg.fetcher.user_agent},
            follow_redirects=True,
        ) as fetch_client:
            fetcher = SnapshotFetcher(cfg.fetcher, fetch_client)
            await fetcher.fetch_many(pending, on_result=on_result)

        # ── 4. Update run ───────────────────────────────────────────
        counts = await db.count_snapshots_by_status(run.id)
        run.fetched_snapshots = counts.get("fetched", 0)
        run.failed_snapshots = counts.get("failed", 0)
        run.tweets_extracted = tweet_count
        run.finished_at = now_iso()
        await db.update_crawl_run(run)

        # ── 5. Report ──────────────────────────────────────────────
        print(f"[4/5] Generating report ...")
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        # Gather stats for this run only
        cur = await db.conn.execute(
            """SELECT m.media_type, m.downloaded, COUNT(*) as c
               FROM media m
               JOIN tweets t ON m.tweet_id = t.id
               JOIN snapshots s ON t.snapshot_id = s.id
               WHERE s.crawl_run_id = ?
               GROUP BY m.media_type, m.downloaded""",
            (run.id,)
        )
        media_stats = await cur.fetchall()

        cur = await db.conn.execute(
            """SELECT t.tweet_id, t.text, t.author_handle, t.timestamp,
                      m.media_type, m.url as media_url, m.downloaded, m.local_path
               FROM tweets t
               LEFT JOIN media m ON m.tweet_id = t.id
               WHERE t.snapshot_id IN (SELECT id FROM snapshots WHERE crawl_run_id = ?)
               ORDER BY t.timestamp""",
            (run.id,),
        )
        rows = await cur.fetchall()

        # Build tweet index
        tweet_map: dict[str, dict] = {}
        for r in rows:
            tid = r["tweet_id"]
            if tid not in tweet_map:
                tweet_map[tid] = {
                    "tweet_id": tid,
                    "author": r["author_handle"],
                    "text": (r["text"] or "")[:120],
                    "timestamp": r["timestamp"],
                    "media": [],
                }
            if r["media_url"]:
                tweet_map[tid]["media"].append({
                    "type": r["media_type"],
                    "url": r["media_url"],
                    "downloaded": bool(r["downloaded"]),
                    "local_path": r["local_path"],
                })

        total_videos = sum(m["c"] for m in media_stats if m["media_type"] == "video" and m["downloaded"] == 1)
        total_images = sum(m["c"] for m in media_stats if m["media_type"] == "image" and m["downloaded"] == 1)
        total_video_urls = sum(m["c"] for m in media_stats if m["media_type"] == "video")
        total_media = sum(m["c"] for m in media_stats)
        total_downloaded = sum(m["c"] for m in media_stats if m["downloaded"] == 1)



        print(f"[5/5] Done!")
        print()
        print(f"  Tweets:   {run.tweets_extracted}")
        print(f"  Media:    {total_downloaded} downloaded ({total_videos} videos, {total_images} images)")
        print(f"  Files in: {MEDIA_DIR.resolve()}/")

    finally:
        await db.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    start = time.monotonic()
    asyncio.run(main())
    elapsed = time.monotonic() - start
    print(f"\nTotal time: {elapsed:.0f}s")
