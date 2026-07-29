"""Layered page retrieval with bounded, public-network-only HTTP access.

The HTTP fetcher enforces timeouts, retries, redirect limits, content-type
checks, a streaming response-size cap, and an SSRF boundary. Every requested
host is resolved before a connection is attempted; loopback, private,
link-local, reserved, multicast, and unspecified addresses are refused.
Redirects are followed manually so each destination is validated before the
next request. Cross-host redirects are refused: the collector may retry a
known apex/www variant only after loading that host's robots policy.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import time
from collections.abc import Callable
from typing import Optional
from urllib.parse import urljoin, urlsplit

import httpx

from . import USER_AGENT
from .config import CrawlerConfig
from .models import FetchResult, utcnow_iso

log = logging.getLogger(__name__)

HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")
TEXTUAL_CONTENT_TYPES = HTML_CONTENT_TYPES + (
    "text/plain",
    "application/xml",
    "text/xml",
    "application/rss+xml",
)
REDIRECT_STATUS = {301, 302, 303, 307, 308}
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

Resolver = Callable[[str, int], tuple[str, ...]]


class UnsafeTargetError(ValueError):
    """A URL would connect outside the public Internet boundary."""


def _system_resolver(host: str, port: int) -> tuple[str, ...]:
    addresses = {
        item[4][0].split("%", 1)[0]
        for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    }
    return tuple(sorted(addresses))


class HttpFetcher:
    def __init__(
        self,
        cfg: CrawlerConfig,
        *,
        resolver: Resolver | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.cfg = cfg
        self.max_bytes = int(cfg.max_response_size_mb * 1024 * 1024)
        self._resolver = resolver or _system_resolver
        self.client = httpx.Client(
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.5",
                "Accept-Language": "en;q=0.9,*;q=0.5",
                "Accept-Encoding": "gzip, deflate",
            },
            timeout=httpx.Timeout(
                cfg.request_timeout_seconds,
                connect=min(10.0, cfg.request_timeout_seconds),
            ),
            follow_redirects=False,
            transport=transport,
            trust_env=False,
        )

    def close(self) -> None:
        self.client.close()

    def fetch(self, url: str, expect_html: bool = True) -> FetchResult:
        """GET a URL with retries; returns a FetchResult, never raises."""
        last_error: Optional[str] = None
        for attempt in range(self.cfg.max_retries + 1):
            try:
                result = self._fetch_once(url, expect_html)
                if result.status_code in RETRYABLE_STATUS and attempt < self.cfg.max_retries:
                    last_error = f"HTTP {result.status_code}"
                    time.sleep(min(2**attempt, 8))
                    continue
                return result
            except UnsafeTargetError as exc:
                last_error = f"unsafe target: {exc}"
                break
            except httpx.TimeoutException:
                last_error = "timeout"
            except httpx.TooManyRedirects:
                last_error = "too many redirects"
                break
            except httpx.ConnectError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                msg = str(exc).lower()
                if "certificate" in msg or "ssl" in msg:
                    last_error = f"TLS certificate error: {exc}"
                    break
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            except Exception as exc:  # defensive: fetching must never crash a job
                last_error = f"{type(exc).__name__}: {exc}"
            if attempt < self.cfg.max_retries:
                time.sleep(min(2**attempt, 8))
        log.warning("fetch failed %s: %s", url, last_error)
        return FetchResult(
            requested_url=url,
            final_url=url,
            status_code=None,
            ok=False,
            method="http",
            error=last_error,
            fetched_at=utcnow_iso(),
        )

    def _validate_target(self, url: str) -> tuple[str, int]:
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError as exc:
            raise UnsafeTargetError(f"malformed URL: {url!r}") from exc
        if parts.scheme not in {"http", "https"}:
            raise UnsafeTargetError(f"unsupported scheme: {parts.scheme!r}")
        if parts.username is not None or parts.password is not None:
            raise UnsafeTargetError("embedded credentials are not allowed")
        host = (parts.hostname or "").rstrip(".").lower()
        if not host:
            raise UnsafeTargetError("URL has no hostname")
        effective_port = port or (443 if parts.scheme == "https" else 80)

        try:
            literal = ipaddress.ip_address(host.split("%", 1)[0])
            addresses = (literal,)
        except ValueError:
            try:
                resolved = self._resolver(host, effective_port)
            except OSError as exc:
                raise UnsafeTargetError(f"DNS resolution failed for {host}: {exc}") from exc
            if not resolved:
                raise UnsafeTargetError(f"DNS returned no addresses for {host}")
            try:
                addresses = tuple(
                    ipaddress.ip_address(address.split("%", 1)[0]) for address in resolved
                )
            except ValueError as exc:
                raise UnsafeTargetError(f"DNS returned an invalid address for {host}") from exc

        unsafe = [str(address) for address in addresses if not address.is_global]
        if unsafe:
            raise UnsafeTargetError(f"{host} resolves to non-public address(es): {', '.join(unsafe)}")
        return host, effective_port

    def _fetch_once(self, url: str, expect_html: bool) -> FetchResult:
        current_url = url
        requested_host, _ = self._validate_target(current_url)

        for redirect_count in range(self.cfg.max_redirects + 1):
            current_host, _ = self._validate_target(current_url)
            if current_host != requested_host:
                raise UnsafeTargetError(
                    f"cross-host redirect from {requested_host} to {current_host} is not allowed"
                )

            with self.client.stream("GET", current_url) as resp:
                if resp.status_code in REDIRECT_STATUS:
                    location = resp.headers.get("location")
                    if not location:
                        return FetchResult(
                            requested_url=url,
                            final_url=current_url,
                            status_code=resp.status_code,
                            ok=False,
                            method="http",
                            error="redirect response has no Location header",
                        )
                    if redirect_count >= self.cfg.max_redirects:
                        raise httpx.TooManyRedirects(
                            "maximum redirects exceeded",
                            request=resp.request,
                        )
                    next_url = urljoin(str(resp.url), location)
                    next_host, _ = self._validate_target(next_url)
                    if next_host != requested_host:
                        raise UnsafeTargetError(
                            f"cross-host redirect from {requested_host} to {next_host} is not allowed"
                        )
                    current_url = next_url
                    continue

                return self._read_response(url, resp, expect_html)

        raise httpx.TooManyRedirects("maximum redirects exceeded")

    def _read_response(
        self,
        requested_url: str,
        resp: httpx.Response,
        expect_html: bool,
    ) -> FetchResult:
        warnings: list[str] = []
        ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        final_url = str(resp.url)
        if resp.status_code >= 400:
            return FetchResult(
                requested_url=requested_url,
                final_url=final_url,
                status_code=resp.status_code,
                ok=False,
                method="http",
                content_type=ctype,
                error=f"HTTP {resp.status_code}",
            )
        allowed = HTML_CONTENT_TYPES if expect_html else TEXTUAL_CONTENT_TYPES
        if ctype and ctype not in allowed:
            return FetchResult(
                requested_url=requested_url,
                final_url=final_url,
                status_code=resp.status_code,
                ok=False,
                method="http",
                content_type=ctype,
                error=f"skipped non-HTML content-type: {ctype}",
            )
        declared = resp.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > self.max_bytes:
            return FetchResult(
                requested_url=requested_url,
                final_url=final_url,
                status_code=resp.status_code,
                ok=False,
                method="http",
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
            requested_url=requested_url,
            final_url=final_url,
            status_code=resp.status_code,
            ok=True,
            method="http",
            content_type=ctype,
            html=html,
            warnings=warnings,
        )


class PlaywrightFetcher:
    """JavaScript-rendering fallback. Only constructed when needed.

    Requires: pip install playwright && playwright install chromium
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
                    url,
                    timeout=self.cfg.request_timeout_seconds * 1000,
                    wait_until="networkidle",
                )
                html = page.content()
                status = resp.status if resp else None
                final_url = page.url
            finally:
                context.close()
            ok = status is not None and status < 400 and bool(html)
            return FetchResult(
                requested_url=url,
                final_url=final_url,
                status_code=status,
                ok=ok,
                method="playwright",
                content_type="text/html",
                html=html if ok else None,
                error=None if ok else f"HTTP {status}",
            )
        except Exception as exc:
            log.warning("playwright fetch failed %s: %s", url, exc)
            return FetchResult(
                requested_url=url,
                final_url=url,
                status_code=None,
                ok=False,
                method="playwright",
                error=f"{type(exc).__name__}: {exc}",
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
