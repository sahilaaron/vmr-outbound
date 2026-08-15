"""Campaign Contact enrolment, provenance, lifecycle, and eligibility."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.campaign import Campaign, CampaignContact
from app.models.contact import Contact
from app.models.enums import (
    AgentIdentifier,
    AgentJobStatus,
    CampaignContactEligibility,
    CampaignMembershipStatus,
    CampaignStatus,
    ContactWorkflowState,
    PipelineEventType,
    PipelineStageStatus,
)
from app.models.pipeline import CampaignContactSource
from app.models.verification_job import AgentJob
from app.services.agents import locking
from app.services.agents.readiness import execution_readiness
from app.services.audit import record_audit_event
from app.services.personalization.cadence import campaign_opted_in
from app.services.pipeline import (
    agent_state,
    append_event,
    initialize_pipeline,
    transition_stage,
)
from app.services.resolution.gates import DownstreamStage, authorize_contact
from app.services.suppressions import evaluate_suppression


class CampaignContactError(Exception):
    """Safe operator-facing Campaign Contact error."""


class CampaignContactNotFound(CampaignContactError):
    pass


@dataclass(frozen=True)
class EnrollmentResult:
    membership: CampaignContact
    created: bool
    source_created: bool
    queued_job: AgentJob | None


def _source_context(value: dict[str, Any] | None) -> dict[str, Any]:
    clean = value or {}
    if not isinstance(clean, dict):
        raise CampaignContactError("source_context must be a JSON object")
    try:
        encoded = json.dumps(clean, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise CampaignContactError("source_context must contain JSON values") from exc
    if len(encoded.encode("utf-8")) > 50_000:
        raise CampaignContactError("source_context is too large (max 50000 bytes)")
    return clean


def _source_key(
    *,
    campaign_id: uuid.UUID,
    contact_id: uuid.UUID,
    source_type: str,
    source_reference: str | None,
    capture_id: uuid.UUID | None,
    import_batch_id: uuid.UUID | None,
    collection_id: uuid.UUID | None,
    source_context: dict[str, Any],
    explicit: str | None,
) -> str:
    serialized_context = json.dumps(source_context, separators=(",", ":"), sort_keys=True)
    raw = explicit or ":".join(
        [
            str(campaign_id),
            str(contact_id),
            source_type,
            source_reference or "",
            str(capture_id or ""),
            str(import_batch_id or ""),
            str(collection_id or ""),
            serialized_context,
        ]
    )
    if len(raw) <= 512:
        return raw
    return f"sha256:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


def _blocking_reasons(
    session: Session, contact: Contact, *, campaign: Campaign | None = None
) -> tuple[list[dict[str, Any]], bool]:
    """Why this Contact cannot be worked, and whether the reason is terminal.

    ``campaign`` decides how a provisional company domain is read. It is optional
    so a caller without one still gets the strict answer rather than the most
    permissive campaign's.
    """

    reasons: list[dict[str, Any]] = []
    suppression = evaluate_suppression(
        session,
        email=contact.email,
        domain=contact.company_domain,
    )
    if suppression.blocked:
        reasons.append(
            {
                "code": "suppression",
                "detail": suppression.blocked_reason
                or "the suppression ledger blocks this identity",
                "terminal": True,
            }
        )
    if not contact.company_domain:
        reasons.append(
            {
                "code": "company_domain_missing",
                "detail": "Company resolution needs an observed or approved domain.",
                "terminal": False,
            }
        )
    if not contact.first_name or not contact.last_name:
        reasons.append(
            {
                "code": "person_name_missing",
                "detail": "Personalization and email work need an observed first and last name.",
                "terminal": False,
            }
        )
    gate = authorize_contact(
        session,
        contact=contact,
        stage=DownstreamStage.CAMPAIGN_ELIGIBILITY,
        campaign=campaign,
    )
    if gate.blocked:
        reasons.append(
            {
                "code": "company_identity",
                "detail": gate.reason,
                "terminal": False,
            }
        )
    return reasons, suppression.blocked


def is_terminally_blocked(session: Session, *, membership: CampaignContact) -> bool:
    """Whether policy blocks this membership terminally, asked without writing.

    :func:`refresh_eligibility` answers the same question authoritatively, but it also
    re-projects the answer onto the row and can transition the current stage. A page
    render must not do either, so read-only callers use this instead. The two can only
    disagree in the operator's favour: a suppression lifted a moment ago is invisible
    here until the next write path notices it.
    """

    contact = session.get(Contact, membership.contact_id)
    if contact is None:  # pragma: no cover - protected by FK
        raise CampaignContactNotFound(f"contact {membership.contact_id} does not exist")
    if membership.state in (ContactWorkflowState.EXCLUDED, ContactWorkflowState.SUPPRESSED):
        return True
    _, terminal = _blocking_reasons(
        session, contact, campaign=session.get(Campaign, membership.campaign_id)
    )
    return terminal


def refresh_eligibility(
    session: Session,
    *,
    membership: CampaignContact,
    actor: str = "system",
) -> bool:
    """Re-project current suppression and identity gates onto a membership.

    Returns ``True`` for an authoritative terminal policy block. Non-terminal
    missing evidence remains review-required and is handled by the relevant
    Agent.
    """

    contact = session.get(Contact, membership.contact_id)
    if contact is None:  # pragma: no cover - protected by FK
        raise CampaignContactNotFound(f"contact {membership.contact_id} does not exist")
    reasons, terminal_block = _blocking_reasons(
        session, contact, campaign=session.get(Campaign, membership.campaign_id)
    )
    if membership.state is ContactWorkflowState.EXCLUDED:
        reasons.insert(
            0,
            {
                "code": "excluded",
                "detail": "The Campaign Contact was deliberately excluded.",
                "terminal": True,
            },
        )
        terminal_block = True
    elif membership.state is ContactWorkflowState.SUPPRESSED and not terminal_block:
        # The legacy workflow treated SUPPRESSED as terminal. Do not silently
        # reactivate that historical decision merely because a later ledger
        # read no longer finds its original row.
        reasons.insert(
            0,
            {
                "code": "legacy_suppressed",
                "detail": "The Campaign Contact has a historical terminal suppression.",
                "terminal": True,
            },
        )
        terminal_block = True
    target = (
        CampaignContactEligibility.BLOCKED
        if terminal_block
        else (
            CampaignContactEligibility.REVIEW_REQUIRED
            if reasons
            else CampaignContactEligibility.ELIGIBLE
        )
    )
    previous = membership.eligibility_status
    previous_reasons = list(membership.blocking_reasons or [])
    membership.eligibility_status = target
    membership.blocking_reasons = reasons
    if terminal_block:
        # Eligibility is authoritative over operator execution controls. A
        # suppressed/excluded Contact is blocked, never merely disabled or
        # paused, even when the Campaign or Agent was already switched off.
        membership.pipeline_status = PipelineStageStatus.BLOCKED
        current_agent = membership.next_stage
        current_state = (
            agent_state(
                session,
                campaign_contact_id=membership.id,
                agent_id=current_agent,
                create=False,
            )
            if current_agent is not None
            else None
        )
        if (
            current_agent is not None
            and current_state is not None
            and current_state.status
            in {
                PipelineStageStatus.WAITING,
                PipelineStageStatus.RUNNING,
                PipelineStageStatus.PAUSED,
                PipelineStageStatus.RETRYING,
                PipelineStageStatus.DISABLED,
            }
        ):
            transition_stage(
                session,
                membership=membership,
                agent_id=current_agent,
                target=PipelineStageStatus.BLOCKED,
                event_type=PipelineEventType.ELIGIBILITY_BLOCKED,
                actor=actor,
                reason_code=str(reasons[0].get("code")) if reasons else "eligibility_blocked",
                reason_detail=(
                    str(reasons[0].get("detail"))
                    if reasons
                    else "Campaign Contact is blocked by an authoritative eligibility rule."
                ),
            )
    if terminal_block and any(reason.get("code") == "suppression" for reason in reasons):
        membership.state = ContactWorkflowState.SUPPRESSED
    session.flush()
    if previous is not target or previous_reasons != reasons:
        append_event(
            session,
            campaign_contact_id=membership.id,
            event_type=(
                PipelineEventType.ELIGIBILITY_BLOCKED
                if terminal_block
                else PipelineEventType.ELIGIBILITY_RESTORED
            ),
            actor=actor,
            reason_code=(str(reasons[0].get("code")) if reasons else "eligibility_clear"),
            reason_detail=(
                str(reasons[0].get("detail"))
                if reasons
                else "Campaign Contact eligibility gates are clear."
            ),
            detail={
                "previous": previous.value,
                "current": target.value,
                "blocking_reasons": reasons,
            },
        )
        record_audit_event(
            session,
            actor=actor,
            action="campaign_contact.eligibility_reconciled",
            entity_type="campaign_contact",
            entity_id=str(membership.id),
            previous_state=previous.value,
            new_state=target.value,
            reason="Campaign Contact eligibility was re-evaluated from durable evidence",
            context={"blocking_reasons": reasons},
        )
    return terminal_block


_EVIDENCE_RESOLVABLE_BLOCKS = frozenset(
    {
        "company_domain_missing",
        "company_missing",
        "company_identity",
        "person_name_missing",
    }
)


def reconcile_contact_memberships(
    session: Session,
    *,
    contact_id: uuid.UUID,
    actor: str = "system",
) -> int:
    """Refresh every Campaign membership and resume evidence-resolved work."""

    memberships = list(
        session.scalars(
            select(CampaignContact)
            .where(
                CampaignContact.contact_id == contact_id,
                CampaignContact.membership_status == CampaignMembershipStatus.ACTIVE,
            )
            .order_by(CampaignContact.id)
            .with_for_update()
        ).all()
    )
    resumed = 0
    now = datetime.now(UTC)
    for membership in memberships:
        terminal = refresh_eligibility(
            session,
            membership=membership,
            actor=actor,
        )
        if terminal or membership.next_stage is None:
            continue
        paused_jobs = locking.lock_agent_jobs(
            session,
            select(AgentJob).where(
                AgentJob.campaign_contact_id == membership.id,
                AgentJob.agent_id == membership.next_stage,
                AgentJob.status == AgentJobStatus.PAUSED,
            ),
        )
        paused = paused_jobs[0] if paused_jobs else None
        if paused is None or paused.error_class not in _EVIDENCE_RESOLVABLE_BLOCKS:
            continue
        paused.status = AgentJobStatus.PENDING
        paused.next_run_at = now
        paused.error_class = None
        paused.last_error = None
        paused.error = None
        transition_stage(
            session,
            membership=membership,
            agent_id=paused.agent_id,
            target=PipelineStageStatus.WAITING,
            event_type=PipelineEventType.STAGE_WAITING,
            actor=actor,
            job=paused,
            reason_code="evidence_updated",
            reason_detail="New permanent evidence resolved the prior Agent block.",
        )
        resumed += 1
    if memberships:
        session.flush()
    return resumed


def retry_processing(
    session: Session,
    *,
    campaign_contact_id: uuid.UUID,
    actor: str = "operator",
    reason: str = "operator requested retry",
) -> AgentJob | None:
    """Retry the current retryable/blocked stage without bypassing controls."""

    membership = locking.lock_campaign_contact(session, campaign_contact_id)
    if membership is None:
        raise CampaignContactNotFound(f"campaign contact {campaign_contact_id} does not exist")
    if membership.membership_status is not CampaignMembershipStatus.ACTIVE:
        raise CampaignContactError("the Campaign Contact must be active before its Agent can retry")
    if refresh_eligibility(session, membership=membership, actor=actor):
        raise CampaignContactError(
            "the terminal eligibility block must be resolved before retrying"
        )
    agent_id = membership.next_stage
    if agent_id is None:
        raise CampaignContactError("the Campaign Contact has no stage to retry")
    locked_jobs = locking.lock_agent_jobs(
        session,
        select(AgentJob).where(
            AgentJob.campaign_contact_id == membership.id,
            AgentJob.agent_id == agent_id,
        ),
    )
    job = locked_jobs[-1] if locked_jobs else None
    if job is not None:
        if job.status not in {AgentJobStatus.PAUSED, AgentJobStatus.FAILED}:
            raise CampaignContactError(
                f"the current Agent job is {job.status.value}, not retryable or blocked"
            )
        if job.error_class in {
            "agent_disabled",
            "agent_paused",
            "membership_paused",
            "suppression",
        }:
            raise CampaignContactError(
                "the controlling pause or suppression must be resolved before retrying"
            )
        if job.status is AgentJobStatus.FAILED and not bool(
            (job.error or {}).get("retryable", False)
        ):
            raise CampaignContactError("the current Agent failure is terminal")
        job.status = AgentJobStatus.PENDING
        job.next_run_at = datetime.now(UTC)
        job.finished_at = None
        job.error_class = None
        job.last_error = None
        job.error = None
    transition_stage(
        session,
        membership=membership,
        agent_id=agent_id,
        target=PipelineStageStatus.WAITING,
        event_type=PipelineEventType.STAGE_WAITING,
        actor=actor,
        job=job,
        reason_code="operator_retry",
        reason_detail=reason,
    )
    from app.services.agents.orchestrator import schedule_next

    queued = schedule_next(session, membership=membership, actor=actor)
    return queued or job


def _record_source(
    session: Session,
    *,
    membership: CampaignContact,
    source_type: str,
    source_reference: str | None,
    source_context: dict[str, Any] | None,
    capture_id: uuid.UUID | None,
    import_batch_id: uuid.UUID | None,
    collection_id: uuid.UUID | None,
    actor: str,
    idempotency_key: str,
) -> tuple[CampaignContactSource, bool]:
    def same_intent(row: CampaignContactSource) -> bool:
        return (
            row.campaign_contact_id == membership.id
            and row.source_type == source_type
            and row.source_reference == source_reference
            and row.capture_id == capture_id
            and row.import_batch_id == import_batch_id
            and row.collection_id == collection_id
            and (row.source_context or {}) == (source_context or {})
        )

    existing = session.scalars(
        select(CampaignContactSource).where(
            CampaignContactSource.idempotency_key == idempotency_key
        )
    ).one_or_none()
    if existing is not None:
        if not same_intent(existing):
            raise CampaignContactError(
                "campaign enrolment idempotency key was reused for a different source intent"
            )
        return existing, False
    source = CampaignContactSource(
        campaign_contact_id=membership.id,
        idempotency_key=idempotency_key,
        source_type=source_type,
        source_reference=source_reference,
        capture_id=capture_id,
        import_batch_id=import_batch_id,
        collection_id=collection_id,
        source_context=source_context or {},
        recorded_by=actor,
    )
    try:
        with session.begin_nested():
            session.add(source)
            session.flush()
    except IntegrityError as exc:
        winner = session.scalars(
            select(CampaignContactSource).where(
                CampaignContactSource.idempotency_key == idempotency_key
            )
        ).one_or_none()
        if winner is None:  # pragma: no cover - defensive
            raise
        if not same_intent(winner):
            raise CampaignContactError(
                "campaign enrolment idempotency key was reused for a different source intent"
            ) from exc
        return winner, False
    return source, True


#: The pipeline stage an enrolment aims at unless a caller says otherwise. Named
#: here rather than repeated as a literal so that a second surface adopting "the
#: same target the product uses" cannot drift from it silently.
DEFAULT_DESIRED_STAGE = AgentIdentifier.SENDING


def enrol_contact(
    session: Session,
    *,
    campaign_id: uuid.UUID,
    contact_id: uuid.UUID,
    source_type: str,
    source_reference: str | None = None,
    source_context: dict[str, Any] | None = None,
    capture_id: uuid.UUID | None = None,
    import_batch_id: uuid.UUID | None = None,
    collection_id: uuid.UUID | None = None,
    idempotency_key: str | None = None,
    actor: str = "operator",
    enqueue: bool = True,
    desired_stage: AgentIdentifier = DEFAULT_DESIRED_STAGE,
) -> EnrollmentResult:
    """Idempotently upsert one permanent Contact's Campaign participation."""

    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise CampaignContactNotFound(f"campaign {campaign_id} does not exist")
    if campaign.status is CampaignStatus.ARCHIVED:
        raise CampaignContactError("an archived campaign cannot receive contacts")
    # Refused here, before the first write, because everything after this point
    # is the damage. `schedule_next` steps a running Campaign's contact past a
    # disabled skippable Agent into SKIPPED, and SKIPPED is absorbing — enabling
    # the Agent afterwards recovers nobody.
    #
    # `enqueue=False` is not an escape from it: the auto-skip in `schedule_next`
    # runs before that flag is consulted, so a deferred enqueue burns the stage
    # just as thoroughly as an immediate one. Holding the membership unqueued
    # instead was therefore not an option this pipeline offers — it would need
    # the scheduler not to walk at all, plus a wake-up path that does not exist:
    # `reconcile_agent_control` only re-schedules memberships whose `next_stage`
    # already names the Agent being enabled, so a membership held before its walk
    # would simply be stranded.
    #
    # Refusing the enrolment loses nothing. The Contact is permanent and never
    # required a Campaign to exist, so the operator can enrol them the moment the
    # Agent is switched on.
    if campaign.execution_enabled and campaign_opted_in(campaign):
        readiness = execution_readiness(session, campaign=campaign, prospective_stage=desired_stage)
        if not readiness.runnable:
            raise CampaignContactError(readiness.enrolment_refusal_message())
    contact = session.get(Contact, contact_id)
    if contact is None:
        raise CampaignContactNotFound(f"contact {contact_id} does not exist")
    if contact.merged_into_id is not None:
        raise CampaignContactError(
            f"contact {contact_id} was merged; enrol its surviving Contact instead"
        )
    clean_type = source_type.strip().lower()
    if not clean_type or len(clean_type) > 64:
        raise CampaignContactError("source_type must be 1 to 64 characters")
    if source_reference is not None and len(source_reference) > 512:
        raise CampaignContactError("source_reference must be 512 characters or fewer")
    clean_context = _source_context(source_context)

    membership = session.scalars(
        select(CampaignContact).where(
            CampaignContact.campaign_id == campaign_id,
            CampaignContact.contact_id == contact_id,
        )
    ).one_or_none()
    created = False
    reasons, suppression_blocked = _blocking_reasons(session, contact, campaign=campaign)
    if membership is None:
        membership = CampaignContact(
            campaign_id=campaign_id,
            contact_id=contact_id,
            source_batch_id=import_batch_id,
            source_capture_id=capture_id,
            source_kind=clean_type,
            source_reference=source_reference,
            enrolled_by=actor,
            membership_status=CampaignMembershipStatus.ACTIVE,
            state=(
                ContactWorkflowState.SUPPRESSED
                if suppression_blocked
                else ContactWorkflowState.IMPORTED
            ),
            eligibility_status=(
                CampaignContactEligibility.BLOCKED
                if suppression_blocked
                else (
                    CampaignContactEligibility.REVIEW_REQUIRED
                    if reasons
                    else CampaignContactEligibility.ELIGIBLE
                )
            ),
            blocking_reasons=reasons,
            desired_stage=desired_stage,
            pipeline_status=(
                PipelineStageStatus.BLOCKED if suppression_blocked else PipelineStageStatus.WAITING
            ),
        )
        try:
            with session.begin_nested():
                session.add(membership)
                session.flush()
        except IntegrityError:
            membership = session.scalars(
                select(CampaignContact).where(
                    CampaignContact.campaign_id == campaign_id,
                    CampaignContact.contact_id == contact_id,
                )
            ).one()
        else:
            created = True
    if not created:
        if membership.membership_status is CampaignMembershipStatus.ARCHIVED:
            # Idempotency must never silently reactivate a deliberately archived row.
            enqueue = False
        elif membership.desired_stage is not desired_stage:
            raise CampaignContactError(
                "existing Campaign Contact has a different desired pipeline stage"
            )
        else:
            refresh_eligibility(session, membership=membership, actor=actor)

    key = _source_key(
        campaign_id=campaign_id,
        contact_id=contact_id,
        source_type=clean_type,
        source_reference=source_reference,
        capture_id=capture_id,
        import_batch_id=import_batch_id,
        collection_id=collection_id,
        source_context=clean_context,
        explicit=idempotency_key,
    )
    source, source_created = _record_source(
        session,
        membership=membership,
        source_type=clean_type,
        source_reference=source_reference,
        source_context=clean_context,
        capture_id=capture_id,
        import_batch_id=import_batch_id,
        collection_id=collection_id,
        actor=actor,
        idempotency_key=key,
    )

    if created:
        append_event(
            session,
            campaign_contact_id=membership.id,
            event_type=PipelineEventType.ENROLLED,
            actor=actor,
            reason_code=clean_type,
            reason_detail="Permanent Contact enrolled in Campaign.",
            detail={"source_reference": source_reference, "source_idempotency_key": key},
        )
        initialize_pipeline(
            session,
            membership=membership,
            actor=actor,
            blocked=suppression_blocked,
            block_reason=(
                str(reasons[0].get("detail")) if suppression_blocked and reasons else None
            ),
        )
        record_audit_event(
            session,
            actor=actor,
            action="campaign_contact.enrolled",
            entity_type="campaign_contact",
            entity_id=str(membership.id),
            new_state=membership.membership_status.value,
            reason=(
                "contact enrolled but blocked by suppression"
                if suppression_blocked
                else "contact enrolled"
            ),
            context={
                "campaign_id": str(campaign_id),
                "contact_id": str(contact_id),
                "source_type": clean_type,
                "eligibility": membership.eligibility_status.value,
            },
        )

    queued_job: AgentJob | None = None
    if membership.membership_status is CampaignMembershipStatus.ACTIVE:
        from app.services.agents.orchestrator import schedule_next

        queued_job = schedule_next(
            session,
            membership=membership,
            actor=actor,
            allow_enqueue=enqueue,
        )

    # Capture/import have richer source-specific finalizers that run only after
    # their validation, promotion and filing records are complete. Identity
    # resolution is the next Agent's authority, not a new Capture execution.
    # All other existing enrollment surfaces pin the operator/API selection here.
    if clean_type in {"manual", "api"}:
        from app.services.captures.execution_lineage import record_enrollment_execution

        record_enrollment_execution(
            session,
            source=source,
            membership=membership,
            contact=contact,
            actor=actor,
            membership_created=created,
        )
    return EnrollmentResult(
        membership=membership,
        created=created,
        source_created=source_created,
        queued_job=queued_job,
    )


