"""URL discovery, filtering, and prioritization.

Combines homepage links, navigation/footer links, sitemap URLs, and known
high-value URL patterns into a ranked candidate list. This is deliberately
NOT a whole-site crawler: it scores likely-useful pages and takes the top N.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl

from .domain import NormalizedDomain, same_site
from .models import URLCandidate

# --- filtering ------------------------------------------------------------

SKIP_EXTENSIONS = (
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip", ".rar",
    ".7z", ".tar", ".gz", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp",
    ".ico", ".mp3", ".mp4", ".avi", ".mov", ".wmv", ".webm", ".css", ".js",
    ".json", ".xml", ".rss", ".atom", ".woff", ".woff2", ".ttf", ".eot",
    ".exe", ".dmg", ".msi", ".apk", ".bin", ".iso",
)

# Path fragments that indicate low-value or unsafe-to-crawl areas.
SKIP_PATH_PATTERNS = [
    r"/login", r"/log-in", r"/signin", r"/sign-in", r"/signup", r"/sign-up",
    r"/register", r"/account", r"/my-account", r"/cart", r"/checkout",
    r"/basket", r"/wp-admin", r"/wp-login", r"/admin\b", r"/auth/",
    r"/search", r"/tag/", r"/tags/", r"/category/", r"/categories/",
    r"/label/", r"/archive/", r"/archives/", r"/calendar", r"/events?/\d{4}",
    r"/page/\d+", r"/feed/?$", r"/print/", r"/share", r"/cdn-cgi/",
    r"/wp-json/", r"/xmlrpc", r"/comment", r"/trackback", r"/#",
    r"/privacy-tools", r"/unsubscribe", r"/logout",
]
_SKIP_RE = re.compile("|".join(SKIP_PATH_PATTERNS), re.IGNORECASE)

# Query parameters that create infinite URL spaces; drop the whole URL if
# it relies on them, otherwise strip tracking params.
TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
                   "utm_content", "gclid", "fbclid", "mc_cid", "mc_eid", "ref"}
TRAP_PARAMS = {"page", "p", "offset", "start", "sort", "order", "filter", "s",
               "q", "search", "query", "date", "month", "year", "day", "view",
               "replytocom", "sessionid", "sid", "PHPSESSID"}

# --- prioritization -------------------------------------------------------

# (regex on path or anchor/title text, page-value score)
VALUE_PATTERNS: list[tuple[str, float, str]] = [
    (r"^/?$", 100, "homepage"),
    (r"about|who[-_ ]we[-_ ]are|our[-_ ]story|company|profile|overview|mission", 90, "about"),
    (r"leadership|management|board|executive|our[-_ ]team|team|founders?", 80, "leadership"),
    (r"products?|catalog", 78, "product"),
    (r"services?", 76, "service"),
    (r"solutions?", 74, "solution"),
    (r"industri|sectors?|markets?|verticals?", 72, "industry"),
    (r"applications?|use[-_ ]cases?", 70, "application"),
    (r"customers?|clients?|references", 66, "customer"),
    (r"case[-_ ]stud|success[-_ ]stor|testimonial", 64, "case_study"),
    (r"locations?|offices?|facilities|global|worldwide|branches", 62, "location"),
    (r"contact|get[-_ ]in[-_ ]touch|reach[-_ ]us", 60, "contact"),
    (r"press[-_ ]?(releases?|room)?|media|newsroom", 56, "press_release"),
    (r"news|announcements?|updates", 54, "news"),
    (r"blog|insights|articles|resources", 45, "blog"),
    (r"careers?|jobs?|join[-_ ]us|work[-_ ]with[-_ ]us|vacanc", 44, "careers"),
    (r"investors?|shareholders?|financial|annual[-_ ]report", 42, "investor"),
    (r"partners?|partnership|alliances", 40, "other"),
    (r"certifications?|quality|accreditations?", 38, "other"),
    (r"history|milestones", 36, "about"),
]
_VALUE_RES = [(re.compile(p, re.IGNORECASE), score, label) for p, score, label in VALUE_PATTERNS]

# Common high-value paths to try even if not linked from the homepage.
GUESS_PATHS = ["/about", "/about-us", "/company", "/contact", "/contact-us",
               "/products", "/services", "/team", "/careers"]


def _clean_parts(url: str) -> tuple:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in TRACKING_PARAMS]
    qs = "&".join(f"{k}={v}" if v else k for k, v in query)
    path = re.sub(r"//+", "/", parts.path) or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return parts, path, qs


def canonicalize_url(url: str) -> str:
    """Normalize a URL as a DEDUP KEY: strip fragment/tracking params, tidy
    path, drop www and force https. Not necessarily fetchable - use
    clean_url() for the URL to actually request."""
    parts, path, qs = _clean_parts(url)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    netloc = host + (f":{parts.port}" if parts.port and parts.port not in (80, 443) else "")
    return urlunsplit(("https", netloc, path, qs, ""))


def clean_url(url: str) -> str:
    """Tidy a URL for FETCHING: same cleanup but preserve scheme and host
    exactly as discovered (some sites only serve TLS on www. or apex)."""
    parts, path, qs = _clean_parts(url)
    return urlunsplit((parts.scheme or "https", parts.netloc, path, qs, ""))


def prefer_host(url: str, preferred_netloc: str, preferred_scheme: str = "https") -> str:
    """Rewrite url onto the host variant that is known to work.

    If url's host differs from preferred_netloc only by a www. prefix,
    swap it (e.g. sitemap says https://example.com/x but only
    https://www.example.com/ answers with a valid certificate).
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    pref_host = preferred_netloc.split("@")[-1].split(":")[0].lower()
    if host == pref_host:
        return url
    strip = lambda h: h[4:] if h.startswith("www.") else h  # noqa: E731
    if strip(host) == strip(pref_host):
        return urlunsplit((preferred_scheme, preferred_netloc,
                           parts.path, parts.query, ""))
    return url


def should_skip(url: str) -> str | None:
    """Return a skip reason, or None if the URL is crawlable."""
    parts = urlsplit(url)
    path = parts.path.lower()
    for ext in SKIP_EXTENSIONS:
        if path.endswith(ext):
            return f"binary/asset extension {ext}"
    if _SKIP_RE.search(path):
        m = _SKIP_RE.search(path)
        return f"low-value/unsafe path pattern '{m.group(0)}'"
    params = {k.lower() for k, _ in parse_qsl(parts.query, keep_blank_values=True)}
    trap = params & TRAP_PARAMS
    if trap:
        return f"URL-parameter trap ({', '.join(sorted(trap))})"
    if len(params - TRACKING_PARAMS) > 2:
        return "too many query parameters"
    if len(url) > 300:
        return "URL too long"
    if path.count("/") > 6:
        return "path too deep"
    return None


def score_url(url: str, anchor_text: str = "") -> tuple[float, list[str]]:
    """Priority score plus the signals that produced it."""
    parts = urlsplit(url)
    path = parts.path or "/"
    text = anchor_text.strip().lower()
    signals: list[str] = []
    best = 0.0
    if path in ("", "/"):
        return 100.0, ["homepage path"]
    for rx, score, label in _VALUE_RES[1:]:
        if rx.search(path):
            if score > best:
                best = score
                signals = [f"url pattern -> {label} ({rx.pattern})"]
        elif text and rx.search(text):
            adj = score - 5
            if adj > best:
                best = adj
                signals = [f"anchor text -> {label} ({rx.pattern})"]
    # Shallow paths on a company site are usually more central.
    depth = max(0, path.rstrip("/").count("/") - 1)
    depth_bonus = max(0.0, 10.0 - 4.0 * depth)
    if best == 0.0:
        best = 15.0
        signals = ["no keyword match; default low priority"]
    best += depth_bonus
    if depth_bonus:
        signals.append(f"shallow-path bonus +{depth_bonus:.0f}")
    return best, signals


def build_candidates(
    nd: NormalizedDomain,
    homepage_links: list[dict[str, str]],
    sitemap_urls: list[str],
    include_subdomains: bool = True,
    max_candidates: int = 200,
) -> list[URLCandidate]:
    """Merge sources into a deduplicated, ranked candidate list."""
    seen: dict[str, URLCandidate] = {}

    def add(url: str, source: str, anchor: str = "", depth: int = 1) -> None:
        url = url.strip()
        if not url or url.startswith(("mailto:", "tel:", "javascript:", "#")):
            return
        absolute = urljoin(nd.start_url, url)
        if not same_site(absolute, nd, include_subdomains):
            return
        canon = canonicalize_url(absolute)
        fetchable = clean_url(absolute)
        skip = should_skip(canon)
        priority, _signals = score_url(canon, anchor)
        existing = seen.get(canon)
        if existing:
            if priority > existing.priority:
                existing.priority = priority
                existing.anchor_text = anchor or existing.anchor_text
            return
        seen[canon] = URLCandidate(
            url=fetchable, priority=priority, discovery_source=source,
            depth=depth, anchor_text=anchor[:120], skip_reason=skip,
        )

    add(nd.start_url, "start", depth=0)
    for link in homepage_links:
        add(link.get("url", ""), "homepage_link", link.get("text", ""))
    for sm_url in sitemap_urls:
        add(sm_url, "sitemap")
    for path in GUESS_PATHS:
        add(nd.start_url.rstrip("/") + path, "guess")

    ranked = sorted(seen.values(), key=lambda c: (-c.priority, c.url))
    return ranked[:max_candidates]
