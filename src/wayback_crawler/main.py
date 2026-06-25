"""CLI entry point for the Wayback Machine crawler."""

from __future__ import annotations

import asyncio
import csv
import io
import json as json_mod
import logging
import sys
from pathlib import Path
from typing import Optional

import httpx
import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
from rich.table import Table

from wayback_crawler import __version__
from wayback_crawler.cdx import CdxClient
from wayback_crawler.config import DEFAULT_CONFIG_YAML, Config, load_config
from wayback_crawler.fetcher import SnapshotFetcher
from wayback_crawler.media_downloader import download_media
from wayback_crawler.models import CrawlRun, Snapshot, Tweet
from wayback_crawler.parser import ParserDispatcher
from wayback_crawler.storage import Storage
from wayback_crawler.utils import (
    compute_wayback_url,
    now_iso,
    setup_logging,
)

app = typer.Typer(
    name="wayback-crawler",
    help="Crawl the Wayback Machine to extract structured data from archived pages.",
    add_completion=False,
)
console = Console()
logger = logging.getLogger(__name__)

_config: Config | None = None
_storage: Storage | None = None

# ── Shared options ─────────────────────────────────────────────────────

def _get_config(config_path: Optional[Path] = None) -> Config:
    global _config
    if _config is None:
        _config = load_config(str(config_path) if config_path else None)
    return _config


async def _get_storage(config_path: Optional[Path] = None) -> Storage:
    global _storage
    if _storage is None:
        cfg = _get_config(config_path)
        s = Storage(cfg.database.path)
        await s.initialize()
        _storage = s
    return _storage


# ── Progress helper ────────────────────────────────────────────────────

def _progress(description: str) -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TextColumn("[cyan]{task.completed}/{task.total}"),
        console=console,
    )


# ═══════════════════════════════════════════════════════════════════════
#  init
# ═══════════════════════════════════════════════════════════════════════

@app.command()
def init(
    force: bool = typer.Option(False, "--force", help="Overwrite existing config file"),
) -> None:
    """Generate a config.example.yaml file in the current directory."""
    target = Path("config.example.yaml")
    if target.exists() and not force:
        console.print(f"[yellow]{target} already exists. Use --force to overwrite.[/yellow]")
        raise typer.Exit(1)

    target.write_text(DEFAULT_CONFIG_YAML, encoding="utf-8")
    console.print(f"[green]Generated {target}[/green]")


# ═══════════════════════════════════════════════════════════════════════
#  query
# ═══════════════════════════════════════════════════════════════════════

@app.command()
def query(
    url_pattern: str = typer.Argument(..., help="URL pattern (e.g. 'twitter.com/user/*')"),
    from_date: Optional[str] = typer.Option(None, "--from", help="Start date (YYYYMMDDHHMMSS)"),
    to_date: Optional[str] = typer.Option(None, "--to", help="End date (YYYYMMDDHHMMSS)"),
    limit: Optional[int] = typer.Option(None, "--limit", "-n", help="Max CDX results"),
    collapse: str = typer.Option("digest", help="CDX collapse field"),
    match_type: str = typer.Option("prefix", help="CDX match type"),
    output_format: str = typer.Option("text", "--output-format", help="text|json"),
    config_path: Optional[Path] = typer.Option(None, "--config", exists=True),
) -> None:
    """Query the Wayback Machine CDX API and store discovered snapshots."""
    # Windows compatibility: ensure proper event loop policy
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(_query_async(url_pattern, from_date, to_date, limit,
                               collapse, match_type, output_format, config_path))