def ensure_membership(
    session: Session,
    *,
    campaign_id: uuid.UUID,
    contact_id: uuid.UUID,
    source_batch_id: uuid.UUID | None = None,
    actor: str = "operator",
) -> tuple[CampaignContact, bool]:
    """Compatibility wrapper for the historical Campaign Contact service."""

    result = enrol_contact(
        session,
        campaign_id=campaign_id,
        contact_id=contact_id,
        source_type="import",
        import_batch_id=source_batch_id,
        source_reference=str(source_batch_id) if source_batch_id else None,
        actor=actor,
        enqueue=False,
    )
    return result.membership, result.created


def archive_membership(
    session: Session,
    *,
    campaign_contact_id: uuid.UUID,
    actor: str = "operator",
    reason: str = "removed from Campaign by operator",
) -> CampaignContact:
    membership = locking.lock_campaign_contact(session, campaign_contact_id)
    if membership is None:
        raise CampaignContactNotFound(f"campaign contact {campaign_contact_id} does not exist")
    if membership.membership_status is CampaignMembershipStatus.ARCHIVED:
        return membership
    previous = membership.membership_status
    previous_pipeline = membership.pipeline_status
    membership.membership_status = CampaignMembershipStatus.ARCHIVED
    membership.archived_at = datetime.now(UTC)
    membership.pipeline_status = PipelineStageStatus.PAUSED
    session.flush()
    from app.services.agents.jobs import cancel_jobs_for_membership

    cancel_jobs_for_membership(
        session,
        campaign_contact_id=membership.id,
        reason=reason,
        actor=actor,
    )
    append_event(
        session,
        campaign_contact_id=membership.id,
        event_type=PipelineEventType.MEMBERSHIP_ARCHIVED,
        actor=actor,
        reason_code="operator_archive",
        reason_detail=reason,
        from_status=previous_pipeline,
        to_status=PipelineStageStatus.PAUSED,
    )
    record_audit_event(
        session,
        actor=actor,
        action="campaign_contact.archived",
        entity_type="campaign_contact",
        entity_id=str(membership.id),
        previous_state=previous.value,
        new_state=membership.membership_status.value,
        reason=reason,
    )
    return membership


