"""The durable queue for Campaign offering research, and the version ledger.

The run *is* the version, so this module owns both jobs: leasing work to a
worker, and deciding which version a Campaign is currently leading with. Keeping
them together is what makes the promotion atomic — a version becomes current in
the same transaction that closes its run, so there is no window in which a run
has succeeded and the Campaign has not noticed.

Three rules are enforced here and repeated nowhere else:

* **One run in flight per Campaign.** A partial unique index says so; this module
  reads it first and reports "already preparing" rather than racing it.
* **Only success promotes.** :func:`mark_ready` is the sole writer of
  ``is_current``, and it demotes the previous current version in the same
  statement pair. :func:`mark_failed` never touches it, which is what makes a
  failed re-analysis harmless to a Campaign that already had a good answer.
* **Nothing is mutated in place.** Re-analyze and Change URL both call
  :func:`request_research`, which inserts the next version.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.campaign import Campaign
from app.models.campaign_offering_research import (
    ACTIVE_RESEARCH_STATUSES,
    CLAIMABLE_RESEARCH_STATUSES,
    CampaignOfferingResearch,
)
from app.models.enums import CampaignOfferingResearchStatus, CampaignOfferingSource
from app.services.audit import record_audit_event
from app.services.campaign_offering.urls import OfferingUrlError, normalize_offering_url

JOB_ACTOR = "system:campaign-offering-research"

DEFAULT_MAX_ATTEMPTS = 2
RETRY_BASE_SECONDS = 45.0
RETRY_CAP_SECONDS = 600.0


class OfferingResearchError(Exception):
    """A queue or version operation that cannot be performed as asked."""

    def __init__(self, message: str, *, code: str = "offering_research_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _now() -> datetime:
    return datetime.now(UTC)


def idempotency_key(*, campaign_id: uuid.UUID, version_number: int) -> str:
    """One key per (Campaign, version).

    Not keyed on the URL. Re-analysing the *same* address is a legitimate,
    deliberate second question — the page may have changed, or the first answer
    may have been thin — so the version number is the right identity. The
    active-run index is what absorbs a double-click, and it does so at the
    database rather than by guessing at intent.
    """

    return f"campaign-offering-research:{campaign_id}:{version_number}"


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def active_run(session: Session, *, campaign_id: uuid.UUID) -> CampaignOfferingResearch | None:
    """The run this Campaign is waiting on, if any."""

    return session.scalars(
        select(CampaignOfferingResearch)
        .where(
            CampaignOfferingResearch.campaign_id == campaign_id,
            CampaignOfferingResearch.status.in_(ACTIVE_RESEARCH_STATUSES),
        )
        .order_by(CampaignOfferingResearch.version_number.desc())
        .limit(1)
    ).first()


def current_version(session: Session, *, campaign_id: uuid.UUID) -> CampaignOfferingResearch | None:
    """The READY version this Campaign is leading with, if any."""

    return session.scalars(
        select(CampaignOfferingResearch).where(
            CampaignOfferingResearch.campaign_id == campaign_id,
            CampaignOfferingResearch.is_current.is_(True),
        )
    ).first()


def latest_run(session: Session, *, campaign_id: uuid.UUID) -> CampaignOfferingResearch | None:
    """The most recent run of any status — what the Setup screen reports on."""

    return session.scalars(
        select(CampaignOfferingResearch)
        .where(CampaignOfferingResearch.campaign_id == campaign_id)
        .order_by(CampaignOfferingResearch.version_number.desc())
        .limit(1)
    ).first()


def history(
    session: Session, *, campaign_id: uuid.UUID, limit: int = 20
) -> list[CampaignOfferingResearch]:
    """Every run for one Campaign, newest first. Admin diagnostics read this."""

    return list(
        session.scalars(
            select(CampaignOfferingResearch)
            .where(CampaignOfferingResearch.campaign_id == campaign_id)
            .order_by(CampaignOfferingResearch.version_number.desc())
            .limit(max(1, min(limit, 200)))
        ).all()
    )


def _next_version_number(session: Session, *, campaign_id: uuid.UUID) -> int:
    highest = session.scalar(
        select(func.max(CampaignOfferingResearch.version_number)).where(
            CampaignOfferingResearch.campaign_id == campaign_id
        )
    )
    return int(highest or 0) + 1


# ---------------------------------------------------------------------------
# Requesting
# ---------------------------------------------------------------------------


def request_research(
    session: Session,
    *,
    campaign: Campaign,
    raw_url: str,
    requested_by: str | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    actor: str = JOB_ACTOR,
) -> CampaignOfferingResearch:
    """Elect URL mode for this Campaign and queue the next version.

    The election and the run are written together on purpose: a Campaign in URL
    mode with nothing queued is a state that means "waiting forever", and there is
    no screen that can produce it.

    Raises :class:`OfferingResearchError` when a run is already in flight, and
    :class:`~app.services.campaign_offering.urls.OfferingUrlError` when the
    address cannot be used. Neither writes anything.
    """

    existing = active_run(session, campaign_id=campaign.id)
    if existing is not None:
        raise OfferingResearchError(
            "This Campaign's offering is already being prepared.",
            code="offering_research_in_flight",
        )

    normalized, host = normalize_offering_url(raw_url)
    version_number = _next_version_number(session, campaign_id=campaign.id)
    run = CampaignOfferingResearch(
        campaign_id=campaign.id,
        version_number=version_number,
        source_url=normalized,
        source_host=host,
        status=CampaignOfferingResearchStatus.QUEUED,
        attempts=0,
        max_attempts=max(1, min(max_attempts, 10)),
        next_run_at=_now(),
        idempotency_key=idempotency_key(campaign_id=campaign.id, version_number=version_number),
        requested_by=(requested_by or actor)[:255],
        requested_at=_now(),
    )
    session.add(run)
    campaign.offering_source = CampaignOfferingSource.URL_RESEARCH
    session.flush()
    record_audit_event(
        session,
        actor=actor,
        action="campaign_offering_research.requested",
        entity_type="campaign_offering_research",
        entity_id=str(run.id),
        new_state=run.status.value,
        reason="campaign offering research requested from a URL",
        context={
            "campaign_id": str(campaign.id),
            "version_number": version_number,
            "source_host": host,
        },
    )
    return run


def use_library_offering(
    session: Session, *, campaign: Campaign, actor: str = JOB_ACTOR
) -> Campaign:
    """Return this Campaign to leading with its Library offering.

    Deliberately *not* a delete. Every run stays, the current version keeps its
    ``is_current`` flag, and switching back to URL mode later leads with it again
    without spending another model call. What changes is one election, and the
    resolver stops consulting the research at all.

    An in-flight run is cancelled, because leaving it to finish would silently
    re-elect URL mode when it promoted itself.
    """

    running = active_run(session, campaign_id=campaign.id)
    if running is not None:
        cancel(session, run=running, reason="the Campaign returned to its Library offering")
    previous = campaign.offering_source
    campaign.offering_source = CampaignOfferingSource.LIBRARY
    session.flush()
    if previous is not CampaignOfferingSource.LIBRARY:
        record_audit_event(
            session,
            actor=actor,
            action="campaign_offering_research.library_restored",
            entity_type="campaign",
            entity_id=str(campaign.id),
            previous_state=previous.value,
            new_state=CampaignOfferingSource.LIBRARY.value,
            reason="campaign returned to its Library offering",
        )
    return campaign


# ---------------------------------------------------------------------------
# Leasing
# ---------------------------------------------------------------------------


def recover_expired_leases(
    session: Session, *, now: datetime | None = None
) -> list[CampaignOfferingResearch]:
    """Return abandoned runs to the queue, or fail them when attempts are spent.

    Part of every claim pass, so a worker that died needs no separate scheduler.
    A recovered run keeps a durable ``lease_expired`` marker: "this crashed and
    was retried" and "this failed and was retried" are different stories, and the
    customer-facing sentence is the same either way.
    """

    moment = now or _now()
    stale = list(
        session.scalars(
            select(CampaignOfferingResearch).where(
                CampaignOfferingResearch.status.in_(
                    (
                        CampaignOfferingResearchStatus.READING,
                        CampaignOfferingResearchStatus.ANALYZING,
                        CampaignOfferingResearchStatus.CONNECTING,
                    )
                ),
                CampaignOfferingResearch.lease_expires_at.is_not(None),
                CampaignOfferingResearch.lease_expires_at < moment,
            )
        ).all()
    )
    for run in stale:
        run.lease_owner = None
        run.lease_expires_at = None
        run.failure_code = "lease_expired"
        run.failure_reason = "the worker preparing this offering stopped reporting"
        if run.attempts >= run.max_attempts:
            run.status = CampaignOfferingResearchStatus.FAILED
            run.completed_at = moment
        else:
            run.status = CampaignOfferingResearchStatus.QUEUED
            run.next_run_at = moment
    if stale:
        session.flush()
    return stale


def claim_next(
    session: Session,
    *,
    worker_id: str,
    lease_seconds: float = 420.0,
    now: datetime | None = None,
    recover: bool = True,
) -> CampaignOfferingResearch | None:
    """Lease one due run and move it to ``READING``, or return None.

    ``SKIP LOCKED`` so a second worker steps over a row the first is holding. The
    claim is meant to be committed on its own: ``READING`` is the status the
    customer watches, and it is worth nothing if it only becomes visible when the
    model call that follows it has already finished.
    """

    clean_worker = worker_id.strip()
    if not clean_worker or len(clean_worker) > 100:
        raise OfferingResearchError("worker_id must be 1 to 100 characters")
    if lease_seconds <= 0:
        raise OfferingResearchError("lease_seconds must be positive")

    moment = now or _now()
    if recover:
        recover_expired_leases(session, now=moment)

    run = session.scalars(
        select(CampaignOfferingResearch)
        .where(
            CampaignOfferingResearch.status.in_(CLAIMABLE_RESEARCH_STATUSES),
            CampaignOfferingResearch.next_run_at <= moment,
        )
        .order_by(
            CampaignOfferingResearch.next_run_at.asc(),
            CampaignOfferingResearch.created_at.asc(),
            CampaignOfferingResearch.id.asc(),
        )
        .limit(1)
        .with_for_update(skip_locked=True)
    ).first()
    if run is None:
        return None

    run.status = CampaignOfferingResearchStatus.READING
    run.attempts += 1
    run.lease_owner = clean_worker
    run.lease_expires_at = moment + timedelta(seconds=lease_seconds)
    run.read_at = moment
    if run.failure_code != "lease_expired":
        run.failure_code = None
        run.failure_reason = None
    session.flush()
    return run


def mark_analyzing(
    session: Session, *, run: CampaignOfferingResearch, now: datetime | None = None
) -> CampaignOfferingResearch:
    """The page came back; the structure is being validated."""

    run.status = CampaignOfferingResearchStatus.ANALYZING
    run.analyzed_at = now or _now()
    session.flush()
    return run


def mark_connecting(
    session: Session, *, run: CampaignOfferingResearch
) -> CampaignOfferingResearch:
    """The structure is valid; it is being attached to the seller's own context."""

    run.status = CampaignOfferingResearchStatus.CONNECTING
    session.flush()
    return run


