"""Per-page content extraction.

Uses BeautifulSoup(lxml) for structure/metadata and trafilatura for clean
readable main text (with a BeautifulSoup fallback when trafilatura yields
nothing). All extraction is deterministic; no network access happens here.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .domain import NormalizedDomain, same_site
from .models import FetchResult, PageExtract

log = logging.getLogger(__name__)

try:
    import trafilatura
    _HAS_TRAFILATURA = True
except ImportError:  # pragma: no cover
    _HAS_TRAFILATURA = False

EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}\b")
# Conservative international phone matcher; requires phone-ish context or
# tel: links to keep false positives down.
PHONE_RE = re.compile(
    r"(?:(?:\+|00)[1-9]\d{0,2}[\s.\-]?)?(?:\(\d{1,4}\)[\s.\-]?)?\d{2,4}(?:[\s.\-]\d{2,4}){1,4}")
PHONE_CONTEXT_RE = re.compile(
    r"(phone|tel|call|fax|mobile|hotline|\+\d)", re.IGNORECASE)

SOCIAL_DOMAINS = (
    "linkedin.com", "twitter.com", "x.com", "facebook.com", "instagram.com",
    "youtube.com", "github.com", "tiktok.com", "vimeo.com", "medium.com",
    "pinterest.com", "threads.net", "crunchbase.com", "glassdoor.com",
    "xing.com", "wechat.com", "weibo.com",
)

BOILERPLATE_HINTS = re.compile(
    r"cookie|consent|gdpr|newsletter|subscribe|copyright|all rights reserved",
    re.IGNORECASE)

_IMG_EXT = (".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp", ".ico")


def _clean_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def extract_emails(text: str, html: str = "") -> list[str]:
    found = set(EMAIL_RE.findall(text))
    # mailto: links
    for m in re.finditer(r'mailto:([^"\'>\s?]+)', html, re.IGNORECASE):
        addr = m.group(1).strip()
        if EMAIL_RE.fullmatch(addr):
            found.add(addr)
    # Drop obvious asset names matched by the email regex (logo@2x.png)
    cleaned = {e.lower() for e in found
               if not e.lower().endswith(_IMG_EXT) and ".." not in e}
    return sorted(cleaned)


def extract_phones(text: str, html: str = "") -> list[str]:
    found: set[str] = set()
    for m in re.finditer(r'tel:([^"\'>\s]+)', html, re.IGNORECASE):
        num = re.sub(r"[^\d+]", "", m.group(1))
        if 7 <= len(num.lstrip("+")) <= 15:
            found.add(num)
    for m in PHONE_RE.finditer(text):
        raw = m.group(0)
        digits = re.sub(r"[^\d+]", "", raw)
        if not (7 <= len(digits.lstrip("+")) <= 15):
            continue
        # Require a plus prefix or phone-y context nearby to avoid dates/ids.
        ctx = text[max(0, m.start() - 40):m.start()]
        if raw.strip().startswith("+") or PHONE_CONTEXT_RE.search(ctx):
            found.add(_clean_ws(raw))
    return sorted(found)


def extract_json_ld(soup: BeautifulSoup) -> list[Any]:
    blocks: list[Any] = []
    for script in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}):
        raw = script.string or script.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Common malformation: trailing commas / control chars; try a mild repair
            try:
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", raw))
            except json.JSONDecodeError:
                continue
        blocks.extend(data if isinstance(data, list) else [data])
    return blocks


def extract_open_graph(soup: BeautifulSoup) -> dict[str, str]:
    og: dict[str, str] = {}
    for meta in soup.find_all("meta"):
        prop = meta.get("property") or meta.get("name") or ""
        if prop.startswith(("og:", "twitter:")) and meta.get("content"):
            og[prop] = _clean_ws(meta["content"])[:500]
    return og


def _fallback_text(soup: BeautifulSoup) -> str:
    """BeautifulSoup-based readable-text fallback with boilerplate removal."""
    body = soup.find("body")
    if body is None:
        return ""
    clone = BeautifulSoup(str(body), "lxml")
    for tag in clone.find_all(["script", "style", "noscript", "template",
                               "svg", "iframe", "form", "nav", "header",
                               "footer", "aside"]):
        tag.decompose()
    for tag in clone.find_all(attrs={"class": BOILERPLATE_HINTS}):
        tag.decompose()
    for tag in clone.find_all(attrs={"id": BOILERPLATE_HINTS}):
        tag.decompose()
    lines: list[str] = []
    seen: set[str] = set()
    for el in clone.find_all(["h1", "h2", "h3", "h4", "p", "li", "td", "figcaption", "blockquote"]):
        txt = _clean_ws(el.get_text(" "))
        if len(txt) < 3:
            continue
        key = txt.lower()
        if key in seen:
            continue
        seen.add(key)
        lines.append(txt)
    return "\n".join(lines)


def _main_text(html: str, url: str) -> tuple[str, list[str]]:
    warnings: list[str] = []
    text = ""
    if _HAS_TRAFILATURA:
        try:
            text = trafilatura.extract(
                html, url=url, include_comments=False, include_tables=True,
                favor_recall=True,
            ) or ""
        except Exception as exc:  # pragma: no cover - defensive
            warnings.append(f"trafilatura error: {exc}")
    if len(text) < 200:
        soup = BeautifulSoup(html, "lxml")
        fb = _fallback_text(soup)
        if len(fb) > len(text):
            text = fb
            if _HAS_TRAFILATURA:
                warnings.append("trafilatura yielded little text; used fallback extractor")
    # Deduplicate repeated lines (nav/footer repetition survivors)
    lines, seen = [], set()
    for line in text.splitlines():
        key = line.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        lines.append(line.strip())
    return "\n".join(lines), warnings


def _get_date(soup: BeautifulSoup, json_ld: list[Any], key_names: tuple[str, ...],
              meta_names: tuple[str, ...]) -> Optional[str]:
    for meta in soup.find_all("meta"):
        name = (meta.get("property") or meta.get("name") or "").lower()
        if name in meta_names and meta.get("content"):
            return _clean_ws(meta["content"])[:40]
    for block in json_ld:
        if isinstance(block, dict):
            for k in key_names:
                v = block.get(k)
                if isinstance(v, str) and v:
                    return v[:40]
    t = soup.find("time", attrs={"datetime": True})
    if t and key_names[0] == "datePublished":
        return t["datetime"][:40]
    return None


def extract_page(fetch: FetchResult, nd: NormalizedDomain,
                 include_subdomains: bool = True) -> PageExtract:
    html = fetch.html or ""
    warnings = list(fetch.warnings)
    soup = BeautifulSoup(html, "lxml")

    title = _clean_ws(soup.title.get_text()) if soup.title else None

    meta_description = None
    md = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    if md and md.get("content"):
        meta_description = _clean_ws(md["content"])[:500]

    canonical = None
    link = soup.find("link", rel=lambda v: v and "canonical" in v)
    if link and link.get("href"):
        canonical = urljoin(fetch.final_url, link["href"].strip())

    language = None
    html_tag = soup.find("html")
    if html_tag and html_tag.get("lang"):
        language = html_tag["lang"].strip()[:16]

    headings = []
    for level in ("h1", "h2", "h3"):
        for h in soup.find_all(level):
            txt = _clean_ws(h.get_text(" "))
            if txt and len(txt) < 200:
                headings.append({"level": level, "text": txt})
    # de-dup while preserving order
    seen_h: set[str] = set()
    headings = [h for h in headings
                if not (h["text"].lower() in seen_h or seen_h.add(h["text"].lower()))]

    internal_links: list[dict[str, str]] = []
    external_links: list[dict[str, str]] = []
    social_links: set[str] = set()
    seen_links: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("javascript:", "#", "mailto:", "tel:")):
            continue
        absolute = urljoin(fetch.final_url, href)
        if not absolute.startswith(("http://", "https://")):
            continue
        anchor = _clean_ws(a.get_text(" "))[:120]
        key = absolute.split("#")[0]
        host = (urlsplit(absolute).hostname or "").lower().removeprefix("www.")
        entry = {"url": key, "text": anchor}
        if any(host == d or host.endswith("." + d) for d in SOCIAL_DOMAINS):
            social_links.add(key.rstrip("/"))
            continue
        if key in seen_links:
            continue
        seen_links.add(key)
        if same_site(absolute, nd, include_subdomains):
            internal_links.append(entry)
        else:
            external_links.append(entry)

    text, text_warnings = _main_text(html, fetch.final_url)
    warnings.extend(text_warnings)

    json_ld = extract_json_ld(soup)
    og = extract_open_graph(soup)

    body_plus = text + "\n" + (meta_description or "")
    emails = extract_emails(body_plus, html)
    phones = extract_phones(body_plus, html)

    published = _get_date(soup, json_ld, ("datePublished",),
                          ("article:published_time", "date", "dc.date", "publishdate"))
    modified = _get_date(soup, json_ld, ("dateModified",),
                         ("article:modified_time", "og:updated_time", "last-modified"))

    if len(text) < 80:
        warnings.append("very little readable text extracted")

    return PageExtract(
        requested_url=fetch.requested_url,
        final_url=fetch.final_url,
        http_status=fetch.status_code,
        retrieval_method=fetch.method,
        retrieved_at=fetch.fetched_at,
        title=title,
        meta_description=meta_description,
        canonical_url=canonical,
        language=language,
        headings=headings[:40],
        clean_text=text[:60000],
        internal_links=internal_links[:300],
        external_links=external_links[:100],
        emails=emails[:30],
        phones=phones[:20],
        social_links=sorted(social_links)[:30],
        json_ld=json_ld[:20],
        open_graph=og,
        published_date=published,
        modified_date=modified,
        warnings=warnings,
    )