async def _query_async(
    url_pattern: str, from_date: str | None, to_date: str | None,
    limit: int | None, collapse: str, match_type: str,
    output_format: str, config_path: Path | None,
) -> None:
    cfg = _get_config(config_path)
    db = await _get_storage(config_path)

    # Override CDX config with CLI args
    cfg.cdx.collapse = collapse
    cfg.cdx.match_type = match_type

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(cfg.fetcher.timeout),
            headers={"User-Agent": cfg.fetcher.user_agent},
        ) as client:
            cdx_client = CdxClient(cfg.cdx, client)
            entries = await cdx_client.query(
                url_pattern,
                from_date=from_date,
                to_date=to_date,
                limit=limit,
            )
    except httpx.TimeoutException:
        console.print("[red]Error: CDX API request timed out. Check your network connection.[/red]")
        raise typer.Exit(1)
    except httpx.RequestError as e:
        console.print(f"[red]Error: Failed to connect to CDX API: {e}[/red]")
        raise typer.Exit(1)

    if not entries:
        console.print("[yellow]No snapshots found for this pattern.[/yellow]")
        return

    # Create a crawl run
    run = CrawlRun(url_pattern=url_pattern, config_snapshot=cfg.to_json())
    run = await db.create_crawl_run(run)

    # Insert snapshots
    snapshots = [
        Snapshot(
            crawl_run_id=run.id,
            timestamp=e["timestamp"],
            original_url=e["original"],
            wayback_url=compute_wayback_url(e["timestamp"], e["original"]),
        )
        for e in entries
    ]
    inserted = await db.insert_snapshots_batch(snapshots)

    run.total_snapshots = len(snapshots)
    await db.update_crawl_run(run)

    if output_format == "json":
        console.print(json_mod.dumps(entries, indent=2, ensure_ascii=False))
    else:
        console.print(f"\n[bold]URL Pattern:[/bold] {url_pattern}")
        console.print(f"[bold]Run ID:[/bold] {run.id}")
        console.print(f"[bold]Total snapshots found:[/bold] {len(entries)}")
        console.print(f"[bold]New snapshots inserted:[/bold] {inserted}")
        if entries:
            console.print(f"\n[dim]First: {entries[0]['timestamp']} → {entries[0]['original'][:80]}[/dim]")
            console.print(f"[dim]Last:  {entries[-1]['timestamp']} → {entries[-1]['original'][:80]}[/dim]")


# ═══════════════════════════════════════════════════════════════════════
#  fetch
# ═══════════════════════════════════════════════════════════════════════