def pause_membership(
    session: Session,
    *,
    campaign_contact_id: uuid.UUID,
    actor: str = "operator",
    reason: str = "paused by operator",
) -> CampaignContact:
    membership = locking.lock_campaign_contact(session, campaign_contact_id)
    if membership is None:
        raise CampaignContactNotFound(f"campaign contact {campaign_contact_id} does not exist")
    if membership.membership_status is CampaignMembershipStatus.ARCHIVED:
        raise CampaignContactError("an archived Campaign Contact cannot be paused")
    if membership.membership_status is CampaignMembershipStatus.PAUSED:
        return membership
    previous_pipeline = membership.pipeline_status
    membership.membership_status = CampaignMembershipStatus.PAUSED
    membership.pipeline_status = PipelineStageStatus.PAUSED
    session.flush()
    from app.services.agents.jobs import pause_jobs_for_membership

    pause_jobs_for_membership(
        session,
        campaign_contact_id=membership.id,
        reason=reason,
        actor=actor,
    )
    current_agent = membership.current_stage
    current_state = (
        agent_state(
            session,
            campaign_contact_id=membership.id,
            agent_id=current_agent,
            create=False,
        )
        if current_agent is not None
        else None
    )
    if (
        current_agent is not None
        and current_state is not None
        and current_state.status
        in {
            PipelineStageStatus.WAITING,
            PipelineStageStatus.RUNNING,
            PipelineStageStatus.RETRYING,
            PipelineStageStatus.DISABLED,
            PipelineStageStatus.BLOCKED,
        }
    ):
        transition_stage(
            session,
            membership=membership,
            agent_id=current_agent,
            target=PipelineStageStatus.PAUSED,
            event_type=PipelineEventType.MEMBERSHIP_PAUSED,
            actor=actor,
            reason_code="operator_pause",
            reason_detail=reason,
        )
    else:
        append_event(
            session,
            campaign_contact_id=membership.id,
            event_type=PipelineEventType.MEMBERSHIP_PAUSED,
            actor=actor,
            from_status=previous_pipeline,
            to_status=PipelineStageStatus.PAUSED,
            reason_code="operator_pause",
            reason_detail=reason,
        )
    return membership