# ---------------------------------------------------------------------------
# Finishing
# ---------------------------------------------------------------------------


def mark_ready(
    session: Session,
    *,
    run: CampaignOfferingResearch,
    offering_context: dict[str, object],
    context_digest: str,
    context_policy_version: str,
    producer: str,
    producer_version: str,
    producer_model: str | None = None,
    supporting_offering_id: uuid.UUID | None = None,
    now: datetime | None = None,
    actor: str = JOB_ACTOR,
) -> CampaignOfferingResearch:
    """Store the answer and make this version the Campaign's current one.

    The demotion of the previous current version and the promotion of this one
    happen in one flush, under the partial unique index that permits exactly one
    current row per Campaign. There is therefore no moment at which a Campaign has
    two current offerings, and none at which it has none while a good version
    exists.
    """

    moment = now or _now()
    previous = current_version(session, campaign_id=run.campaign_id)
    if previous is not None and previous.id != run.id:
        previous.is_current = False
        previous.superseded_at = moment
        session.flush()

    run.status = CampaignOfferingResearchStatus.READY
    run.offering_context = dict(offering_context)
    run.context_digest = context_digest
    run.context_policy_version = context_policy_version
    run.producer = producer[:64]
    run.producer_version = producer_version[:64]
    run.producer_model = producer_model[:120] if producer_model else None
    run.supporting_offering_id = supporting_offering_id
    run.failure_code = None
    run.failure_reason = None
    run.lease_owner = None
    run.lease_expires_at = None
    run.completed_at = moment
    run.is_current = True
    session.flush()
    record_audit_event(
        session,
        actor=actor,
        action="campaign_offering_research.ready",
        entity_type="campaign_offering_research",
        entity_id=str(run.id),
        new_state=run.status.value,
        reason="campaign offering research became the Campaign's primary offering",
        context={
            "campaign_id": str(run.campaign_id),
            "version_number": run.version_number,
            "superseded_version": previous.version_number if previous is not None else None,
            "context_digest": context_digest,
        },
    )
    return run


