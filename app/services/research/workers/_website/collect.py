"""In-memory crawl orchestration for the vendored website collector.

Adapted from the prototype's ``pipeline._run``. The prototype wrote a job
row, a directory of JSON reports, and an optional LLM interpretation. This
repository owns job state and persistence, so this module returns the
collected facts instead and writes nothing.

Deliberate omissions relative to the prototype:

* no Playwright fallback -- a headless browser is not an approved
  dependency, and a JS-only site is reported as insufficient evidence
  rather than silently retried through one;
* no interpreter stage -- AI synthesis is MVP-02, behind the untrusted
  evidence boundary in #181;
* no filesystem output.

``robots.txt`` is always honoured. A page the site disallows is skipped
and recorded, never fetched.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

from .classification import classify_page
from .config import AppConfig
from .discovery import build_candidates, canonicalize_url, prefer_host
from .domain import NormalizedDomain, normalize_domain, same_site
from .extraction import extract_page
from .facts import extract_facts
from .fetcher import HttpFetcher
from .models import Fact, PageExtract, utcnow_iso
from .robots import RobotsInfo, fetch_robots
from .sitemap import collect_sitemap_urls

log = logging.getLogger(__name__)

PARKED_HINTS = (
    "domain is for sale",
    "buy this domain",
    "parked free",
    "domain parking",
    "this domain may be for sale",
    "coming soon",
    "under construction",
)
SOCIAL_REDIRECT_HOSTS = (
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "x.com",
    "twitter.com",
)

# A crawl that reaches the site but reads almost nothing is not a failure --
# it is a truthful "insufficient evidence" outcome.
MIN_FACTS_FOR_SUFFICIENCY = 3


@dataclass
class PageRecord:
    """What was read from one page, for the preserved raw payload."""

    url: str
    title: str | None
    page_type: str
    retrieval_method: str
    retrieved_at: str
    text_length: int


@dataclass
class CollectionOutcome:
    """The complete result of one website collection run.

    ``unreachable`` distinguishes "we could not read the site at all" from
    "we read the site and it says little". Only the former is a failure.
    """

    domain: str
    facts: list[Fact] = field(default_factory=list)
    pages: list[PageRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    unreachable: bool = False
    unreachable_reason: str | None = None
    started_at: str = ""
    completed_at: str = ""

    @property
    def sufficient(self) -> bool:
        return not self.unreachable and len(self.facts) >= MIN_FACTS_FOR_SUFFICIENCY


def _record_error(outcome: CollectionOutcome, url: str, stage: str, message: str) -> None:
    outcome.errors.append({"url": url, "stage": stage, "error": message, "at": utcnow_iso()})


def _looks_parked(page: PageExtract) -> bool:
    haystack = f"{page.title or ''} {page.clean_text[:2000]}".lower()
    return any(hint in haystack for hint in PARKED_HINTS)


def _fail(outcome: CollectionOutcome, url: str, stage: str, reason: str) -> CollectionOutcome:
    _record_error(outcome, url, stage, reason)
    outcome.unreachable = True
    outcome.unreachable_reason = reason
    outcome.completed_at = utcnow_iso()
    return outcome


def collect(
    domain: str,
    config: AppConfig,
    *,
    fetcher: HttpFetcher | None = None,
) -> CollectionOutcome:
    """Crawl one company website and return sourced facts.

    ``fetcher`` exists as a seam for tests: injecting a fake keeps the
    suite entirely offline, which is what #173 requires of CI.
    """

    normalized = normalize_domain(domain)
    outcome = CollectionOutcome(domain=normalized.registered_domain, started_at=utcnow_iso())

    owns_fetcher = fetcher is None
    active = fetcher if fetcher is not None else HttpFetcher(config.crawler)
    try:
        return _collect(normalized, config, active, outcome)
    finally:
        if owns_fetcher:
            active.close()


def _collect(
    nd: NormalizedDomain,
    cfg: AppConfig,
    fetcher: HttpFetcher,
    outcome: CollectionOutcome,
) -> CollectionOutcome:
    delay = cfg.crawler.delay_between_requests_seconds
    include_subdomains = cfg.discovery.include_subdomains

    # 1. Homepage, trying the apex/www/http variants the prototype tried.
    homepage = None
    tried: list[str] = []
    for start in (nd.start_url, f"https://www.{nd.host}/", f"http://{nd.host}/"):
        if start in tried:
            continue
        tried.append(start)
        homepage = fetcher.fetch(start)
        if homepage.ok:
            break
        time.sleep(delay)

    if homepage is None or not homepage.ok:
        reason = (homepage.error if homepage else None) or "no response"
        return _fail(outcome, nd.start_url, "homepage", f"homepage unreachable: {reason}")

    if not same_site(homepage.final_url, nd, include_subdomains):
        host = (urlsplit(homepage.final_url).hostname or "").lower()
        reason = (
            "redirects to a social media profile"
            if any(h in host for h in SOCIAL_REDIRECT_HOSTS)
            else f"redirects off-domain to {host}"
        )
        return _fail(outcome, nd.start_url, "homepage", reason)

    # 2. robots.txt is authoritative over everything below.
    robots = RobotsInfo.allow_all()
    if cfg.discovery.use_robots_txt:
        robots = fetch_robots(fetcher, homepage.final_url)
        time.sleep(min(delay, 0.5))
    if robots.crawl_delay and robots.crawl_delay > delay:
        delay = min(robots.crawl_delay, 10.0)

    sitemap_urls: list[str] = []
    if cfg.discovery.use_sitemap:
        sources = list(robots.sitemaps) or [urljoin(homepage.final_url, "/sitemap.xml")]
        sitemap_urls = [
            entry.loc
            for entry in collect_sitemap_urls(
                fetcher, sources, max_urls=cfg.discovery.max_sitemap_urls
            )
        ]

    # 3. Extract the homepage and rank what to read next.
    homepage_page = extract_page(homepage, nd, include_subdomains)
    homepage_page.classification = classify_page(
        homepage.final_url,
        homepage_page.title,
        [h["text"] for h in homepage_page.headings],
    )

    if _looks_parked(homepage_page):
        return _fail(outcome, nd.start_url, "homepage", "site appears parked or a placeholder")

    candidates = build_candidates(
        nd,
        homepage_page.internal_links,
        sitemap_urls,
        include_subdomains=include_subdomains,
        max_candidates=cfg.discovery.max_candidates,
    )

    # 4. Crawl, bounded by max_pages, max_depth and the politeness delay.
    parts = urlsplit(homepage.final_url)
    preferred_netloc = parts.netloc
    preferred_scheme = parts.scheme or "https"

    pages: list[PageExtract] = [homepage_page]
    seen: set[str] = {canonicalize_url(homepage.final_url)}
    processed = 0

    for cand in candidates:
        if processed + 1 >= cfg.crawler.max_pages:
            break
        if cand.depth > cfg.crawler.max_depth or cand.skip_reason:
            continue
        canon = canonicalize_url(cand.url)
        if canon in seen:
            continue

        fetch_url = prefer_host(cand.url, preferred_netloc, preferred_scheme)
        if not robots.can_fetch(fetch_url):
            outcome.warnings.append(f"skipped {fetch_url}: disallowed by robots.txt")
            continue

        time.sleep(delay)
        result = fetcher.fetch(fetch_url)
        if not result.ok:
            message = result.error or "fetch failed"
            _record_error(outcome, fetch_url, "fetch", message)
            continue

        final_canon = canonicalize_url(result.final_url)
        if final_canon in seen:
            continue
        if not same_site(result.final_url, nd, include_subdomains):
            continue

        try:
            page = extract_page(result, nd, include_subdomains)
            page.classification = classify_page(
                result.final_url,
                page.title,
                [h["text"] for h in page.headings],
                cand.anchor_text,
            )
        except Exception as exc:  # a bad page must never end the crawl
            _record_error(outcome, fetch_url, "extract", f"{type(exc).__name__}: {exc}")
            continue

        if len(page.clean_text) < cfg.crawler.min_useful_text_chars and not page.json_ld:
            outcome.warnings.append(f"no useful content extracted from {fetch_url}")
            continue

        seen.add(final_canon)
        seen.add(canon)
        pages.append(page)
        processed += 1

    # 5. Deterministic fact extraction over everything that was read.
    outcome.facts = list(extract_facts(pages))
    outcome.pages = [
        PageRecord(
            url=p.final_url,
            title=p.title,
            page_type=p.classification.page_type if p.classification else "other",
            retrieval_method=p.retrieval_method,
            retrieved_at=p.retrieved_at,
            text_length=len(p.clean_text),
        )
        for p in pages
    ]
    if outcome.errors:
        outcome.warnings.append(
            f"{len(outcome.errors)} page(s) could not be read; "
            "facts below come only from the pages that were read"
        )
    if not outcome.sufficient:
        outcome.warnings.append(
            f"only {len(outcome.facts)} fact(s) extracted from "
            f"{len(pages)} page(s) - insufficient evidence"
        )
    outcome.completed_at = utcnow_iso()
    return outcome
