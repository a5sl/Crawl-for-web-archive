# User Guide

## Installation

```bash
cd F:/Pics/crawl
pip install -e ".[dev]"
```

## Configuration

Generate a default config file:

```bash
wayback-crawler init
```

This creates `config.example.yaml`. Rename to `config.yaml` and customize:

```yaml
database:
  path: "wayback_crawler.db"

cdx:
  rate_limit: 1.5       # seconds between CDX API calls

fetcher:
  concurrency: 5         # max parallel fetches
  rate_limit: 0.5        # seconds between page fetches
  max_retries: 3
  timeout: 30.0

parsers:
  twitter:
    enabled: true
    prefer_json: true    # extract from __INITIAL_STATE__ first

logging:
  level: "INFO"
```

## Basic Usage

### Query CDX (discover snapshots, no fetching)

```bash
wayback-crawler query "twitter.com/youzaimeimei/*" --limit 50
```

Options:
- `--from YYYYMMDD` / `--to YYYYMMDD` — date range filter
- `--limit N` — max CDX results
- `--output-format json` — JSON output to stdout

### Fetch and Parse

```bash
wayback-crawler fetch --limit 20 --concurrency 3
```

Options:
- `--run-id N` — target a specific crawl run
- `--no-parse` — fetch HTML but skip parsing (useful for debugging)

### Full Pipeline

```bash
wayback-crawler crawl "twitter.com/youzaimeimei/*" --limit 100 --concurrency 5
```

### Resume

If a crawl is interrupted:

```bash
wayback-crawler resume --retry-failed
```

### Check Status

```bash
wayback-crawler status
wayback-crawler status --run-id 3
```

### List All Runs

```bash
wayback-crawler list-runs
```

### Export Results

```bash
# JSON (default)
wayback-crawler export --output tweets.json

# CSV
wayback-crawler export --format csv --output tweets.csv

# Include media URLs
wayback-crawler export --include-media --output full_export.json
```

## URL Patterns

The URL pattern supports wildcards:

| Pattern | Matches |
|---------|---------|
| `twitter.com/user/*` | All URLs under twitter.com/user/ |
| `twitter.com/user` | Exact match for that URL |
| `*.twitter.com/*` | All subdomains and paths |

## Rate Limiting

The Wayback Machine does not publish official rate limits but appreciates polite crawling. Default settings:
- CDX queries: 1.5 seconds between requests
- Page fetches: 0.5 seconds between requests (with jitter)

Adjust these in `config.yaml` if you encounter rate limiting (HTTP 429 responses). The fetcher automatically retries 429 responses with exponential backoff.

## Database

All data is stored in a single SQLite file (default: `wayback_crawler.db`). You can query it directly:

```bash
sqlite3 wayback_crawler.db "SELECT text FROM tweets WHERE author_handle='@youzaimeimei' LIMIT 5"
```