def mark_failed(
    session: Session,
    *,
    run: CampaignOfferingResearch,
    code: str,
    message: str,
    retryable: bool,
    now: datetime | None = None,
    actor: str = JOB_ACTOR,
) -> CampaignOfferingResearch:
    """Fail a run, scheduling a retry only when one could plausibly help.

    It never touches ``is_current``. That is the whole of "a failed re-analysis
    preserves the last good context": there is no code path from here to the
    Campaign's active version.
    """

    moment = now or _now()
    run.failure_code = code[:96]
    run.failure_reason = message[:2000]
    run.lease_owner = None
    run.lease_expires_at = None

    if retryable and run.attempts < run.max_attempts:
        run.status = CampaignOfferingResearchStatus.QUEUED
        backoff = min(RETRY_BASE_SECONDS * (2 ** (run.attempts - 1)), RETRY_CAP_SECONDS)
        run.next_run_at = moment + timedelta(seconds=backoff)
    else:
        run.status = CampaignOfferingResearchStatus.FAILED
        run.completed_at = moment
    session.flush()
    record_audit_event(
        session,
        actor=actor,
        action="campaign_offering_research.failed",
        entity_type="campaign_offering_research",
        entity_id=str(run.id),
        new_state=run.status.value,
        reason=message[:500],
        context={
            "campaign_id": str(run.campaign_id),
            "version_number": run.version_number,
            "code": code,
            "retryable": retryable,
            "attempts": run.attempts,
            "max_attempts": run.max_attempts,
        },
    )
    return run


def cancel(
    session: Session,
    *,
    run: CampaignOfferingResearch,
    reason: str,
    now: datetime | None = None,
    actor: str = JOB_ACTOR,
) -> CampaignOfferingResearch:
    """Stop a run that is no longer wanted. Never applied to a finished one."""

    if run.status not in ACTIVE_RESEARCH_STATUSES:
        return run
    moment = now or _now()
    run.status = CampaignOfferingResearchStatus.CANCELLED
    run.failure_code = "cancelled"
    run.failure_reason = reason[:2000]
    run.lease_owner = None
    run.lease_expires_at = None
    run.completed_at = moment
    session.flush()
    record_audit_event(
        session,
        actor=actor,
        action="campaign_offering_research.cancelled",
        entity_type="campaign_offering_research",
        entity_id=str(run.id),
        new_state=run.status.value,
        reason=reason[:500],
        context={
            "campaign_id": str(run.campaign_id),
            "version_number": run.version_number,
        },
    )
    return run
