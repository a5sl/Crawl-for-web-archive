# Development Guide

## Setup

```bash
cd F:/Pics/crawl
pip install -e ".[dev]"
```

This installs the package in editable mode with all dev dependencies (pytest, pytest-asyncio, pytest-httpx, pytest-cov).

## Running Tests

```bash
# All tests
pytest tests/ -v

# Skip integration tests
pytest tests/ -v -m "not integration"

# Only integration tests
pytest tests/ -v -m integration

# With coverage
pytest tests/ --cov=wayback_crawler --cov-report=html
```

## Project Structure

```
src/wayback_crawler/
├── __init__.py      # Package version
├── main.py          # CLI entry point (typer + rich)
├── config.py        # YAML config parsing + dataclasses
├── models.py        # Pure @dataclass models
├── cdx.py           # CDX API async client
├── fetcher.py       # Snapshot HTTP fetcher (async, rate-limited)
├── parser.py        # Parser dispatcher (domain → parser)
├── parsers/
│   ├── __init__.py
│   ├── base.py      # Abstract BaseParser
│   ├── twitter_old.py   # Pre-2017 classic Twitter
│   └── twitter_new.py   # Post-2017 React Twitter/X
├── storage.py       # SQLite repo (aiosqlite, async)
└── utils.py         # Logging, URL helpers, jitter

tests/
├── conftest.py              # Shared fixtures
├── test_config.py           # Config loading tests
├── test_cdx.py              # CDX client tests (mocked HTTP)
├── test_fetcher.py          # Fetcher tests (mocked HTTP)
├── test_storage.py          # Storage tests (in-memory SQLite)
├── test_parser_twitter_old.py  # Old Twitter parser tests
├── test_parser_twitter_new.py  # New Twitter parser tests
├── test_integration.py      # Full pipeline tests
└── fixtures/                # Sample HTML and JSON
    ├── cdx_sample.json
    ├── twitter_old_sample.html
    ├── twitter_old_timeline.html
    ├── twitter_new_sample.html
    ├── twitter_new_initial_state.html
    ├── twitter_deleted.html
    └── twitter_login_wall.html
```

## Key Design Decisions

### Async throughout
All I/O (HTTP, SQLite) is async via `httpx.AsyncClient` + `aiosqlite`. The CLI layer runs sync via `typer` and wraps async entry points with `asyncio.run()`.

### No ORM
Raw SQL via `aiosqlite`. The schema is small and stable. An ORM would add complexity without benefit.

### Parser registry pattern
`ParserDispatcher` holds a `dict[str, BaseParser]` keyed by domain. Adding a new site parser (e.g., Reddit, Instagram) means:
1. Create a new file in `parsers/`
2. Subclass `BaseParser`
3. Register in `parsers/__init__.py` and `parser.py`

### Resume by snapshot status
Instead of tracking position, we query `WHERE fetch_status = 'pending'`. This naturally handles crashes and interruptions.

### Separate fetch and parse
HTML is never stored — only extracted structured data is. The `--no-parse` flag allows fetching without parsing, useful for debugging.

## Adding a New Parser

1. Create `parsers/reddit.py` (example):

```python
from wayback_crawler.parsers.base import BaseParser
from wayback_crawler.models import ParseResult, Tweet

class RedditParser(BaseParser):
    @property
    def domain(self) -> str:
        return "reddit.com"

    def can_parse(self, html: str, url: str) -> bool:
        return "reddit" in html.lower()

    def parse(self, html: str, url: str, snapshot_timestamp: str) -> ParseResult:
        result = ParseResult()
        # extract data from HTML...
        return result
```

2. Register in `parser.py`:

```python
from wayback_crawler.parsers.reddit import RedditParser
# In ParserDispatcher.__init__:
self.register(RedditParser())
```

3. Add config toggle in `config.py` if desired.

## Dependency Graph

```
models.py         ← no internal deps
config.py         ← no internal deps (uses models for types)
utils.py          ← no internal deps
storage.py        ← models
cdx.py            ← config, utils
fetcher.py        ← config, models, utils
parsers/base.py   ← models
parsers/*.py      ← models, base
parser.py         ← models, parsers/*, utils
main.py           ← everything above
```