def resume_membership(
    session: Session,
    *,
    campaign_contact_id: uuid.UUID,
    actor: str = "operator",
    reason: str = "resumed by operator",
) -> CampaignContact:
    membership = locking.lock_campaign_contact(session, campaign_contact_id)
    if membership is None:
        raise CampaignContactNotFound(f"campaign contact {campaign_contact_id} does not exist")
    if membership.membership_status is CampaignMembershipStatus.ARCHIVED:
        raise CampaignContactError("archived membership requires an explicit re-enrolment action")
    if membership.membership_status is CampaignMembershipStatus.ACTIVE:
        return membership
    membership.membership_status = CampaignMembershipStatus.ACTIVE
    membership.pipeline_status = PipelineStageStatus.WAITING
    session.flush()
    from app.services.agents.jobs import resume_jobs_for_membership
    from app.services.agents.orchestrator import schedule_next

    resume_jobs_for_membership(session, campaign_contact_id=membership.id, actor=actor)
    current_agent = membership.current_stage
    current_state = (
        agent_state(
            session,
            campaign_contact_id=membership.id,
            agent_id=current_agent,
            create=False,
        )
        if current_agent is not None
        else None
    )
    if (
        current_agent is not None
        and current_state is not None
        and current_state.status is PipelineStageStatus.PAUSED
    ):
        state = transition_stage(
            session,
            membership=membership,
            agent_id=current_agent,
            target=PipelineStageStatus.WAITING,
            event_type=PipelineEventType.MEMBERSHIP_RESUMED,
            actor=actor,
            reason_code="operator_resume",
            reason_detail=reason,
        )
        state.reason_code = None
        state.reason_detail = None
    else:
        append_event(
            session,
            campaign_contact_id=membership.id,
            event_type=PipelineEventType.MEMBERSHIP_RESUMED,
            actor=actor,
            from_status=PipelineStageStatus.PAUSED,
            to_status=PipelineStageStatus.WAITING,
            reason_code="operator_resume",
            reason_detail=reason,
        )
    schedule_next(session, membership=membership, actor=actor)
    return membership


