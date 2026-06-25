# Architecture

## Overview

Wayback Crawler follows a pipeline architecture with six layers:

```
CLI (main.py)
  │
  ├── Config (config.py) ── YAML + defaults
  ├── Models (models.py) ── @dataclass definitions
  │
  ├── CDX Client (cdx.py) ──► CDX API
  │         │
  │         ▼ list of {timestamp, original_url}
  │   Storage (storage.py) ──► snapshots table
  │
  ├── Fetcher (fetcher.py) ──► Wayback pages
  │         │                    (async semaphore + rate limiter + backoff)
  │         ▼ HTML
  │   Parser (parser.py)
  │         │
  │         ├── TwitterOldParser ── BeautifulSoup
  │         └── TwitterNewParser ── JSON + BeautifulSoup
  │         │
  │         ▼ ParseResult {tweets, media}
  │   Storage (storage.py) ──► tweets + media tables
  │
  └── Export (main.py) ── JSON / CSV output
```

## Module Details

### config.py
Configuration management via YAML files. Merges file-based config with CLI overrides. Default values embedded as dataclass field defaults. Full config snapshot stored as JSON in each `crawl_run` for auditability.

### models.py
Pure `@dataclass` definitions with zero internal dependencies:
- `CrawlRun` — crawl session metadata
- `Snapshot` — a single Wayback Machine snapshot entry
- `Tweet` — extracted structured tweet
- `Media` — image/video reference from a tweet
- `ParseResult` — parser output container

### cdx.py
Async CDX API client. Constructs query URLs with parameters (url pattern, date range, limit, collapse field, match type). Supports both `query()` (returns list) and `query_iter()` (streaming generator). Handles retries on 5xx errors with exponential backoff.

### fetcher.py
Async snapshot HTML fetcher. Uses `asyncio.Semaphore` for concurrency control and a `RateLimiter` class ensuring minimum interval between requests. Implements exponential backoff retry with jitter. Detects Wayback Machine error pages (not-found, not-archived) and marks them as "skipped".

### parser.py / parsers/
Parser subsystem with domain-based routing:

**BaseParser** — abstract interface with `can_parse(html, url)` and `parse(html, url, snapshot_timestamp)`.

**TwitterOldParser** — handles classic Twitter HTML (2010–2017):
- Detects via `<div class="tweet">`, `<li class="js-stream-item">`, etc.
- Extracts via `data-*` attributes and CSS selectors
- Excludes pages with `<div id="react-root">`

**TwitterNewParser** — handles modern React-based Twitter/X (2017–present):
- Layer 1: searches `<script>` tags for `window.__INITIAL_STATE__` JSON blob
- Layer 2: falls back to HTML scraping with `data-testid` selectors
- Detects deleted tweets and login walls

**ParserDispatcher** — normalizes URL domain, looks up registered parser, validates via `can_parse()`, returns `ParseResult`.

### storage.py
Async SQLite repository using `aiosqlite`. Schema includes 4 tables with foreign keys, composite unique constraints, and indexes. All methods are async. Uses WAL mode for concurrent read/write. `INSERT OR IGNORE` prevents duplicates.

### main.py
CLI entry point using `typer` with `rich` for output formatting. 9 commands:

| Command | Purpose |
|---------|---------|
| `init` | Generate config file |
| `query` | Query CDX, store snapshots |
| `fetch` | Fetch and parse pending snapshots |
| `crawl` | Full pipeline (query + fetch) |
| `resume` | Resume incomplete run |
| `status` | Show run statistics |
| `export` | Export tweets as JSON/CSV |
| `list-runs` | List all crawl runs |
| `version` | Show version |

## Database Schema

4 tables with WAL mode and foreign keys:

- **crawl_runs** — one row per crawl session with config snapshot
- **snapshots** — CDX entries, indexed by (run_id, status) and (timestamp, original_url)
- **tweets** — extracted structured data with unique (snapshot_id, tweet_id) constraint
- **media** — image/video URLs linked to tweets

## Concurrency Model

Uses `asyncio.Semaphore` to limit concurrent fetches. A `RateLimiter` wraps each HTTP request with a minimum interval plus random jitter. This ensures polite behavior toward web.archive.org while maximizing throughput within configured limits.

## Error Handling

- **CDX errors**: retry up to `max_retries` on 5xx
- **Fetch errors**: retry on 429 (rate limit) and 5xx with exponential backoff; mark as "failed" after exhaustion
- **Parse errors**: caught per-snapshot, recorded in `ParseResult.errors`; never crash the pipeline
- **Deleted tweets**: detected by text patterns, return empty ParseResult
- **Login walls**: detected early, marked as "skipped"
