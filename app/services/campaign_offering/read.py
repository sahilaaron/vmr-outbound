"""What the customer is shown about their Campaign's offering.

The screen is the reason this module is separate from
:mod:`~app.services.campaign_offering.jobs`. The durable row carries a lease
owner, an attempt count, a failure code, a producer version and an idempotency
key, and a customer must see none of that — those are diagnostics, and
``docs/AGENTS.md`` keeps them on the Admin side. What a customer sees is where
their offering has got to, in four steps, and what it says.

So this projects. It reads the run and returns a frozen view whose fields are all
safe to render: labels, product sentences, and the structured offering itself,
which is the thing they asked for. Nothing here can leak a queue id or a stack
trace because nothing here reads one.

The four progress steps are the product's own words. ``QUEUED`` and ``READING``
both sit on step one, which is honest: the run is claimed within a poll and the
long part of it genuinely is the page read. ``ANALYZING`` and ``CONNECTING`` are
steps two and three and are quick — they are still distinct states because a run
that dies in one of them should be able to say which.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.campaign import Campaign
from app.models.campaign_offering_research import CampaignOfferingResearch
from app.models.enums import CampaignOfferingResearchStatus, CampaignOfferingSource
from app.services.campaign_offering import jobs
from app.services.campaign_offering.contracts import (
    OfferingIntelligence,
    offering_from_stored,
)

#: The one sentence a customer sees when research could not produce an offering.
#: Deliberately identical for every failure cause: the causes differ in ways only
#: an administrator can act on, and offering the customer six variations of "it
#: did not work" is not more truthful, only less usable.
FAILURE_MESSAGE = "Could not prepare this offering — using VMI for now."

PROGRESS_STEPS: tuple[str, ...] = (
    "Reading page",
    "Understanding offering",
    "Connecting it to your company",
    "Ready",
)

_STEP_INDEX: dict[CampaignOfferingResearchStatus, int] = {
    CampaignOfferingResearchStatus.QUEUED: 0,
    CampaignOfferingResearchStatus.READING: 0,
    CampaignOfferingResearchStatus.ANALYZING: 1,
    CampaignOfferingResearchStatus.CONNECTING: 2,
    CampaignOfferingResearchStatus.READY: 3,
}

_IN_FLIGHT_MESSAGE = "Preparing this offering. Emails wait until it is ready."


@dataclass(frozen=True)
class OfferingStep:
    """One of the four progress steps, and where the run stands against it."""

    label: str
    state: str  # "done" | "active" | "pending"


@dataclass(frozen=True)
class CampaignOfferingView:
    """Everything Campaign Setup renders about the offering. Customer-safe."""

    mode: CampaignOfferingSource
    #: The URL of the most recent run, whatever became of it. Shown so "Change
    #: URL" has something to change.
    source_url: str | None = None
    status: CampaignOfferingResearchStatus | None = None
    version_number: int | None = None
    #: The structured offering currently leading, when one is.
    offering: OfferingIntelligence | None = None
    #: True while a run is in flight — the state in which emails wait.
    in_flight: bool = False
    #: True when the latest run failed and nothing is current: the Campaign is
    #: using its Library offering and has been told so.
    failed: bool = False
    #: True when a previous version is still leading while a newer run is going.
    #: The distinction matters on screen: this Campaign is *not* waiting.
    reanalyzing: bool = False
    steps: tuple[OfferingStep, ...] = field(default_factory=tuple)
    message: str = ""

    @property
    def is_url_mode(self) -> bool:
        return self.mode is CampaignOfferingSource.URL_RESEARCH

    @property
    def has_offering(self) -> bool:
        return self.offering is not None

    @property
    def has_history(self) -> bool:
        """Whether anything has ever been attempted, so Setup can offer Re-analyze."""

        return self.status is not None


def _steps(status: CampaignOfferingResearchStatus | None) -> tuple[OfferingStep, ...]:
    if status is None or status not in _STEP_INDEX:
        return ()
    reached = _STEP_INDEX[status]
    return tuple(
        OfferingStep(
            label=label,
            state="done" if index < reached else ("active" if index == reached else "pending"),
        )
        for index, label in enumerate(PROGRESS_STEPS)
    )


def campaign_offering_view(session: Session, campaign: Campaign) -> CampaignOfferingView:
    """Project one Campaign's offering state for the customer screen."""

    if campaign.offering_source is not CampaignOfferingSource.URL_RESEARCH:
        # Library mode. The latest run, if there ever was one, is deliberately not
        # reported: the Campaign is not using it, and showing it would be a
        # second answer to "what is this Campaign selling?".
        return CampaignOfferingView(mode=campaign.offering_source)

    latest = jobs.latest_run(session, campaign_id=campaign.id)
    current = jobs.current_version(session, campaign_id=campaign.id)
    offering = offering_from_stored(current.offering_context) if current is not None else None

    if latest is None:  # pragma: no cover - request_research writes both together
        return CampaignOfferingView(mode=campaign.offering_source)

    in_flight = latest.is_active
    failed = (
        not in_flight
        and offering is None
        and latest.status
        in (
            CampaignOfferingResearchStatus.FAILED,
            CampaignOfferingResearchStatus.CANCELLED,
        )
    )
    reanalyzing = in_flight and offering is not None

    if offering is not None and not in_flight:
        message = ""
    elif reanalyzing:
        message = "Preparing a new version. The current offering is still in use until it is ready."
    elif in_flight:
        message = _IN_FLIGHT_MESSAGE
    elif failed:
        message = FAILURE_MESSAGE
    else:  # pragma: no cover - defensive
        message = ""

    return CampaignOfferingView(
        mode=campaign.offering_source,
        source_url=latest.source_url,
        status=latest.status,
        version_number=latest.version_number,
        offering=offering,
        in_flight=in_flight,
        failed=failed,
        reanalyzing=reanalyzing,
        steps=_steps(latest.status),
        message=message,
    )


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OfferingRunDiagnostic:
    """One run as an administrator sees it. Never rendered to a customer."""

    id: uuid.UUID
    version_number: int
    status: CampaignOfferingResearchStatus
    source_url: str
    is_current: bool
    attempts: int
    max_attempts: int
    failure_code: str | None
    failure_reason: str | None
    producer: str | None
    producer_version: str | None
    producer_model: str | None
    context_digest: str | None
    requested_by: str | None
    requested_at: str
    completed_at: str | None


def _stamp(value: object) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None


def _diagnostic(run: CampaignOfferingResearch) -> OfferingRunDiagnostic:
    return OfferingRunDiagnostic(
        id=run.id,
        version_number=run.version_number,
        status=run.status,
        source_url=run.source_url,
        is_current=run.is_current,
        attempts=run.attempts,
        max_attempts=run.max_attempts,
        failure_code=run.failure_code,
        failure_reason=run.failure_reason,
        producer=run.producer,
        producer_version=run.producer_version,
        producer_model=run.producer_model,
        context_digest=run.context_digest,
        requested_by=run.requested_by,
        requested_at=_stamp(run.requested_at) or "",
        completed_at=_stamp(run.completed_at),
    )


def admin_history(
    session: Session, *, campaign_id: uuid.UUID, limit: int = 20
) -> tuple[OfferingRunDiagnostic, ...]:
    """Every run for one Campaign, newest first, for Admin diagnostics."""

    return tuple(
        _diagnostic(run) for run in jobs.history(session, campaign_id=campaign_id, limit=limit)
    )
