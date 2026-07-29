"""Layered page retrieval: httpx first, optional Playwright fallback.

The HTTP fetcher enforces timeouts, redirect limits, retries with backoff,
content-type checks, and a streaming response-size cap. Playwright is only
imported when actually used, so the project installs and runs without it.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

from . import USER_AGENT
from .config import CrawlerConfig
from .models import FetchResult, utcnow_iso

log = logging.getLogger(__name__)

HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")
TEXTUAL_CONTENT_TYPES = HTML_CONTENT_TYPES + (
    "text/plain", "application/xml", "text/xml", "application/rss+xml",
)

# Retry only on transient conditions.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class HttpFetcher:
    def __init__(self, cfg: CrawlerConfig) -> None:
        self.cfg = cfg
        self.max_bytes = int(cfg.max_response_size_mb * 1024 * 1024)
        self.client = httpx.Client(
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.5",
                "Accept-Language": "en;q=0.9,*;q=0.5",
                "Accept-Encoding": "gzip, deflate",
            },
            timeout=httpx.Timeout(cfg.request_timeout_seconds, connect=min(10.0, cfg.request_timeout_seconds)),
            follow_redirects=True,
            max_redirects=cfg.max_redirects,
        )

    def close(self) -> None:
        self.client.close()

    def fetch(self, url: str, expect_html: bool = True) -> FetchResult:
        """GET a URL with retries; returns a FetchResult, never raises."""
        last_error: Optional[str] = None
        for attempt in range(self.cfg.max_retries + 1):
            try:
                result = self._fetch_once(url, expect_html)
                if (result.status_code in RETRYABLE_STATUS
                        and attempt < self.cfg.max_retries):
                    last_error = f"HTTP {result.status_code}"
                    time.sleep(min(2 ** attempt, 8))
                    continue
                return result
            except httpx.TimeoutException:
                last_error = "timeout"
            except httpx.TooManyRedirects:
                last_error = "too many redirects"
                break  # not transient
            except httpx.ConnectError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                msg = str(exc).lower()
                if "certificate" in msg or "ssl" in msg:
                    # TLS misconfiguration is permanent - retrying won't help
                    last_error = f"TLS certificate error: {exc}"
                    break
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            except Exception as exc:  # defensive: fetching must never crash a job
                last_error = f"{type(exc).__name__}: {exc}"
            if attempt < self.cfg.max_retries:
                time.sleep(min(2 ** attempt, 8))
        log.warning("fetch failed %s: %s", url, last_error)
        return FetchResult(
            requested_url=url, final_url=url, status_code=None, ok=False,
            method="http", error=last_error, fetched_at=utcnow_iso(),
        )

    def _fetch_once(self, url: str, expect_html: bool) -> FetchResult:
        warnings: list[str] = []
        with self.client.stream("GET", url) as resp:
            ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
            final_url = str(resp.url)
            if resp.status_code >= 400:
                return FetchResult(
                    requested_url=url, final_url=final_url,
                    status_code=resp.status_code, ok=False, method="http",
                    content_type=ctype, error=f"HTTP {resp.status_code}",
                )
            allowed = HTML_CONTENT_TYPES if expect_html else TEXTUAL_CONTENT_TYPES
            if ctype and ctype not in allowed:
                return FetchResult(
                    requested_url=url, final_url=final_url,
                    status_code=resp.status_code, ok=False, method="http",
                    content_type=ctype,
                    error=f"skipped non-HTML content-type: {ctype}",
                )
            declared = resp.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > self.max_bytes:
                return FetchResult(
                    requested_url=url, final_url=final_url,
                    status_code=resp.status_code, ok=False, method="http",
                    content_type=ctype,
                    error=f"response too large ({declared} bytes)",
                )
            chunks: list[bytes] = []
            size = 0
            for chunk in resp.iter_bytes(chunk_size=65536):
                size += len(chunk)
                if size > self.max_bytes:
                    warnings.append(f"response truncated at {self.max_bytes} bytes")
                    break
                chunks.append(chunk)
            body = b"".join(chunks)
        encoding = resp.encoding or "utf-8"
        try:
            html = body.decode(encoding, errors="replace")
        except LookupError:
            html = body.decode("utf-8", errors="replace")
            warnings.append(f"unknown encoding {encoding!r}; decoded as utf-8")
        return FetchResult(
            requested_url=url, final_url=final_url, status_code=resp.status_code,
            ok=True, method="http", content_type=ctype, html=html,
            warnings=warnings,
        )


class PlaywrightFetcher:
    """JavaScript-rendering fallback. Only constructed when needed.

    Requires:  pip install playwright  &&  playwright install chromium
    """

    def __init__(self, cfg: CrawlerConfig) -> None:
        self.cfg = cfg
        self._pw = None
        self._browser = None

    def available(self) -> bool:
        try:
            import playwright.sync_api  # noqa: F401
            return True
        except ImportError:
            return False

    def _ensure_browser(self):
        if self._browser is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
        return self._browser

    def fetch(self, url: str) -> FetchResult:
        try:
            browser = self._ensure_browser()
            context = browser.new_context(user_agent=USER_AGENT)
            page = context.new_page()
            try:
                resp = page.goto(
                    url, timeout=self.cfg.request_timeout_seconds * 1000,
                    wait_until="networkidle",
                )
                html = page.content()
                status = resp.status if resp else None
                final_url = page.url
            finally:
                context.close()
            ok = status is not None and status < 400 and bool(html)
            return FetchResult(
                requested_url=url, final_url=final_url, status_code=status,
                ok=ok, method="playwright", content_type="text/html",
                html=html if ok else None,
                error=None if ok else f"HTTP {status}",
            )
        except Exception as exc:
            log.warning("playwright fetch failed %s: %s", url, exc)
            return FetchResult(
                requested_url=url, final_url=url, status_code=None, ok=False,
                method="playwright", error=f"{type(exc).__name__}: {exc}",
            )

    def close(self) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # pragma: no cover
            pass
        self._browser = None
        self._pw = None
