"""robots.txt fetching and rule evaluation."""
from __future__ import annotations

import logging
from typing import Optional
from urllib.robotparser import RobotFileParser

from . import USER_AGENT
from .fetcher import HttpFetcher

log = logging.getLogger(__name__)


class RobotsInfo:
    """Parsed robots.txt for one host. Missing/unfetchable => allow all."""

    def __init__(self, parser: Optional[RobotFileParser], sitemaps: list[str],
                 fetched: bool, crawl_delay: Optional[float] = None) -> None:
        self._parser = parser
        self.sitemaps = sitemaps
        self.fetched = fetched
        self.crawl_delay = crawl_delay

    def can_fetch(self, url: str) -> bool:
        if self._parser is None:
            return True
        try:
            return self._parser.can_fetch(USER_AGENT, url)
        except Exception:  # pragma: no cover - defensive
            return True

    @classmethod
    def from_text(cls, text: str, base_url: str = "") -> "RobotsInfo":
        parser = RobotFileParser()
        lines = text.splitlines()
        parser.parse(lines)
        sitemaps = [
            line.split(":", 1)[1].strip()
            for line in lines
            if line.lower().startswith("sitemap:")
        ]
        delay = None
        try:
            d = parser.crawl_delay(USER_AGENT)
            if d is not None:
                delay = float(d)
        except Exception:  # pragma: no cover
            pass
        return cls(parser, sitemaps, fetched=True, crawl_delay=delay)

    @classmethod
    def allow_all(cls) -> "RobotsInfo":
        return cls(None, [], fetched=False)


def fetch_robots(fetcher: HttpFetcher, start_url: str) -> RobotsInfo:
    from urllib.parse import urlsplit, urlunsplit
    parts = urlsplit(start_url)
    robots_url = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
    result = fetcher.fetch(robots_url, expect_html=False)
    if not result.ok or not result.html:
        log.info("no robots.txt for %s (%s) - allowing all", parts.netloc, result.error)
        return RobotsInfo.allow_all()
    return RobotsInfo.from_text(result.html, base_url=start_url)
