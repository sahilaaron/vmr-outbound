"""XML sitemap and sitemap-index parsing (bounded)."""
from __future__ import annotations

import gzip
import logging
from dataclasses import dataclass
from typing import Optional
from xml.etree import ElementTree

from .fetcher import HttpFetcher

log = logging.getLogger(__name__)

_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


@dataclass
class SitemapURL:
    loc: str
    lastmod: Optional[str] = None


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_sitemap_xml(text: str) -> tuple[list[SitemapURL], list[str]]:
    """Return (page_urls, child_sitemap_urls). Tolerates namespace variants."""
    urls: list[SitemapURL] = []
    children: list[str] = []
    try:
        root = ElementTree.fromstring(text.encode("utf-8", errors="replace"))
    except ElementTree.ParseError as exc:
        log.warning("sitemap parse error: %s", exc)
        return urls, children
    root_tag = _strip_ns(root.tag)
    for entry in root:
        tag = _strip_ns(entry.tag)
        if tag not in ("url", "sitemap"):
            continue
        loc, lastmod = None, None
        for child in entry:
            ctag = _strip_ns(child.tag)
            if ctag == "loc" and child.text:
                loc = child.text.strip()
            elif ctag == "lastmod" and child.text:
                lastmod = child.text.strip()
        if not loc:
            continue
        if root_tag == "sitemapindex" or tag == "sitemap":
            children.append(loc)
        else:
            urls.append(SitemapURL(loc=loc, lastmod=lastmod))
    return urls, children


def collect_sitemap_urls(fetcher: HttpFetcher, sitemap_urls: list[str],
                         max_urls: int = 500, max_sitemaps: int = 10) -> list[SitemapURL]:
    """Fetch sitemaps (and one level of indexes) up to bounded limits."""
    seen_sitemaps: set[str] = set()
    queue = list(dict.fromkeys(sitemap_urls))
    collected: list[SitemapURL] = []
    fetched = 0
    while queue and fetched < max_sitemaps and len(collected) < max_urls:
        sm_url = queue.pop(0)
        if sm_url in seen_sitemaps:
            continue
        seen_sitemaps.add(sm_url)
        fetched += 1
        result = fetcher.fetch(sm_url, expect_html=False)
        text = result.html
        if not result.ok or not text:
            # try gz transparently (some servers serve .gz without CE header)
            continue
        if text.lstrip()[:1] not in ("<",):
            try:
                text = gzip.decompress(text.encode("latin-1")).decode("utf-8", "replace")
            except Exception:
                continue
        urls, children = parse_sitemap_xml(text)
        collected.extend(urls)
        for child in children:
            if child not in seen_sitemaps:
                queue.append(child)
    return collected[:max_urls]
