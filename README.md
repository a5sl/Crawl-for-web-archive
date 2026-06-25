# Wayback Crawler

A Python tool for crawling the [Wayback Machine](https://web.archive.org/) to extract structured data from archived web pages. Currently supports extracting tweets from archived Twitter/X pages.

## Features

- **CDX API integration** — query the Wayback Machine's CDX index to discover available snapshots
- **Intelligent parsing** — extracts structured tweet data (text, author, timestamp, engagement counts) from both classic Twitter (pre-2017) and modern Twitter/X (React-based) archived pages
- **Dual-layer extraction** — prefers `__INITIAL_STATE__` JSON when available, falls back to HTML scraping
- **Async concurrency** — fetches and parses multiple snapshots concurrently with configurable rate limiting
- **Resume capability** — interrupted crawls can be resumed without re-fetching completed snapshots
- **SQLite storage** — all data stored in a single portable database file
- **Multiple export formats** — export extracted tweets as JSON or CSV

## Quickstart
Edit `TARGET_HANDLE` in crawl.py to the twitter user name, whose archive you want to download. Then
```
python crawl.py
```
Or you want to try
```bash
# Install
pip install -e ".[dev]"

# Generate config
wayback-crawler init

# Crawl a Twitter user's archived tweets
wayback-crawler crawl "twitter.com/xxxx/*" --limit 100

# Check progress
wayback-crawler status

# Resume if interrupted
wayback-crawler resume

# Export results
wayback-crawler export --format json --output tweets.json
```

## How It Works

1. **Query** — Queries the CDX API (`https://web.archive.org/cdx/search/cdx`) with a URL pattern and stores the list of unique snapshots
2. **Fetch** — Downloads archived HTML from `https://web.archive.org/web/{timestamp}id_/{url}` with rate limiting and retry logic
3. **Parse** — Routes HTML to the correct parser (old or new Twitter), extracts structured tweet data
4. **Store** — Persists tweets, media references, and crawl metadata to SQLite
5. **Export** — Exports extracted data as JSON or CSV

## Project Structure

```
src/wayback_crawler/
├── main.py         # CLI entry point (typer)
├── config.py       # YAML config loading
├── models.py       # Data models (Snapshot, Tweet, Media, CrawlRun)
├── cdx.py          # CDX API client
├── fetcher.py      # Async snapshot fetcher with rate limiting
├── parser.py       # Parser dispatcher
├── parsers/
│   ├── base.py         # Abstract parser interface
│   ├── twitter_old.py  # Pre-2017 classic Twitter
│   └── twitter_new.py  # Post-2017 Twitter/X
├── storage.py      # SQLite repository (async via aiosqlite)
└── utils.py        # Logging, URL helpers, jitter
```

## License

Research project — use responsibly and respect web.archive.org's rate limits.
