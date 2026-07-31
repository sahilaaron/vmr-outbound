"""XML sitemap and sitemap-index parsing (bounded, incremental).

Parsing is incremental. A sitemap that the fetcher truncated at its
response-size cap, or that is malformed at the tail, still yields every
complete <url>/<sitemap> entry that arrived before the fault.

This replaces an all-or-nothing ``ElementTree.fromstring``, which discarded
the entire document on a single bad byte. On a large sitemap that meant tens
of thousands of good entries were thrown away and the crawl silently fell
back to homepage links alone -- with nothing in the run's evidence to say so,
because the parse error was logged and then swallowed.

Two properties matter as much as the recovery itself. A document that did not
parse to its closing tag is never reported as whole: callers get
``complete=False`` and a warning naming what was kept. And the parser stops as
soon as the caller's URL bound is reached, so an enormous sitemap costs the
bound rather than the document.
"""
from __future__ import annotations

import gzip
import logging
from dataclasses import dataclass, field
from typing import Optional
from xml.etree import ElementTree

from .fetcher import HttpFetcher

log = logging.getLogger(__name__)

_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"

# Only these are entries; anything else at document level is ignored.
_ENTRY_TAGS = frozenset({"url", "sitemap"})

# Feed size. Small enough that the URL bound stops parsing promptly on a huge
# document, large enough that chunking costs nothing on an ordinary one.
_FEED_CHUNK_BYTES = 65536


@dataclass
class SitemapURL:
    loc: str
    lastmod: Optional[str] = None


@dataclass
class SitemapParseResult:
    """What one sitemap document yielded, and whether it was whole.

    ``complete`` is False when the XML ended prematurely -- truncated body or
    malformed tail. ``limit_reached`` is not a fault: it means parsing stopped
    on the caller's bound with the document possibly still having more.
    """

    urls: list[SitemapURL] = field(default_factory=list)
    children: list[str] = field(default_factory=list)
    complete: bool = True
    limit_reached: bool = False
    warning: Optional[str] = None


@dataclass
class SitemapCollection:
    """Entries gathered across every sitemap fetched for one site."""

    entries: list[SitemapURL] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    partial: bool = False


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _entry_loc(elem: ElementTree.Element) -> tuple[Optional[str], Optional[str]]:
    loc, lastmod = None, None
    for child in elem:
        ctag = _strip_ns(child.tag)
        if ctag == "loc" and child.text:
            loc = child.text.strip()
        elif ctag == "lastmod" and child.text:
            lastmod = child.text.strip()
    return loc, lastmod


def parse_sitemap_xml(text: str, max_urls: Optional[int] = None) -> SitemapParseResult:
    """Parse a sitemap or sitemap index incrementally, keeping what completed.

    Tolerates namespace variants and either document type. Returns in document
    order. Stops as soon as ``max_urls`` page URLs have been collected.
    """
    result = SitemapParseResult()
    parser = ElementTree.XMLPullParser(events=("start", "end"))
    root: Optional[ElementTree.Element] = None
    root_tag: Optional[str] = None

    def harvest() -> Optional[ElementTree.ParseError]:
        """Drain queued events. Returns the fault that ended the document.

        ``read_events`` re-raises a parse fault in stream position, so events
        for elements that completed before it are yielded first and survive.
        """
        nonlocal root, root_tag
        try:
            for event, elem in parser.read_events():
                if event == "start":
                    if root is None:
                        root = elem
                        root_tag = _strip_ns(elem.tag)
                    continue
                tag = _strip_ns(elem.tag)
                if tag not in _ENTRY_TAGS:
                    continue
                loc, lastmod = _entry_loc(elem)
                # Release this entry and detach it from the root, so parsing a
                # 60k-entry sitemap does not hold 60k elements in memory.
                elem.clear()
                if root is not None and len(root):
                    del root[:]
                if not loc:
                    continue
                if root_tag == "sitemapindex" or tag == "sitemap":
                    result.children.append(loc)
                else:
                    result.urls.append(SitemapURL(loc=loc, lastmod=lastmod))
                    if max_urls is not None and len(result.urls) >= max_urls:
                        result.limit_reached = True
                        return None
        except ElementTree.ParseError as exc:
            return exc
        return None

    data = text.encode("utf-8", errors="replace")
    fault: Optional[ElementTree.ParseError] = None
    for offset in range(0, len(data), _FEED_CHUNK_BYTES):
        try:
            parser.feed(data[offset : offset + _FEED_CHUNK_BYTES])
        except ElementTree.ParseError as exc:
            fault = exc
            break
        fault = harvest()
        if fault is not None or result.limit_reached:
            break

    if fault is None and not result.limit_reached:
        # A body cut mid-element parses cleanly until the end, where the
        # unclosed tag finally surfaces. This is where truncation is caught.
        try:
            parser.close()
        except ElementTree.ParseError as exc:
            fault = exc
        drained = harvest()
        fault = fault or drained

    if fault is not None:
        result.complete = False
        result.warning = (
            f"sitemap XML ended prematurely ({fault}); kept {len(result.urls)} complete "
            f"url entr{'y' if len(result.urls) == 1 else 'ies'} and "
            f"{len(result.children)} child sitemap(s)"
        )
        log.warning("partial sitemap: %s", result.warning)
    return result


def collect_sitemap_urls(
    fetcher: HttpFetcher,
    sitemap_urls: list[str],
    max_urls: int = 500,
    max_sitemaps: int = 10,
) -> SitemapCollection:
    """Fetch sitemaps (and one level of indexes) up to bounded limits.

    Returns the entries alongside the reason any document was incomplete, so a
    caller can record that the crawl worked from a partial list rather than
    presenting it as the whole site.
    """
    seen_sitemaps: set[str] = set()
    queue = list(dict.fromkeys(sitemap_urls))
    collection = SitemapCollection()
    fetched = 0
    while queue and fetched < max_sitemaps and len(collection.entries) < max_urls:
        sm_url = queue.pop(0)
        if sm_url in seen_sitemaps:
            continue
        seen_sitemaps.add(sm_url)
        fetched += 1
        result = fetcher.fetch(sm_url, expect_html=False)
        text = result.html
        if not result.ok or not text:
            continue
        if text.lstrip()[:1] not in ("<",):
            # try gz transparently (some servers serve .gz without CE header)
            try:
                text = gzip.decompress(text.encode("latin-1")).decode("utf-8", "replace")
            except Exception:
                continue
        remaining = max_urls - len(collection.entries)
        parsed = parse_sitemap_xml(text, max_urls=remaining)
        collection.entries.extend(parsed.urls)
        # Truncation and a malformed tail both end the document early, but they
        # are different facts about the site and are reported as such.
        if result.truncated:
            collection.partial = True
            collection.warnings.append(
                f"{sm_url}: response hit the {fetcher.max_bytes} byte cap and was cut short; "
                f"kept {len(parsed.urls)} complete entries"
            )
        elif not parsed.complete:
            collection.partial = True
            collection.warnings.append(
                f"{sm_url}: malformed XML tail; kept {len(parsed.urls)} complete entries"
            )
        for child in parsed.children:
            if child not in seen_sitemaps:
                queue.append(child)
    collection.entries = collection.entries[:max_urls]
    return collection