@app.command()
def fetch(
    run_id: Optional[int] = typer.Option(None, "--run-id", help="Target crawl run (default: latest)"),
    concurrency: Optional[int] = typer.Option(None, "--concurrency", "-c", help="Max concurrent fetches"),
    limit: Optional[int] = typer.Option(None, "--limit", "-n", help="Max snapshots to process"),
    no_parse: bool = typer.Option(False, "--no-parse", help="Fetch HTML but skip parsing"),
    media_dir: Optional[Path] = typer.Option(None, "--media-dir", help="Download media to this directory"),
    config_path: Optional[Path] = typer.Option(None, "--config", exists=True),
) -> None:
    """Fetch and parse pending snapshots from the database."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(_fetch_async(run_id, concurrency, limit, no_parse, media_dir, config_path))


async def _fetch_async(
    run_id: int | None, concurrency: int | None,
    limit: int | None, no_parse: bool, media_dir: Path | None, config_path: Path | None,
) -> None:
    cfg = _get_config(config_path)
    db = await _get_storage(config_path)
    setup_logging(cfg.logging.level, cfg.logging.file, cfg.logging.format)

    if concurrency is not None:
        cfg.fetcher.concurrency = concurrency

    if run_id is None:
        run = await db.get_latest_run()
        if run is None:
            console.print("[red]No crawl runs found. Run 'query' first.[/red]")
            raise typer.Exit(1)
    else:
        run = await db.get_crawl_run(run_id)
        if run is None:
            console.print(f"[red]Run {run_id} not found.[/red]")
            raise typer.Exit(1)

    pending = await db.get_pending_snapshots(run.id, limit=limit)
    if not pending:
        console.print("[green]No pending snapshots — all done![/green]")
        return

    console.print(f"[bold]Run #{run.id}[/bold]: Fetching {len(pending)} snapshots "
                  f"(concurrency={cfg.fetcher.concurrency})")

    dispatcher = ParserDispatcher(prefer_json=cfg.parsers.twitter.prefer_json)
    fetched_count = 0
    failed_count = 0
    tweet_count = 0
    lock = asyncio.Lock()

    async def on_result(snapshot: Snapshot, html: str | None) -> None:
        nonlocal fetched_count, failed_count, tweet_count

        if html is None:
            async with lock:
                failed_count += 1
            await db.update_snapshot_status(snapshot.id, snapshot.fetch_status,
                                             snapshot.last_error)
            return

        await db.update_snapshot_status(snapshot.id, "fetched")
        async with lock:
            fetched_count += 1

        if no_parse:
            return

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
                    if media_dir:
                        for m in result.media:
                            local_path = await download_media(
                                client, m.url, snapshot.timestamp,
                                str(media_dir), cfg.fetcher.user_agent,
                                timeout=cfg.fetcher.timeout,
                                wayback_url=m.wayback_url,
                            )
                            if local_path and m.id:
                                await db.update_media_download(m.id, local_path, m.wayback_url)

    async with httpx.AsyncClient(
        timeout=cfg.fetcher.timeout,
        headers={"User-Agent": cfg.fetcher.user_agent},
        follow_redirects=True,
    ) as client:
        fetcher = SnapshotFetcher(cfg.fetcher, client)
        await fetcher.fetch_many(pending, on_result=on_result)

    # Update run counters
    counts = await db.count_snapshots_by_status(run.id)
    run.fetched_snapshots = counts.get("fetched", 0) + counts.get("skipped", 0)
    run.failed_snapshots = counts.get("failed", 0)
    run.tweets_extracted = await db.get_tweet_count(run.id)
    run.finished_at = now_iso()
    await db.update_crawl_run(run)

    console.print(f"\n[bold green]Fetch complete.[/bold green] "
                  f"Fetched: {fetched_count}, Failed: {failed_count}, Tweets: {tweet_count}")


# ═══════════════════════════════════════════════════════════════════════
#  crawl
# ═══════════════════════════════════════════════════════════════════════

@app.command()
def crawl(
    url_pattern: str = typer.Argument(..., help="URL pattern (e.g. 'twitter.com/user/*')"),
    from_date: Optional[str] = typer.Option(None, "--from", help="Start date (YYYYMMDDHHMMSS)"),
    to_date: Optional[str] = typer.Option(None, "--to", help="End date (YYYYMMDDHHMMSS)"),
    limit: Optional[int] = typer.Option(None, "--limit", "-n", help="Max CDX results"),
    concurrency: Optional[int] = typer.Option(None, "--concurrency", "-c", help="Max concurrent fetches"),
    no_parse: bool = typer.Option(False, "--no-parse", help="Fetch HTML but skip parsing"),
    media_dir: Optional[Path] = typer.Option(None, "--media-dir", help="Download media to this directory"),
    config_path: Optional[Path] = typer.Option(None, "--config", exists=True),
) -> None:
    """Run the full pipeline: query CDX, fetch, and parse snapshots."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(_crawl_async(url_pattern, from_date, to_date, limit,
                               concurrency, no_parse, media_dir, config_path))


async def _crawl_async(
    url_pattern: str, from_date: str | None, to_date: str | None,
    limit: int | None, concurrency: int | None, no_parse: bool,
    media_dir: Path | None, config_path: Path | None,
) -> None:
    # Phase 1: Query
    await _query_async(url_pattern, from_date, to_date, limit,
                       "digest", "prefix", "text", config_path)

    # Phase 2: Fetch the run we just created
    await _fetch_async(None, concurrency, None, no_parse, media_dir, config_path)


# ═══════════════════════════════════════════════════════════════════════
#  resume
# ═══════════════════════════════════════════════════════════════════════

