"""The contract every research worker speaks.

One Research Agent execution fans out to one or more *workers*. A worker
knows how to gather sourced facts about a company from one source. It
knows nothing about jobs, retries, Postgres, campaigns or evidence
tables -- the Agent owns all of that.

Keeping the contract this narrow is what makes workers pluggable: a new
source becomes a new module implementing :class:`ResearchWorker`, and
nothing else in the pipeline changes. See ``docs/RESEARCH_WORKERS.md``.

Every fact a worker returns must carry the URL it came from and when it
was read. A worker that cannot say where a value came from must not
return it -- an unsupported claim is refused, never softened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

# Facts are keyed by field name so several workers can describe the same
# company without colliding. Unknown field names are allowed: the Agent
# stores them as claims and never invents meaning for them.
MAX_VALUE_LENGTH = 2000
MAX_EXCERPT_LENGTH = 1000


class ResearchWorkerError(Exception):
    """A worker failed in a way the Agent should classify.

    ``retryable`` distinguishes a transient condition (timeout, 503) from
    a dead end (invalid domain). The Agent maps this onto the shared
    Agent error vocabulary; workers never raise adapter exceptions.
    """

    def __init__(self, message: str, *, code: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class ResearchRequest:
    """Everything a worker is allowed to know about the subject."""

    domain: str
    company_name: str | None = None
    timeout_seconds: float = 120.0
    # Per-worker configuration from the Agent control's config blob, so an
    # operator can tune one worker without touching the others.
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourcedFact:
    """One claim, and the evidence for it.

    Maps onto the INS-001 evidence model: ``value`` becomes the claim
    text, and the remaining attributes become one ``EvidenceInput``.
    """

    field: str
    value: str
    source_url: str
    retrieved_at: datetime
    extraction_method: str
    confidence: float
    excerpt: str | None = None
    published_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.field.strip():
            raise ValueError("fact field must not be blank")
        if not self.value.strip():
            raise ValueError("fact value must not be blank")
        if not self.source_url.startswith(("http://", "https://")):
            raise ValueError(f"source_url must be absolute http(s): {self.source_url!r}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be within [0, 1]: {self.confidence}")
        if self.retrieved_at.tzinfo is None:
            raise ValueError("retrieved_at must be timezone-aware")
        if len(self.value) > MAX_VALUE_LENGTH:
            raise ValueError(f"fact value exceeds {MAX_VALUE_LENGTH} characters")
        if self.excerpt is not None and len(self.excerpt) > MAX_EXCERPT_LENGTH:
            raise ValueError(f"excerpt exceeds {MAX_EXCERPT_LENGTH} characters")


@dataclass(frozen=True)
class WorkerResult:
    """What one worker found, including when it found little.

    ``sufficient=False`` is a legitimate, non-failing outcome. A company
    with a thin website is a fact about that company, not an error.
    """

    worker: str
    worker_version: str
    facts: tuple[SourcedFact, ...] = ()
    warnings: tuple[str, ...] = ()
    # Preserved verbatim through ``dossiers.submit`` so a later policy
    # change can be re-derived without re-crawling anyone.
    raw: dict[str, Any] = field(default_factory=dict)
    sufficient: bool = True

    @property
    def empty(self) -> bool:
        return not self.facts


@runtime_checkable
class ResearchWorker(Protocol):
    """A source of sourced company facts.

    Implementations must be safe to call concurrently for different
    domains, must respect ``request.timeout_seconds``, and must never
    raise anything other than :class:`ResearchWorkerError` for expected
    failures.
    """

    name: str
    version: str

    def run(self, request: ResearchRequest) -> WorkerResult: ...
