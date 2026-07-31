"""The research worker contract and registry (RES-001).

The registry is the seam that lets a research source be plugged in or
unplugged without touching the pipeline, so its refusals matter as much
as its successes: a run that quietly did less than the operator asked for
would be exactly the kind of untruthful outcome this system refuses.

Entirely offline. The website worker is driven through its collector seam
with synthetic pages.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from app.services.research.contracts import (
    ResearchRequest,
    ResearchWorkerError,
    SourcedFact,
    WorkerResult,
)
from app.services.research.workers import (
    WorkerNotRegistered,
    available_workers,
    build_workers,
    register_worker,
)
from app.services.research.workers._website.collect import CollectionOutcome, PageRecord
from app.services.research.workers._website.models import Fact
from app.services.research.workers.website import WebsiteWorker

DOMAIN = "engines.example"


def _vendor_fact(
    field: str = "company_name",
    value: object = "Analytical Engines Ltd",
    *,
    source_url: str = f"https://{DOMAIN}/about",
    fact_type: str = "explicit",
    confidence: float = 0.9,
) -> Fact:
    return Fact(
        field=field,
        value=value,
        fact_type=fact_type,
        source_url=source_url,
        supporting_text="We are Analytical Engines Ltd.",
        retrieved_at=datetime.now(UTC).isoformat(),
        extraction_method="explicit_statement",
        confidence=confidence,
    )


def _outcome(**kw: object) -> CollectionOutcome:
    base = CollectionOutcome(domain=DOMAIN)
    base.pages = [
        PageRecord(
            url=f"https://{DOMAIN}/",
            title="Home",
            page_type="home",
            retrieval_method="http",
            retrieved_at=datetime.now(UTC).isoformat(),
            text_length=900,
        )
    ]
    for key, value in kw.items():
        setattr(base, key, value)
    return base


def _worker(outcome: CollectionOutcome) -> WebsiteWorker:
    return WebsiteWorker(collector=lambda _domain, _config: outcome)


# --- the contract ------------------------------------------------------------


def test_a_fact_without_an_absolute_source_url_is_refused() -> None:
    with pytest.raises(ValueError, match="absolute http"):
        SourcedFact(
            field="company_name",
            value="Analytical Engines Ltd",
            source_url="engines.example/about",
            retrieved_at=datetime.now(UTC),
            extraction_method="explicit",
            confidence=0.9,
        )


def test_a_naive_timestamp_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        SourcedFact(
            field="company_name",
            value="Analytical Engines Ltd",
            source_url=f"https://{DOMAIN}/about",
            retrieved_at=datetime(2026, 7, 29, 12, 0, 0),  # noqa: DTZ001
            extraction_method="explicit",
            confidence=0.9,
        )


@pytest.mark.parametrize("confidence", [-0.1, 1.1])
def test_confidence_outside_zero_to_one_is_refused(confidence: float) -> None:
    with pytest.raises(ValueError, match="confidence"):
        SourcedFact(
            field="company_name",
            value="Analytical Engines Ltd",
            source_url=f"https://{DOMAIN}/about",
            retrieved_at=datetime.now(UTC),
            extraction_method="explicit",
            confidence=confidence,
        )


# --- the website worker ------------------------------------------------------


def test_facts_carry_their_page_and_extraction_strength() -> None:
    outcome = _outcome(facts=[_vendor_fact(), _vendor_fact("founded_year", 1843)])
    result = _worker(outcome).run(ResearchRequest(domain=DOMAIN))

    assert isinstance(result, WorkerResult)
    assert [f.field for f in result.facts] == ["company_name", "founded_year"]
    assert all(f.source_url == f"https://{DOMAIN}/about" for f in result.facts)
    # A heuristic must never later read as an explicit statement.
    assert all(":" in f.extraction_method for f in result.facts)
    assert result.facts[1].value == "1843", "non-string values are stringified, not dropped"


def test_a_fact_with_no_usable_source_is_dropped_and_reported() -> None:
    outcome = _outcome(facts=[_vendor_fact(), _vendor_fact(source_url="")])
    result = _worker(outcome).run(ResearchRequest(domain=DOMAIN))

    assert len(result.facts) == 1
    assert any("no usable source URL" in w for w in result.warnings)


def test_an_unreachable_site_raises_a_non_retryable_error() -> None:
    outcome = _outcome(unreachable=True, unreachable_reason="homepage unreachable: refused")
    with pytest.raises(ResearchWorkerError) as excinfo:
        _worker(outcome).run(ResearchRequest(domain=DOMAIN))

    assert excinfo.value.code == "site_unreachable"
    assert excinfo.value.retryable is False, "retrying an unreachable site changes nothing"


def test_a_thin_site_is_insufficient_rather_than_an_error() -> None:
    outcome = _outcome(facts=[_vendor_fact()], warnings=["only 1 fact(s) extracted"])
    result = _worker(outcome).run(ResearchRequest(domain=DOMAIN))

    assert result.sufficient is False
    assert result.facts, "an insufficient run still returns what it did find"


def test_a_missing_domain_is_refused_before_any_fetch() -> None:
    calls: list[str] = []

    def _collector(domain: str, _config: object) -> CollectionOutcome:
        calls.append(domain)
        return _outcome()

    with pytest.raises(ResearchWorkerError) as excinfo:
        WebsiteWorker(collector=_collector).run(ResearchRequest(domain="  "))

    assert excinfo.value.code == "domain_missing"
    assert calls == []


def test_the_raw_payload_preserves_what_was_read() -> None:
    outcome = _outcome(facts=[_vendor_fact()])
    result = _worker(outcome).run(ResearchRequest(domain=DOMAIN))

    assert result.raw["domain"] == DOMAIN
    assert result.raw["pages"][0]["url"] == f"https://{DOMAIN}/"
    assert result.raw["facts"][0]["field"] == "company_name"


def test_crawl_bounds_cannot_be_widened_by_configuration() -> None:
    """An operator may tighten the crawl. Widening it is not on offer."""

    captured: dict[str, object] = {}

    def _collector(_domain: str, config: object) -> CollectionOutcome:
        captured["max_pages"] = config.crawler.max_pages  # type: ignore[attr-defined]
        captured["max_depth"] = config.crawler.max_depth  # type: ignore[attr-defined]
        captured["delay"] = config.crawler.delay_between_requests_seconds  # type: ignore[attr-defined]
        captured["robots"] = config.discovery.use_robots_txt  # type: ignore[attr-defined]
        return _outcome(facts=[_vendor_fact()])

    WebsiteWorker(collector=_collector).run(
        ResearchRequest(
            domain=DOMAIN,
            options={
                "max_pages": 5000,
                "max_depth": 40,
                "delay_between_requests_seconds": 0.0,
                "use_robots_txt": False,
            },
        )
    )

    assert captured["max_pages"] == 25
    assert captured["max_depth"] == 3
    assert captured["delay"] == 1.0, "the politeness delay has a floor"
    assert captured["robots"] is True, "robots.txt is never optional"


# --- the registry ------------------------------------------------------------


def test_the_website_worker_is_registered_by_default() -> None:
    assert "website" in available_workers()
    built = build_workers()
    assert [w.name for w in built] == ["website"]


def test_an_unknown_worker_is_an_error_not_a_silent_skip() -> None:
    with pytest.raises(WorkerNotRegistered, match="nonexistent"):
        build_workers(["website", "nonexistent"])


def test_workers_run_in_the_order_requested() -> None:
    class Stub:
        name = "stub"
        version = "1"

        def run(self, request: ResearchRequest) -> WorkerResult:  # pragma: no cover
            raise AssertionError("not called")

    register_worker("stub", Stub)
    try:
        assert [w.name for w in build_workers(["stub", "website"])] == ["stub", "website"]
        assert [w.name for w in build_workers(["website", "stub"])] == ["website", "stub"]
    finally:
        from app.services.research.workers import registry

        registry._REGISTRY.pop("stub", None)


def test_requesting_no_workers_yields_none() -> None:
    assert build_workers([]) == ()


# --- live HTTP safety boundary ------------------------------------------------


def _public_resolver(host: str, _port: int) -> tuple[str, ...]:
    assert host == "public.example"
    return ("93.184.216.34",)


def test_http_fetcher_rejects_private_targets_before_transport() -> None:
    import httpx
    from app.services.research.workers._website.config import CrawlerConfig
    from app.services.research.workers._website.fetcher import HttpFetcher

    calls: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text="should not be reached")

    fetcher = HttpFetcher(
        CrawlerConfig(max_retries=0),
        transport=httpx.MockTransport(_handler),
    )
    try:
        result = fetcher.fetch("http://127.0.0.1/admin")
    finally:
        fetcher.close()

    assert result.ok is False
    assert "non-public" in (result.error or "")
    assert calls == []


def test_http_fetcher_rejects_private_redirect_before_second_request() -> None:
    import httpx
    from app.services.research.workers._website.config import CrawlerConfig
    from app.services.research.workers._website.fetcher import HttpFetcher

    calls: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "http://169.254.169.254/latest/meta-data"},
            request=request,
        )

    fetcher = HttpFetcher(
        CrawlerConfig(max_retries=0),
        resolver=_public_resolver,
        transport=httpx.MockTransport(_handler),
    )
    try:
        result = fetcher.fetch("https://public.example/")
    finally:
        fetcher.close()

    assert result.ok is False
    assert "non-public" in (result.error or "")
    assert calls == ["https://public.example/"]


def test_robots_network_failure_denies_crawling() -> None:
    from app.services.research.workers._website.models import FetchResult
    from app.services.research.workers._website.robots import fetch_robots

    class StubFetcher:
        def fetch(self, url: str, expect_html: bool = True) -> FetchResult:
            del expect_html
            return FetchResult(
                requested_url=url,
                final_url=url,
                status_code=None,
                ok=False,
                method="http",
                error="timeout",
            )

    policy = fetch_robots(StubFetcher(), "https://public.example/")  # type: ignore[arg-type]

    assert policy.fetched is False
    assert policy.can_fetch("https://public.example/") is False


def test_missing_robots_file_allows_crawling() -> None:
    from app.services.research.workers._website.models import FetchResult
    from app.services.research.workers._website.robots import fetch_robots

    class StubFetcher:
        def fetch(self, url: str, expect_html: bool = True) -> FetchResult:
            del expect_html
            return FetchResult(
                requested_url=url,
                final_url=url,
                status_code=404,
                ok=False,
                method="http",
                error="HTTP 404",
            )

    policy = fetch_robots(StubFetcher(), "https://public.example/")  # type: ignore[arg-type]

    assert policy.fetched is True
    assert policy.can_fetch("https://public.example/") is True


# --- partial sitemaps, TLS classification, apex/www deferral ------------------
#
# A research run that quietly gathered less than the site offered, and said
# nothing about it, is the failure mode all three of these guard. Every test
# below is offline: synthetic documents and a scripted fetcher.


SITEMAP_HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
)


def _sitemap(*paths: str) -> str:
    body = "".join(f"  <url><loc>https://{DOMAIN}{p}</loc></url>\n" for p in paths)
    return f"{SITEMAP_HEAD}{body}</urlset>\n"


def test_a_truncated_sitemap_keeps_the_entries_that_arrived() -> None:
    from app.services.research.workers._website.sitemap import parse_sitemap_xml

    whole = _sitemap("/", "/about", "/products")
    cut = whole[: whole.index("/products")] + "/prod"

    result = parse_sitemap_xml(cut)

    assert [u.loc for u in result.urls] == [
        f"https://{DOMAIN}/",
        f"https://{DOMAIN}/about",
    ]
    assert result.complete is False
    assert result.warning is not None and "2 complete url entries" in result.warning


def test_a_malformed_sitemap_tail_keeps_the_entries_before_it() -> None:
    from app.services.research.workers._website.sitemap import parse_sitemap_xml

    document = _sitemap("/", "/about") + " < < < not xml </urlset>"

    result = parse_sitemap_xml(document)

    assert [u.loc for u in result.urls] == [
        f"https://{DOMAIN}/",
        f"https://{DOMAIN}/about",
    ]
    assert result.complete is False


def test_a_whole_sitemap_is_reported_complete() -> None:
    from app.services.research.workers._website.sitemap import parse_sitemap_xml

    result = parse_sitemap_xml(_sitemap("/", "/about"))

    assert len(result.urls) == 2
    assert result.complete is True
    assert result.warning is None


def test_a_sitemap_index_yields_children_to_follow() -> None:
    from app.services.research.workers._website.sitemap import parse_sitemap_xml

    document = (
        '<?xml version="1.0"?>'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<sitemap><loc>https://{DOMAIN}/sitemap-1.xml</loc></sitemap>"
        f"<sitemap><loc>https://{DOMAIN}/sitemap-2.xml</loc></sitemap>"
        "</sitemapindex>"
    )

    result = parse_sitemap_xml(document)

    assert result.urls == []
    assert result.children == [
        f"https://{DOMAIN}/sitemap-1.xml",
        f"https://{DOMAIN}/sitemap-2.xml",
    ]
    assert result.complete is True


def test_prefixed_namespaces_and_loose_whitespace_are_handled() -> None:
    from app.services.research.workers._website.sitemap import parse_sitemap_xml

    document = (
        '<?xml version="1.0"?>'
        '<sm:urlset xmlns:sm="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<sm:url>\n  <sm:loc>\n    https://{DOMAIN}/spaced\n  </sm:loc>\n</sm:url>"
        "</sm:urlset>"
    )

    result = parse_sitemap_xml(document)

    assert [u.loc for u in result.urls] == [f"https://{DOMAIN}/spaced"]


def test_the_sitemap_url_bound_stops_parsing_early() -> None:
    from app.services.research.workers._website.sitemap import parse_sitemap_xml

    result = parse_sitemap_xml(_sitemap(*[f"/p{i}" for i in range(400)]), max_urls=5)

    assert len(result.urls) == 5
    assert result.limit_reached is True
    # A configured bound is not a fault and must not be reported as one.
    assert result.complete is True
    assert result.warning is None


def test_a_truncated_sitemap_response_is_recorded_as_partial() -> None:
    """The defect in full: ok=True plus a cut body used to yield zero URLs."""

    from app.services.research.workers._website.models import FetchResult
    from app.services.research.workers._website.sitemap import collect_sitemap_urls

    whole = _sitemap("/", "/about", "/products")
    cut = whole[: whole.index("/products")] + "/prod"

    class _CappedFetcher:
        max_bytes = 8 * 1024 * 1024

        def fetch(self, url: str, expect_html: bool = True) -> FetchResult:
            del expect_html
            return FetchResult(
                requested_url=url,
                final_url=url,
                status_code=200,
                ok=True,
                method="http",
                content_type="application/xml",
                html=cut,
                truncated=True,
            )

    collected = collect_sitemap_urls(
        _CappedFetcher(),  # type: ignore[arg-type]
        [f"https://{DOMAIN}/sitemap.xml"],
    )

    assert [e.loc for e in collected.entries] == [
        f"https://{DOMAIN}/",
        f"https://{DOMAIN}/about",
    ]
    assert collected.partial is True
    assert any("cut short" in w for w in collected.warnings)


def test_a_malformed_sitemap_is_reported_as_malformed_not_truncated() -> None:
    from app.services.research.workers._website.models import FetchResult
    from app.services.research.workers._website.sitemap import collect_sitemap_urls

    document = _sitemap("/", "/about") + " < < < not xml </urlset>"

    class _WholeButBrokenFetcher:
        max_bytes = 8 * 1024 * 1024

        def fetch(self, url: str, expect_html: bool = True) -> FetchResult:
            del expect_html
            return FetchResult(
                requested_url=url,
                final_url=url,
                status_code=200,
                ok=True,
                method="http",
                content_type="application/xml",
                html=document,
                truncated=False,
            )

    collected = collect_sitemap_urls(
        _WholeButBrokenFetcher(),  # type: ignore[arg-type]
        [f"https://{DOMAIN}/sitemap.xml"],
    )

    assert len(collected.entries) == 2
    assert collected.partial is True
    assert any("malformed XML tail" in w for w in collected.warnings)
    assert not any("cut short" in w for w in collected.warnings)


def _tls_connect_error(message: str, *, verification: bool = False) -> Exception:
    """An httpx ConnectError carrying an SSL cause, as httpx raises it."""

    import ssl

    import httpx

    cause: Exception = (
        ssl.SSLCertVerificationError(1, message) if verification else ssl.SSLError(1, message)
    )
    error = httpx.ConnectError(str(cause))
    error.__cause__ = cause
    return error


def test_a_certificate_failure_is_named_accurately_and_not_retried(monkeypatch) -> None:
    import httpx
    from app.services.research.workers._website import fetcher as fetcher_module
    from app.services.research.workers._website.config import CrawlerConfig
    from app.services.research.workers._website.fetcher import TLS_CERTIFICATE, HttpFetcher

    attempts: list[str] = []
    monkeypatch.setattr(fetcher_module.time, "sleep", lambda _s: None)

    def _handler(request: httpx.Request) -> httpx.Response:
        attempts.append(str(request.url))
        raise _tls_connect_error(
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self signed certificate",
            verification=True,
        )

    fetcher = HttpFetcher(
        CrawlerConfig(max_retries=2),
        resolver=_public_resolver,
        transport=httpx.MockTransport(_handler),
    )
    try:
        result = fetcher.fetch("https://public.example/")
    finally:
        fetcher.close()

    assert result.ok is False
    assert result.error_kind == TLS_CERTIFICATE
    assert "TLS certificate error" in (result.error or "")
    assert len(attempts) == 1, "a rejected certificate is deterministic; retrying spends budget"


def test_a_truncated_handshake_is_retried_within_the_existing_bound(monkeypatch) -> None:
    """The autowhale.io case: an EOF mid-handshake is not a certificate fault."""

    import httpx
    from app.services.research.workers._website import fetcher as fetcher_module
    from app.services.research.workers._website.config import CrawlerConfig
    from app.services.research.workers._website.fetcher import TLS_HANDSHAKE, HttpFetcher

    attempts: list[str] = []
    monkeypatch.setattr(fetcher_module.time, "sleep", lambda _s: None)

    def _handler(request: httpx.Request) -> httpx.Response:
        attempts.append(str(request.url))
        raise _tls_connect_error(
            "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol"
        )

    fetcher = HttpFetcher(
        CrawlerConfig(max_retries=2),
        resolver=_public_resolver,
        transport=httpx.MockTransport(_handler),
    )
    try:
        result = fetcher.fetch("https://public.example/")
    finally:
        fetcher.close()

    assert result.ok is False
    assert result.error_kind == TLS_HANDSHAKE
    assert "certificate" not in (result.error or "").lower()
    # The bound is the configured one: three attempts for max_retries=2, no more.
    assert len(attempts) == 3


def test_a_transient_handshake_failure_can_still_succeed(monkeypatch) -> None:
    import httpx
    from app.services.research.workers._website import fetcher as fetcher_module
    from app.services.research.workers._website.config import CrawlerConfig
    from app.services.research.workers._website.fetcher import HttpFetcher

    attempts: list[str] = []
    monkeypatch.setattr(fetcher_module.time, "sleep", lambda _s: None)

    def _handler(request: httpx.Request) -> httpx.Response:
        attempts.append(str(request.url))
        if len(attempts) == 1:
            raise _tls_connect_error(
                "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol"
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text="<html><body>hello</body></html>",
            request=request,
        )

    fetcher = HttpFetcher(
        CrawlerConfig(max_retries=2),
        resolver=_public_resolver,
        transport=httpx.MockTransport(_handler),
    )
    try:
        result = fetcher.fetch("https://public.example/")
    finally:
        fetcher.close()

    assert result.ok is True
    assert len(attempts) == 2


def test_verification_is_never_relaxed_to_get_past_a_certificate() -> None:
    """No code path may turn certificate checking off."""

    import inspect

    from app.services.research.workers._website import fetcher as fetcher_module

    source = inspect.getsource(fetcher_module)

    assert "verify=False" not in source
    assert "CERT_NONE" not in source
    assert "check_hostname = False" not in source


# --- apex/www policy deferral -------------------------------------------------


def _twin_resolver(host: str, _port: int) -> tuple[str, ...]:
    assert host in {"public.example", "www.public.example"}
    return ("93.184.216.34",)


def test_an_apex_to_www_redirect_is_reported_as_a_deferral_not_a_failure() -> None:
    import httpx
    from app.services.research.workers._website.config import CrawlerConfig
    from app.services.research.workers._website.fetcher import HOST_POLICY_DEFERRAL, HttpFetcher

    calls: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            301,
            headers={"location": "https://www.public.example/robots.txt"},
            request=request,
        )

    fetcher = HttpFetcher(
        CrawlerConfig(max_retries=0),
        resolver=_twin_resolver,
        transport=httpx.MockTransport(_handler),
    )
    try:
        result = fetcher.fetch("https://public.example/robots.txt", expect_html=False)
    finally:
        fetcher.close()

    # Behaviour is unchanged: the redirect is still not followed.
    assert result.ok is False
    assert calls == ["https://public.example/robots.txt"]
    # Only the description changed.
    assert result.error_kind == HOST_POLICY_DEFERRAL
    assert "not allowed" not in (result.error or "")
    assert "robots policy" in (result.error or "")


def test_a_genuinely_foreign_redirect_is_still_refused_outright() -> None:
    import httpx
    from app.services.research.workers._website.config import CrawlerConfig
    from app.services.research.workers._website.fetcher import UNSAFE_TARGET, HttpFetcher

    def _resolver(host: str, _port: int) -> tuple[str, ...]:
        assert host in {"public.example", "elsewhere.example"}
        return ("93.184.216.34",)

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            301,
            headers={"location": "https://elsewhere.example/"},
            request=request,
        )

    fetcher = HttpFetcher(
        CrawlerConfig(max_retries=0),
        resolver=_resolver,
        transport=httpx.MockTransport(_handler),
    )
    try:
        result = fetcher.fetch("https://public.example/")
    finally:
        fetcher.close()

    assert result.ok is False
    assert result.error_kind == UNSAFE_TARGET
    assert "not allowed" in (result.error or "")


def test_a_deferral_still_denies_crawling_but_without_alarm(caplog) -> None:
    """Safety is unchanged; only the operator-facing wording is calmer."""

    import logging

    from app.services.research.workers._website.fetcher import HOST_POLICY_DEFERRAL
    from app.services.research.workers._website.models import FetchResult
    from app.services.research.workers._website.robots import fetch_robots

    class _DeferringFetcher:
        def fetch(self, url: str, expect_html: bool = True) -> FetchResult:
            del expect_html
            return FetchResult(
                requested_url=url,
                final_url=url,
                status_code=None,
                ok=False,
                method="http",
                error=(
                    "public.example redirects to www.public.example; not following it "
                    "under public.example's robots policy, www.public.example's will "
                    "be loaded first"
                ),
                error_kind=HOST_POLICY_DEFERRAL,
            )

    with caplog.at_level(logging.INFO):
        policy = fetch_robots(_DeferringFetcher(), "https://public.example/")  # type: ignore[arg-type]

    # The policy genuinely was not read, so crawling is still denied.
    assert policy.fetched is False
    assert policy.can_fetch("https://public.example/") is False
    # But nothing in the record calls this a refusal or a failure.
    assert logging.WARNING not in {record.levelno for record in caplog.records}
    assert any("defers to another host" in record.message for record in caplog.records)


def test_a_real_robots_failure_is_still_a_warning(caplog) -> None:
    import logging

    from app.services.research.workers._website.models import FetchResult
    from app.services.research.workers._website.robots import fetch_robots

    class _BrokenFetcher:
        def fetch(self, url: str, expect_html: bool = True) -> FetchResult:
            del expect_html
            return FetchResult(
                requested_url=url,
                final_url=url,
                status_code=None,
                ok=False,
                method="http",
                error="timeout",
            )

    with caplog.at_level(logging.INFO):
        policy = fetch_robots(_BrokenFetcher(), "https://public.example/")  # type: ignore[arg-type]

    assert policy.can_fetch("https://public.example/") is False
    assert any(record.levelno == logging.WARNING for record in caplog.records)


# --- the collector end to end -------------------------------------------------


def test_a_partial_sitemap_still_feeds_the_crawl_and_is_recorded() -> None:
    """The whole defect, from cut response to gathered pages.

    Before the fix this run reached exactly one page: the sitemap parsed to
    zero URLs, discovery fell back to homepage links alone, and nothing in the
    outcome said the list had been thrown away.
    """

    from app.services.research.workers._website.collect import collect
    from app.services.research.workers._website.config import (
        AppConfig,
        CrawlerConfig,
        DiscoveryConfig,
    )
    from app.services.research.workers._website.models import FetchResult

    host = "engines.example"
    sitemap = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"  <url><loc>https://{host}/about</loc></url>\n"
        f"  <url><loc>https://{host}/products</loc></url>\n"
        f"  <url><loc>https://{host}/cont"  # cut mid-element by the size cap
    )

    def _page(title: str, body: str) -> str:
        return (
            f"<html><head><title>{title}</title></head><body><main><h1>{title}</h1>"
            f"<p>{body}</p></main></body></html>"
        )

    responses: dict[str, tuple[int, str, bool]] = {
        f"https://{host}/robots.txt": (
            200,
            f"User-agent: *\nAllow: /\nSitemap: https://{host}/sitemap.xml\n",
            False,
        ),
        f"https://{host}/sitemap.xml": (200, sitemap, True),
        f"https://{host}/": (
            200,
            _page(
                "Analytical Engines Ltd",
                "Analytical Engines Ltd builds precision measurement hardware for "
                "industrial laboratories across the United Kingdom and has done so "
                "since nineteen eighty four from its works in Manchester.",
            ),
            False,
        ),
        f"https://{host}/about": (
            200,
            _page(
                "About Analytical Engines Ltd",
                "Analytical Engines Ltd employs one hundred and forty people and "
                "serves laboratory customers throughout Europe from Manchester.",
            ),
            False,
        ),
        f"https://{host}/products": (
            200,
            _page(
                "Products",
                "Our calibration benches and precision measurement instruments are "
                "used by industrial laboratories for routine verification work.",
            ),
            False,
        ),
    }

    requested: list[str] = []

    class _ScriptedFetcher:
        max_bytes = 8 * 1024 * 1024

        def fetch(self, url: str, expect_html: bool = True) -> FetchResult:
            requested.append(url)
            status, body, truncated = responses.get(url, (404, "", False))
            if status != 200:
                return FetchResult(
                    requested_url=url,
                    final_url=url,
                    status_code=status,
                    ok=False,
                    method="http",
                    error=f"HTTP {status}",
                )
            return FetchResult(
                requested_url=url,
                final_url=url,
                status_code=200,
                ok=True,
                method="http",
                content_type="text/html" if expect_html else "application/xml",
                html=body,
                truncated=truncated,
            )

        def close(self) -> None:
            return None

    config = AppConfig(
        crawler=CrawlerConfig(delay_between_requests_seconds=0.0, max_retries=0),
        discovery=DiscoveryConfig(max_sitemap_urls=500),
    )

    outcome = collect(host, config, fetcher=_ScriptedFetcher())  # type: ignore[arg-type]

    # The complete entries survived the cut and were actually crawled.
    fetched = set(requested)
    assert f"https://{host}/about" in fetched
    assert f"https://{host}/products" in fetched
    # The fragment of the entry that was cut was never turned into a request.
    assert not any(url.endswith("/cont") for url in requested)
    assert len(outcome.pages) > 1
    # And the run says the list it worked from was partial.
    assert any("cut short" in warning for warning in outcome.warnings)
    assert outcome.unreachable is False