@app.command()
def resume(
    run_id: Optional[int] = typer.Option(None, "--run-id", help="Resume a specific run (default: latest)"),
    retry_failed: bool = typer.Option(False, "--retry-failed", help="Also retry previously failed snapshots"),
    concurrency: Optional[int] = typer.Option(None, "--concurrency", "-c"),
    limit: Optional[int] = typer.Option(None, "--limit", "-n"),
    media_dir: Optional[Path] = typer.Option(None, "--media-dir", help="Download media to this directory"),
    config_path: Optional[Path] = typer.Option(None, "--config", exists=True),
) -> None:
    """Resume an incomplete crawl run."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(_resume_async(run_id, retry_failed, concurrency, limit, media_dir, config_path))


async def _resume_async(
    run_id: int | None, retry_failed: bool,
    concurrency: int | None, limit: int | None,
    media_dir: Path | None, config_path: Path | None,
) -> None:
    cfg = _get_config(config_path)
    db = await _get_storage(config_path)
    setup_logging(cfg.logging.level, cfg.logging.file, cfg.logging.format)

    if concurrency is not None:
        cfg.fetcher.concurrency = concurrency

    if run_id is None:
        run = await db.get_latest_run()
    else:
        run = await db.get_crawl_run(run_id)

    if run is None:
        console.print("[red]No crawl runs found.[/red]")
        raise typer.Exit(1)

    pending = await db.get_pending_snapshots(run.id, limit=limit,
                                              include_failed=retry_failed)
    if not pending:
        console.print("[green]No pending snapshots — all done![/green]")
        return

    status_label = "pending + failed" if retry_failed else "pending"
    console.print(f"[bold]Resuming Run #{run.id}[/bold]: {len(pending)} {status_label} snapshots")

    dispatcher = ParserDispatcher(prefer_json=cfg.parsers.twitter.prefer_json)
    fetched_count = 0
    failed_count = 0
    tweet_count = 0
    lock = asyncio.Lock()

    async def on_result(snapshot: Snapshot, html: str | None) -> None:
        nonlocal fetched_count, failed_count, tweet_count
        if html is None:
            async with lock:
                failed_count += 1
            await db.update_snapshot_status(snapshot.id, snapshot.fetch_status,
                                             snapshot.last_error)
            return

        await db.update_snapshot_status(snapshot.id, "fetched")
        async with lock:
            fetched_count += 1

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
                    if media_dir:
                        for m in result.media:
                            local_path = await download_media(
                                client, m.url, snapshot.timestamp,
                                str(media_dir), cfg.fetcher.user_agent,
                                timeout=cfg.fetcher.timeout,
                                wayback_url=m.wayback_url,
                            )
                            if local_path and m.id:
                                await db.update_media_download(m.id, local_path, m.wayback_url)

    async with httpx.AsyncClient(
        timeout=cfg.fetcher.timeout,
        headers={"User-Agent": cfg.fetcher.user_agent},
        follow_redirects=True,
    ) as client:
        fetcher = SnapshotFetcher(cfg.fetcher, client)
        await fetcher.fetch_many(pending, on_result=on_result)

    counts = await db.count_snapshots_by_status(run.id)
    run.fetched_snapshots = counts.get("fetched", 0) + counts.get("skipped", 0)
    run.failed_snapshots = counts.get("failed", 0)
    run.tweets_extracted = await db.get_tweet_count(run.id)
    run.finished_at = now_iso()
    await db.update_crawl_run(run)

    console.print(f"\n[bold green]Resume complete.[/bold green] "
                  f"Fetched: {fetched_count}, Failed: {failed_count}, Tweets: {tweet_count}")


# ═══════════════════════════════════════════════════════════════════════
#  status
# ═══════════════════════════════════════════════════════════════════════

@app.command()
def status(
    run_id: Optional[int] = typer.Option(None, "--run-id", help="Show a specific run (default: latest)"),
    config_path: Optional[Path] = typer.Option(None, "--config", exists=True),
) -> None:
    """Show statistics for a crawl run."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(_status_async(run_id, config_path))


