"""Parser dispatcher — routes archived content to the correct site-specific parser."""

from __future__ import annotations

import logging

from wayback_crawler.models import ParseResult, Snapshot
from wayback_crawler.parsers.base import BaseParser
from wayback_crawler.parsers.twitter_json import TwitterJsonParser
from wayback_crawler.parsers.twitter_new import TwitterNewParser
from wayback_crawler.parsers.twitter_old import TwitterOldParser
from wayback_crawler.utils import normalize_domain

logger = logging.getLogger(__name__)


class ParserDispatcher:
    """Routes a snapshot's content to the correct domain-specific parser.

    Parsers are registered by domain (e.g. ``twitter.com``) in a list.
    The dispatcher tries each parser's ``can_parse()`` in registration
    order until one accepts the content.
    """

    def __init__(self, prefer_json: bool = True) -> None:
        self._registry: dict[str, list[BaseParser]] = {}

        # Registration order matters — first parser that can_parse() wins.
        # With id_ flag, most snapshots return JSON (Twitter API responses).
        # JSON parser handles these. HTML parsers are fallback.
        self.register(TwitterJsonParser())
        self.register(TwitterNewParser())
        self.register(TwitterOldParser())

    def register(self, parser: BaseParser) -> None:
        """Register a parser for its declared domain."""
        domain = parser.domain
        if domain not in self._registry:
            self._registry[domain] = []
        self._registry[domain].append(parser)
        logger.debug("Registered parser %s for domain %s",
                      type(parser).__name__, domain)

    def parse(self, snapshot: Snapshot, html: str) -> ParseResult:
        """Parse a snapshot, trying each registered parser for the domain.

        Iterates through parsers in registration order. The first parser
        whose ``can_parse()`` returns True handles the content.
        """
        domain = normalize_domain(snapshot.original_url)
        parsers = self._registry.get(domain, [])

        if not parsers:
            logger.debug("No parser registered for domain '%s' (%s)",
                          domain, snapshot.original_url)
            result = ParseResult()
            result.errors.append(f"No parser for domain '{domain}'")
            return result

        for parser in parsers:
            if parser.can_parse(html, snapshot.original_url):
                logger.info("Parsing %s with %s", snapshot.original_url[:80],
                             type(parser).__name__)
                return parser.parse(html, snapshot.original_url, snapshot.timestamp)

        logger.debug("All parsers rejected snapshot %s", snapshot.original_url[:80])
        result = ParseResult()
        result.errors.append(
            f"No parser for domain '{domain}' could handle this content"
        )
        return result
