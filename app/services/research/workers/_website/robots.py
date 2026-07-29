"""robots.txt fetching and fail-closed rule evaluation."""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from . import USER_AGENT
from .fetcher import HttpFetcher

log = logging.getLogger(__name__)


class RobotsInfo:
    """Parsed robots policy for one host.

    A confirmed 404/410 means there is no policy and permits crawling. A
    network, TLS, redirect, server, content-type, or parsing failure denies
    crawling because the operator asked the worker to honour robots.txt rather
    than guess what it might have said.
    """

    def __init__(
        self,
        parser: Optional[RobotFileParser],
        sitemaps: list[str],
        fetched: bool,
        crawl_delay: Optional[float] = None,
        *,
        default_allowed: bool = False,
    ) -> None:
        self._parser = parser
        self._default_allowed = default_allowed
        self.sitemaps = sitemaps
        self.fetched = fetched
        self.crawl_delay = crawl_delay

    def can_fetch(self, url: str) -> bool:
        if self._parser is None:
            return self._default_allowed
        try:
            return self._parser.can_fetch(USER_AGENT, url)
        except Exception:  # pragma: no cover - defensive
            return False

    @classmethod
    def from_text(cls, text: str, base_url: str = "") -> "RobotsInfo":
        del base_url
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
            configured = parser.crawl_delay(USER_AGENT)
            if configured is not None:
                delay = float(configured)
        except Exception:  # pragma: no cover
            pass
        return cls(parser, sitemaps, fetched=True, crawl_delay=delay)

    @classmethod
    def allow_all(cls) -> "RobotsInfo":
        return cls(None, [], fetched=True, default_allowed=True)

    @classmethod
    def deny_all(cls) -> "RobotsInfo":
        return cls(None, [], fetched=False, default_allowed=False)


def fetch_robots(fetcher: HttpFetcher, start_url: str) -> RobotsInfo:
    parts = urlsplit(start_url)
    robots_url = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
    result = fetcher.fetch(robots_url, expect_html=False)
    if result.status_code in {404, 410}:
        log.info("no robots.txt for %s - allowing crawl", parts.netloc)
        return RobotsInfo.allow_all()
    if not result.ok or not result.html:
        log.warning(
            "robots.txt unavailable for %s (%s) - refusing crawl",
            parts.netloc,
            result.error or f"HTTP {result.status_code}",
        )
        return RobotsInfo.deny_all()
    try:
        return RobotsInfo.from_text(result.html, base_url=start_url)
    except Exception as exc:
        log.warning("invalid robots.txt for %s (%s) - refusing crawl", parts.netloc, exc)
        return RobotsInfo.deny_all()
