"""The deterministic company-website worker.

Wraps the vendored collector and translates its output into the shared
worker contract. There is no model call and no inference here: every fact
is an explicit statement, a structured-data value (JSON-LD / Open Graph),
or a clearly-labelled heuristic signal, and every one of them carries the
page it was read from.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.services.research.contracts import (
    MAX_EXCERPT_LENGTH,
    MAX_VALUE_LENGTH,
    ResearchRequest,
    ResearchWorkerError,
    SourcedFact,
    WorkerResult,
)
from app.services.research.workers._website import __version__ as VENDOR_VERSION
from app.services.research.workers._website.collect import CollectionOutcome, collect
from app.services.research.workers._website.config import AppConfig, CrawlerConfig, DiscoveryConfig
from app.services.research.workers._website.domain import DomainError
from app.services.research.workers._website.models import Fact

WORKER_NAME = "website"

# Confidence the vendored extractor assigns per fact type. A heuristic
# signal must never present itself as an explicit statement.
_MIN_CONFIDENCE = 0.0
_MAX_CONFIDENCE = 1.0


def _config(request: ResearchRequest) -> AppConfig:
    """Build the crawl configuration from the Agent control's options.

    Defaults are the prototype's: bounded, polite, robots-respecting. An
    operator may tighten them; the guard below stops anyone widening the
    crawl past what this repository considers acceptable.
    """

    options = request.options
    crawler = CrawlerConfig(
        max_pages=min(int(options.get("max_pages", 25)), 25),
        max_depth=min(int(options.get("max_depth", 3)), 3),
        request_timeout_seconds=float(options.get("request_timeout_seconds", 20.0)),
        delay_between_requests_seconds=max(
            float(options.get("delay_between_requests_seconds", 1.0)), 1.0
        ),
        # Never available in this build; a browser is not an approved
        # dependency and the vendored fallback is not wired in.
        use_playwright_fallback=False,
    )
    discovery = DiscoveryConfig(
        use_sitemap=bool(options.get("use_sitemap", True)),
        # Not configurable. robots.txt is always fetched and always obeyed.
        use_robots_txt=True,
        include_subdomains=bool(options.get("include_subdomains", True)),
    )
    return AppConfig(crawler=crawler, discovery=discovery)


def _clamp(value: float) -> float:
    return max(_MIN_CONFIDENCE, min(_MAX_CONFIDENCE, float(value)))


def _to_sourced_fact(fact: Fact) -> SourcedFact | None:
    """Translate one vendored fact, or drop it if it cannot be evidenced.

    A fact without an absolute source URL is unusable downstream, so it is
    discarded here rather than stored as an unsupported claim.
    """

    if not fact.source_url or not fact.source_url.startswith(("http://", "https://")):
        return None
    value = str(fact.value).strip()
    if not value:
        return None

    try:
        retrieved_at = datetime.fromisoformat(fact.retrieved_at)
    except (TypeError, ValueError):
        retrieved_at = datetime.now(UTC)
    if retrieved_at.tzinfo is None:
        retrieved_at = retrieved_at.replace(tzinfo=UTC)

    excerpt = (fact.supporting_text or "").strip() or None
    try:
        return SourcedFact(
            field=fact.field,
            value=value[:MAX_VALUE_LENGTH],
            source_url=fact.source_url,
            retrieved_at=retrieved_at,
            # Records both how it was extracted and how strong that is, so a
            # heuristic can never later be read as an explicit statement.
            extraction_method=f"{fact.extraction_method}:{fact.fact_type}",
            confidence=_clamp(fact.confidence),
            excerpt=excerpt[:MAX_EXCERPT_LENGTH] if excerpt else None,
        )
    except ValueError:
        return None


def _raw_payload(outcome: CollectionOutcome) -> dict[str, Any]:
    """The verbatim record of what was read, preserved for re-derivation."""

    return {
        "worker": WORKER_NAME,
        "worker_version": VENDOR_VERSION,
        "domain": outcome.domain,
        "started_at": outcome.started_at,
        "completed_at": outcome.completed_at,
        "pages": [
            {
                "url": page.url,
                "title": page.title,
                "page_type": page.page_type,
                "retrieval_method": page.retrieval_method,
                "retrieved_at": page.retrieved_at,
                "text_length": page.text_length,
            }
            for page in outcome.pages
        ],
        "facts": [fact.to_dict() for fact in outcome.facts],
        "errors": outcome.errors,
        "warnings": outcome.warnings,
    }


class WebsiteWorker:
    """Reads a company's own website and returns what it explicitly says."""

    name = WORKER_NAME
    version = VENDOR_VERSION

    def __init__(self, *, collector: Any = None) -> None:
        # Injection seam for tests: the suite must never reach the network.
        self._collect = collector if collector is not None else collect

    def run(self, request: ResearchRequest) -> WorkerResult:
        if not request.domain or not request.domain.strip():
            raise ResearchWorkerError(
                "no domain to research", code="domain_missing", retryable=False
            )

        try:
            outcome = self._collect(request.domain, _config(request))
        except DomainError as exc:
            raise ResearchWorkerError(
                f"unusable domain: {exc}", code="domain_invalid", retryable=False
            ) from exc
        except Exception as exc:  # network/parse faults are worth one retry
            raise ResearchWorkerError(
                f"{type(exc).__name__}: {exc}", code="collection_failed", retryable=True
            ) from exc

        if outcome.unreachable:
            # Reaching nothing is a dead end for this domain, not a transient
            # fault: retrying an unreachable or parked site changes nothing.
            raise ResearchWorkerError(
                outcome.unreachable_reason or "site unreachable",
                code="site_unreachable",
                retryable=False,
            )

        facts = tuple(
            sourced
            for sourced in (_to_sourced_fact(fact) for fact in outcome.facts)
            if sourced is not None
        )
        dropped = len(outcome.facts) - len(facts)
        warnings = list(outcome.warnings)
        if dropped:
            warnings.append(f"{dropped} extracted fact(s) dropped: no usable source URL")

        return WorkerResult(
            worker=self.name,
            worker_version=self.version,
            facts=facts,
            warnings=tuple(warnings),
            raw=_raw_payload(outcome),
            sufficient=outcome.sufficient and bool(facts),
        )