@dataclass(frozen=True)
class BulkEnrollmentResult:
    """What a bulk enrolment actually did, contact by contact.

    Deliberately not a bare count. An operator selecting ninety contacts needs
    to know which ones were already there and which were refused, because
    "eighty-seven enrolled" and "eighty-seven enrolled, three suppressed" call
    for different next actions.
    """

    enrolled: tuple[uuid.UUID, ...] = ()
    already_present: tuple[uuid.UUID, ...] = ()
    refused: tuple[tuple[uuid.UUID, str], ...] = ()
    queued_jobs: int = 0

    @property
    def attempted(self) -> int:
        return len(self.enrolled) + len(self.already_present) + len(self.refused)

    @property
    def summary(self) -> str:
        parts = [f"{len(self.enrolled)} enrolled"]
        if self.already_present:
            parts.append(f"{len(self.already_present)} already in this Campaign")
        if self.refused:
            parts.append(f"{len(self.refused)} refused")
        return ", ".join(parts) + "."


def enrol_contacts(
    session: Session,
    *,
    campaign_id: uuid.UUID,
    contact_ids: Sequence[uuid.UUID],
    source_type: str = "manual",
    source_reference: str | None = None,
    actor: str = "operator",
    enqueue: bool = True,
    desired_stage: AgentIdentifier = AgentIdentifier.SENDING,
) -> BulkEnrollmentResult:
    """Enrol many existing permanent Contacts into one Campaign.

    A thin loop over :func:`enrol_contact`, and thin on purpose: enrolment
    carries eligibility evaluation, source provenance, pipeline initialisation
    and the first queued job, and none of that is safe to shortcut for speed.

    One refusal does not abandon the rest. Each contact is enrolled inside its
    own SAVEPOINT so that a contact the domain layer rejects rolls back alone,
    leaving the successful enrolments and the caller's transaction intact. The
    caller still owns the commit.
    """

    enrolled: list[uuid.UUID] = []
    already: list[uuid.UUID] = []
    refused: list[tuple[uuid.UUID, str]] = []
    queued = 0

    for contact_id in dict.fromkeys(contact_ids):
        try:
            with session.begin_nested():
                outcome = enrol_contact(
                    session,
                    campaign_id=campaign_id,
                    contact_id=contact_id,
                    source_type=source_type,
                    source_reference=source_reference,
                    actor=actor,
                    enqueue=enqueue,
                    desired_stage=desired_stage,
                )
        except (CampaignContactError, IntegrityError) as exc:
            refused.append((contact_id, str(exc)[:200]))
            continue
        if outcome.created:
            enrolled.append(contact_id)
        else:
            already.append(contact_id)
        if outcome.queued_job is not None:
            queued += 1

    return BulkEnrollmentResult(
        enrolled=tuple(enrolled),
        already_present=tuple(already),
        refused=tuple(refused),
        queued_jobs=queued,
    )
