"""Abstract base parser for archived web pages."""

from __future__ import annotations

from abc import ABC, abstractmethod

from wayback_crawler.models import ParseResult


class BaseParser(ABC):
    """Parser for a specific website's archived HTML.

    Subclasses implement detection logic (``can_parse``) and
    extraction logic (``parse``).
    """

    @abstractmethod
    def can_parse(self, html: str, url: str) -> bool:
        """Return True if this parser can handle the given HTML page."""

    @abstractmethod
    def parse(self, html: str, url: str, snapshot_timestamp: str) -> ParseResult:
        """Extract structured data from the archived HTML."""

    @property
    @abstractmethod
    def domain(self) -> str:
        """The domain this parser handles (e.g. 'twitter.com')."""
