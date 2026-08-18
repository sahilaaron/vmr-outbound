"""Execute one Campaign offering-research run end to end.

The only place in this package that reaches a language model, and the seam is
the one already in the repository: a
:class:`~app.services.thinking.contracts.Thinker`, injected, so the automated
suite scripts an answer and never shells out.

Four properties, each of which is a rule something else depends on:

**The model reads one page, with fetch and nothing else.** ``allowed_tools`` comes
from ``Settings.campaign_offering_allowed_tools`` and defaults to ``WebFetch``
alone. No search: this run is about the page the operator pointed at, and giving
it search would give it a way to answer about a different one.

**A structurally invalid answer is a failure, not a thin success.** The payload
goes through ``contracts.parse_offering_payload``, which refuses a missing
offering name, an empty summary, an absent connection to the seller, and the
model's own "I could not read this". Nothing is defaulted in to make a run
succeed.

**Nothing runs unless it is switched on.** ``campaign_offering_research`` is
default-off and read through ``operations.settings`` — the effective value an
administrator sees, not the raw environment flag. A run claimed while the control
is off fails with a stated reason rather than sitting in the queue looking
healthy.

**Success releases the hold; so does the last failure.** Both are the moment
``consistency.offering_context_hold`` starts answering ``None``, and both call
``release_hold`` so held memberships are queued again in the same transaction
that closed the run. Forgetting either would leave a Campaign paused with nothing
left to wait for.

The prompt and the raw answer are not persisted. The validated structure is the
record; a digest makes two runs comparable without storing the model's wording
twice.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.campaign import Campaign
from app.models.campaign_offering_research import CampaignOfferingResearch
from app.services.campaign_offering import consistency, jobs, prompts
from app.services.campaign_offering.contracts import (
    CONTEXT_POLICY_VERSION,
    OfferingResearchMalformed,
    parse_offering_payload,
)
from app.services.operations import settings as operational
from app.services.seller import campaign_offerings as seller_campaign_offerings
from app.services.seller import context as seller_context
from app.services.seller import effective as effective_offering
from app.services.thinking.claude_cli import ClaudeCliThinker
from app.services.thinking.contracts import Thinker, ThinkingError, ThinkingRequest

PRODUCER = "campaign-offering-research"
FEATURE_KEY = "campaign_offering_research"
FEATURE_DISABLED_CODE = "feature_disabled"
CAMPAIGN_MISSING_CODE = "campaign_missing"

ThinkerFactory = Callable[[Settings], Thinker]


def default_thinker_factory(settings: Settings) -> Thinker:
    return ClaudeCliThinker(settings=settings)


@dataclass(frozen=True)
class RunOutcome:
    """What executing one run did, in the vocabulary the queue records."""

    succeeded: bool
    campaign_id: uuid.UUID
    research_id: uuid.UUID
    version_number: int
    code: str
    message: str
    retryable: bool = False

    def as_line(self) -> dict[str, Any]:
        return {
            "queue": "campaign_offering_research",
            "campaign_id": str(self.campaign_id),
            "research_id": str(self.research_id),
            "version": self.version_number,
            "status": "succeeded" if self.succeeded else "failed",
            "outcome": self.code,
            "message": self.message,
        }


#: What the customer is told when a run could not produce an offering. One
#: sentence, product vocabulary, no code and no diagnostics — those stay on the
#: run for Admin. Every failure path maps here; the differences between them are
#: recorded, not displayed.
CUSTOMER_FAILURE_MESSAGE = "Could not prepare this offering — using VMI for now."


def _supporting_offering_id(session: Session, *, campaign_id: uuid.UUID) -> uuid.UUID | None:
    """The Library offering that will sit under the researched one, if any.

    Recorded on the version so an audit can reconstruct the whole pitch. A
    reference only — the Library row is not read for its contents here and is
    never written.
    """

    linked = seller_campaign_offerings.offerings_for_campaign(session, campaign_id)
    return linked[0].id if linked else None


def execute_run(
    session: Session,
    *,
    run: CampaignOfferingResearch,
    thinker_factory: ThinkerFactory | None = None,
    settings: Settings | None = None,
    worker_id: str = "worker",
) -> RunOutcome:
    """Read the page, validate the answer, and close the run truthfully.

    The run arrives already claimed and already at ``READING`` — the claim is a
    separate committed checkpoint precisely so the customer sees that status
    while this function is inside the model call.
    """

    resolved_settings = settings or get_settings()
    campaign = session.get(Campaign, run.campaign_id)
    if campaign is None:  # pragma: no cover - protected by FK
        return _fail(
            session,
            run=run,
            code=CAMPAIGN_MISSING_CODE,
            message="the Campaign this offering was queued for no longer exists",
            retryable=False,
            worker_id=worker_id,
            campaign=None,
        )

    if not operational.enabled(session, FEATURE_KEY, resolved_settings):
        return _fail(
            session,
            run=run,
            code=FEATURE_DISABLED_CODE,
            message=(
                operational.refusal(session, FEATURE_KEY, resolved_settings)
                or "Campaign offering research is switched off."
            ),
            # Not retryable: a switched-off control is a decision, and burning
            # the attempt budget against it only delays telling the operator.
            retryable=False,
            worker_id=worker_id,
            campaign=campaign,
        )

    seller = seller_context.assemble(session, campaign_id=campaign.id)
    thinker = (thinker_factory or default_thinker_factory)(resolved_settings)
    request = ThinkingRequest(
        prompt=prompts.campaign_offering_prompt(
            url=run.source_url,
            # The Library half only. A run must never be shown a previous
            # researched offering as "what we sell": it would read as first-party
            # seller knowledge, and the next version would be a summary of the
            # last one rather than of the page.
            seller_summary=effective_offering.library_summary(seller),
        ),
        purpose="campaign_offering_research",
        timeout_seconds=float(resolved_settings.campaign_offering_timeout_seconds),
        allowed_tools=tuple(resolved_settings.campaign_offering_allowed_tools),
    )

    try:
        answer = thinker.think(request)
    except ThinkingError as exc:
        return _fail(
            session,
            run=run,
            code=exc.code,
            # ``exc.message`` is the seam's own sanitized sentence, already
            # truncated. It is stored for Admin; the customer sees the product
            # sentence above.
            message=exc.message,
            retryable=exc.retryable,
            worker_id=worker_id,
            campaign=campaign,
        )

    jobs.mark_analyzing(session, run=run)
    try:
        offering = parse_offering_payload(answer.payload)
    except OfferingResearchMalformed as exc:
        return _fail(
            session,
            run=run,
            code=exc.code,
            message=exc.message,
            # A page that cannot be read will not become readable on a retry, and
            # neither will a page with no offering on it. A merely malformed
            # answer is a one-off and is worth one more call.
            retryable=exc.code not in {"page_unreadable"},
            worker_id=worker_id,
            campaign=campaign,
        )

    jobs.mark_connecting(session, run=run)
    jobs.mark_ready(
        session,
        run=run,
        offering_context=offering.to_payload(),
        context_digest=offering.digest(),
        context_policy_version=CONTEXT_POLICY_VERSION,
        producer=PRODUCER,
        producer_version=resolved_settings.campaign_offering_producer_version,
        producer_model=answer.producer_version,
        supporting_offering_id=_supporting_offering_id(session, campaign_id=campaign.id),
        actor=worker_id,
    )
    # The Campaign now has a current version, so the hold this run was the reason
    # for no longer answers. Releasing it here, in the same transaction, is what
    # stops a prepared Campaign sitting paused until somebody touches a control.
    consistency.release_hold(session, campaign=campaign, actor=worker_id)
    return RunOutcome(
        succeeded=True,
        campaign_id=campaign.id,
        research_id=run.id,
        version_number=run.version_number,
        code="offering_ready",
        message=f"{offering.offering_name} is this Campaign's primary offering",
    )


def _fail(
    session: Session,
    *,
    run: CampaignOfferingResearch,
    code: str,
    message: str,
    retryable: bool,
    worker_id: str,
    campaign: Campaign | None,
) -> RunOutcome:
    jobs.mark_failed(
        session,
        run=run,
        code=code,
        message=message,
        retryable=retryable,
        actor=worker_id,
    )
    # A run that is only *scheduled* to retry is still active, so the hold still
    # applies and there is nothing to release. A terminal failure with no current
    # version is the fallback state, and the Campaign has to be let go.
    if campaign is not None and not run.is_active:
        consistency.release_hold(session, campaign=campaign, actor=worker_id)
    return RunOutcome(
        succeeded=False,
        campaign_id=run.campaign_id,
        research_id=run.id,
        version_number=run.version_number,
        code=code,
        message=message,
        retryable=retryable,
    )


def run_next(
    session: Session,
    *,
    worker_id: str,
    lease_seconds: float = 420.0,
    thinker_factory: ThinkerFactory | None = None,
    settings: Settings | None = None,
) -> RunOutcome | None:
    """Claim and execute at most one run. Returns None when the queue is idle.

    Provided for tests and for a caller that is content to hold one transaction
    across the model call. The worker uses :func:`claim_next_run` and this
    function's body separately, so ``READING`` is committed before the call
    starts — see ``scripts/run_agent_worker.py``.
    """

    run = jobs.claim_next(session, worker_id=worker_id, lease_seconds=lease_seconds)
    if run is None:
        return None
    return execute_run(
        session,
        run=run,
        thinker_factory=thinker_factory,
        settings=settings,
        worker_id=worker_id,
    )