async def _status_async(run_id: int | None, config_path: Path | None) -> None:
    db = await _get_storage(config_path)

    if run_id is None:
        run = await db.get_latest_run()
    else:
        run = await db.get_crawl_run(run_id)

    if run is None:
        console.print("[yellow]No crawl runs found.[/yellow]")
        return

    counts = await db.count_snapshots_by_status(run.id)

    table = Table(title=f"Crawl Run #{run.id}")
    table.add_column("Field", style="cyan")
    table.add_column("Value", style="green")

    table.add_row("URL Pattern", run.url_pattern)
    table.add_row("Started", run.started_at)
    table.add_row("Finished", run.finished_at or "In progress...")
    table.add_row("Total Snapshots", str(run.total_snapshots))
    for status_name, cnt in sorted(counts.items()):
        table.add_row(f"  └─ {status_name}", str(cnt))
    table.add_row("Tweets Extracted", str(run.tweets_extracted))

    console.print(table)


# ═══════════════════════════════════════════════════════════════════════
#  export
# ═══════════════════════════════════════════════════════════════════════

@app.command()
def export(
    run_id: Optional[int] = typer.Option(None, "--run-id", help="Export from a specific run (default: latest)"),
    fmt: str = typer.Option("json", "--format", "-f", help="Output format: json or csv"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output file path (default: stdout)"),
    include_media: bool = typer.Option(False, "--include-media", help="Include media URLs"),
    config_path: Optional[Path] = typer.Option(None, "--config", exists=True),
) -> None:
    """Export extracted tweets to JSON or CSV."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(_export_async(run_id, fmt, output, include_media, config_path))


async def _export_async(
    run_id: int | None, fmt: str, output: Path | None,
    include_media: bool, config_path: Path | None,
) -> None:
    db = await _get_storage(config_path)

    if run_id is None:
        run = await db.get_latest_run()
    else:
        run = await db.get_crawl_run(run_id)

    if run is None:
        console.print("[yellow]No crawl runs found.[/yellow]")
        return

    tweets = await db.export_tweets(run.id)
    if not tweets:
        console.print("[yellow]No tweets to export.[/yellow]")
        return

    media_items = await db.export_media(run.id) if include_media else []

    if fmt == "csv":
        output_str = _format_csv(tweets, media_items)
    else:
        output_str = json_mod.dumps(tweets, indent=2, ensure_ascii=False, default=str)

    if output:
        Path(output).write_text(output_str, encoding="utf-8")
        console.print(f"[green]Exported {len(tweets)} tweets to {output}[/green]")
    else:
        console.print(output_str)


def _format_csv(tweets: list[dict], media: list[dict]) -> str:
    if not tweets:
        return ""

    fieldnames = [
        "tweet_id", "author_handle", "author_display", "text", "timestamp",
        "reply_count", "retweet_count", "like_count", "quote_count",
        "is_retweet", "is_reply", "parsed_with", "snapshot_timestamp", "original_url",
    ]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(tweets)
    return output.getvalue()


# ═══════════════════════════════════════════════════════════════════════
#  download-media
# ═══════════════════════════════════════════════════════════════════════

@app.command(name="download-media")
def download_media_cmd(
    run_id: Optional[int] = typer.Option(None, "--run-id", help="Download media for a specific run (default: latest)"),
    media_dir: Path = typer.Option("./media", "--media-dir", "-d", help="Output directory for media files"),
    concurrency: int = typer.Option(3, "--concurrency", "-c", help="Max concurrent downloads"),
    config_path: Optional[Path] = typer.Option(None, "--config", exists=True),
) -> None:
    """Download undownloaded media files from the Wayback Machine."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(_download_media_async(run_id, media_dir, concurrency, config_path))


async def _download_media_async(
    run_id: int | None, media_dir: Path, concurrency: int, config_path: Path | None,
) -> None:
    cfg = _get_config(config_path)
    db = await _get_storage(config_path)
    setup_logging(cfg.logging.level, cfg.logging.file, cfg.logging.format)

    if run_id is None:
        run = await db.get_latest_run()
    else:
        run = await db.get_crawl_run(run_id)

    if run is None:
        console.print("[red]No crawl runs found.[/red]")
        raise typer.Exit(1)

    media_items = await db.get_undownloaded_media(run.id)
    if not media_items:
        console.print("[green]All media already downloaded.[/green]")
        return

    console.print(f"Downloading {len(media_items)} media files to {media_dir}/")
    media_dir.mkdir(parents=True, exist_ok=True)

    # Get snapshot timestamps for constructing Wayback URLs
    snapshots_map: dict[int, str] = {}
    snapshots = await db.conn.execute(
        """SELECT t.id as tweet_id, s.timestamp
           FROM tweets t JOIN snapshots s ON t.snapshot_id = s.id
           WHERE s.crawl_run_id = ?""",
        (run.id,),
    )
    async for row in snapshots:
        snapshots_map[row["tweet_id"]] = row["timestamp"]

    sem = asyncio.Semaphore(concurrency)
    downloaded = 0
    failed = 0
    lock = asyncio.Lock()

    async def _download_one(m: dict) -> None:
        nonlocal downloaded, failed
        async with sem:
            timestamp = snapshots_map.get(m["tweet_id"], "00000000000000")
            async with httpx.AsyncClient(
                timeout=cfg.fetcher.timeout,
                headers={"User-Agent": cfg.fetcher.user_agent},
            ) as client:
                path = await download_media(
                    client, m["url"], timestamp,
                    str(media_dir), cfg.fetcher.user_agent,
                    timeout=cfg.fetcher.timeout,
                    wayback_url=m.get("wayback_url"),
                )
                if path:
                    await db.update_media_download(m["id"], path, m.get("wayback_url"))
                    async with lock:
                        downloaded += 1
                else:
                    async with lock:
                        failed += 1

    tasks = [asyncio.create_task(_download_one(m)) for m in media_items]
    await asyncio.gather(*tasks)

    console.print(f"[bold green]Download complete.[/bold green] "
                  f"Downloaded: {downloaded}, Failed: {failed}")


# ═══════════════════════════════════════════════════════════════════════
#  list-runs
# ═══════════════════════════════════════════════════════════════════════

@app.command(name="list-runs")
def list_runs(
    limit: int = typer.Option(20, "--limit", "-n", help="Max runs to show"),
    config_path: Optional[Path] = typer.Option(None, "--config", exists=True),
) -> None:
    """List all crawl runs."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(_list_runs_async(limit, config_path))


async def _list_runs_async(limit: int, config_path: Path | None) -> None:
    db = await _get_storage(config_path)
    runs = await db.list_runs(limit)

    if not runs:
        console.print("[yellow]No crawl runs yet.[/yellow]")
        return

    table = Table(title="Crawl Runs")
    table.add_column("ID", style="cyan")
    table.add_column("URL Pattern")
    table.add_column("Total")
    table.add_column("Fetched")
    table.add_column("Failed")
    table.add_column("Tweets")
    table.add_column("Started")

    for run in runs:
        table.add_row(
            str(run.id), run.url_pattern,
            str(run.total_snapshots), str(run.fetched_snapshots),
            str(run.failed_snapshots), str(run.tweets_extracted),
            run.started_at[:19] if run.started_at else "-",
        )

    console.print(table)


# ═══════════════════════════════════════════════════════════════════════
#  version
# ═══════════════════════════════════════════════════════════════════════

@app.command()
def version() -> None:
    """Show version."""
    console.print(f"wayback-crawler v{__version__}")


# ═══════════════════════════════════════════════════════════════════════
#  main entry point for console_scripts
# ═══════════════════════════════════════════════════════════════════════

def main() -> None:
    app()


if __name__ == "__main__":
    main()
