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
