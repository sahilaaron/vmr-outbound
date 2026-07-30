"""Run one Agent again, for contacts it has already stopped on.

This is a third, distinct operation, and the distinction is the point:

* ``jobs.retry_failed_job`` un-sticks a job that failed *retryably* and still has
  attempts left. It refuses a terminal failure and refuses an exhausted budget, and
  both refusals are correct — retrying either would fail the same way.
* ``campaign_contacts.retry_processing`` retries the stage a contact is *waiting* on,
  and refuses a terminal failure for the same reason.
* This module handles the case neither can: **the cause has been fixed, so run it
  again.** A defect in an Agent, a missing feature switch, a domain entered by hand,
  a website that has come back — all of them turn a terminal failure into work that
  would now succeed, and nothing in the pipeline can know that. A person does.

Because it is a human assertion rather than an inference, it is bounded and recorded
rather than automatic:

* **A fresh attempt budget requires a new job.** ``enqueue_job`` is idempotent on
  its key, so re-queueing the old key returns the old failed job — which is why the
  key now carries a generation (``orchestrator.stage_job_key``). Each re-run is its
  own generation, so the earlier failed job stays on record beside it instead of
  being overwritten.
* **Nothing bypasses a control.** A disabled or paused Agent, a campaign with
  execution off, an archived membership, a suppressed contact: each is refused *by
  name*, because "I pressed run again and nothing happened" is the worst possible
  outcome. The refusal is data the page shows, not an exception.
* **Only a stopped stage is eligible.** A stage that is running, queued or already
  complete is left alone; re-running it would either duplicate work or discard an
  outcome.
* **It never sends.** Sending has no adapter, and this cannot enable one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.campaign import Campaign, CampaignContact
from app.models.contact import Contact
from app.models.enums import (
    AgentControlStatus,
    AgentIdentifier,
    AgentJobStatus,
    CampaignMembershipStatus,
    PipelineEventType,
    PipelineStageStatus,
)
from app.models.pipeline import CampaignContactAgentState
from app.models.verification_job import AgentJob
from app.services import pipeline
from app.services.agents import jobs as agent_jobs
from app.services.agents import orchestrator
from app.services.agents.controls import effective_control
from app.services.agents.registry import AGENT_SPECS
from app.services.audit import record_audit_event
from app.services.campaign_contacts import is_terminally_blocked, refresh_eligibility

#: The most contacts one re-run will touch. A ceiling rather than a page size: the
#: language-model and verification stages each spend real money per contact, so a
#: mis-aimed click must not be able to spend without limit. Anything dropped is
#: reported in :attr:`RerunOutcome.capped_at` — never silently truncated.
MAX_PER_RERUN = 200

#: Stage states a re-run is for. A stage that is running, queued, waiting or already
#: finished is not stopped, and re-running it would duplicate work or discard an
#: outcome.
STOPPED_STATES: tuple[PipelineStageStatus, ...] = (
    PipelineStageStatus.FAILED,
    PipelineStageStatus.BLOCKED,
)

#: Job states that mean this stage is already going to run. Re-running would create a
#: second job for the same turn.
IN_FLIGHT: tuple[AgentJobStatus, ...] = (
    AgentJobStatus.PENDING,
    AgentJobStatus.LEASED,
    AgentJobStatus.IN_PROGRESS,
)

#: Agents whose execution costs money per contact. Surfaced so the confirmation can
#: say so before a bulk re-run, not after.
SPENDS_PER_CONTACT: frozenset[AgentIdentifier] = frozenset(
    {
        AgentIdentifier.EMAIL,
        AgentIdentifier.VERIFICATION,
        AgentIdentifier.RESEARCH,
        AgentIdentifier.INSIGHTS,
        AgentIdentifier.PERSONALIZATION,
    }
)

MAX_REASON_LEN = 500


class RerunError(RuntimeError):
    """The re-run was refused outright, with a reason the page can show."""


@dataclass(frozen=True)
class RerunCandidate:
    """One contact this Agent has stopped on."""

    campaign_contact_id: uuid.UUID
    contact_id: uuid.UUID
    contact_label: str
    status: PipelineStageStatus
    attempt_count: int
    reason_code: str | None
    reason_detail: str | None
    #: Set when a re-run could not help this contact however many times it is
    #: pressed — a suppression, an exclusion, a membership that is not active. The
    #: contact still appears, because the stage really has stopped on them and
    #: hiding the row would make the Agent's own count unaccountable; but the page
    #: states the standing reason instead of offering a button that can only fail.
    standing_block: str | None = None

    @property
    def runnable(self) -> bool:
        return self.standing_block is None


@dataclass(frozen=True)
class RerunRefusal:
    """One contact that was not re-run, and why not."""

    campaign_contact_id: uuid.UUID
    contact_label: str
    code: str
    reason: str


@dataclass(frozen=True)
class RerunOutcome:
    """What a re-run did, in full. Every contact is accounted for."""

    agent_id: AgentIdentifier
    campaign_id: uuid.UUID
    requeued: tuple[uuid.UUID, ...] = ()
    refusals: tuple[RerunRefusal, ...] = ()
    #: Set when more contacts were eligible than :data:`MAX_PER_RERUN` allowed.
    capped_at: int | None = None
    generation: int | None = None

    @property
    def requeued_count(self) -> int:
        return len(self.requeued)

    @property
    def accepted(self) -> bool:
        return bool(self.requeued)

    def message(self) -> str:
        """One sentence an operator can act on."""

        spec = AGENT_SPECS[self.agent_id]
        if not self.requeued and not self.refusals:
            return f"Nothing was stopped at the {spec.display_name}, so nothing was re-run."
        parts: list[str] = []
        if self.requeued:
            noun = "contact" if self.requeued_count == 1 else "contacts"
            parts.append(
                f"{spec.display_name} queued again for {self.requeued_count} {noun}. "
                "Each one starts with a fresh attempt budget, and the earlier failure "
                "stays on record."
            )
        if self.refusals:
            count = len(self.refusals)
            subject = "contact was" if count == 1 else "contacts were"
            parts.append(f"{count} {subject} not re-run.")
        if self.capped_at is not None:
            parts.append(
                f"Stopped at the {self.capped_at}-contact ceiling; run it again for the rest."
            )
        return " ".join(parts)


def _label(contact: Contact | None) -> str:
    if contact is None:
        return "Unknown contact"
    name = " ".join(part for part in (contact.first_name, contact.last_name) if part).strip()
    return name or "Unnamed contact"


def _candidate_rows(
    session: Session, *, campaign_id: uuid.UUID, agent_id: AgentIdentifier
) -> list[tuple[CampaignContactAgentState, CampaignContact, Contact | None]]:
    """The stopped stages for one Agent, with the person each belongs to.

    Outer-joined to Contact so a membership whose contact row has gone (merged away)
    still appears rather than vanishing from the count the page just offered.
    """

    rows = list(
        session.execute(
            select(CampaignContactAgentState, CampaignContact, Contact)
            .join(
                CampaignContact,
                CampaignContact.id == CampaignContactAgentState.campaign_contact_id,
            )
            .outerjoin(Contact, Contact.id == CampaignContact.contact_id)
            .where(
                CampaignContact.campaign_id == campaign_id,
                CampaignContactAgentState.agent_id == agent_id,
                CampaignContactAgentState.status.in_(STOPPED_STATES),
            )
            .order_by(CampaignContactAgentState.updated_at.desc())
        ).all()
    )
    return [(state, membership, contact) for state, membership, contact in rows]


def failure_counts(session: Session, campaign_id: uuid.UUID) -> dict[str, int]:
    """Per Agent, how many contacts have *failed* on it.

    Counts FAILED only, deliberately, even though a re-run also accepts BLOCKED. This
    feeds a hint on the pipeline strip, and a hint must not promise what a click may
    not deliver: BLOCKED is usually a suppression, which outranks a re-run, and the
    per-contact eligibility that would tell them apart costs queries per contact — too
    much for a strip of nine tiles that refreshes every few seconds. A failure is the
    case a re-run is actually for, so the hint follows failures.

    This is a hint, not a gate. :func:`candidates` is what the panel reads, and it
    lists every stopped contact — blocked ones included, with the standing reason.
    """

    rows = session.execute(
        select(CampaignContactAgentState.agent_id, func.count(CampaignContactAgentState.id))
        .join(
            CampaignContact,
            CampaignContact.id == CampaignContactAgentState.campaign_contact_id,
        )
        .where(
            CampaignContact.campaign_id == campaign_id,
            CampaignContactAgentState.status == PipelineStageStatus.FAILED,
        )
        .group_by(CampaignContactAgentState.agent_id)
    ).all()
    return {agent_id.value: int(count) for agent_id, count in rows}


def candidates(
    session: Session, *, campaign_id: uuid.UUID, agent_id: AgentIdentifier
) -> tuple[RerunCandidate, ...]:
    """The contacts a re-run of this Agent would touch, newest failure first."""

    return tuple(
        RerunCandidate(
            campaign_contact_id=membership.id,
            contact_id=membership.contact_id,
            contact_label=_label(contact),
            status=state.status,
            attempt_count=state.attempt_count,
            reason_code=state.reason_code,
            reason_detail=state.reason_detail,
            standing_block=_standing_block(session, membership=membership),
        )
        for state, membership, contact in _candidate_rows(
            session, campaign_id=campaign_id, agent_id=agent_id
        )
    )


def _standing_block(session: Session, *, membership: CampaignContact) -> str | None:
    """Why a re-run could never help this contact, if that is the case.

    Read-only on purpose: :func:`rerun_stage` reaches the same verdict through
    ``refresh_eligibility``, which is authoritative *and* writes, and a page render
    must not write. The two can only disagree in the operator's favour — a suppression
    lifted a moment ago leaves this silent, and the re-run is what notices.
    """

    if membership.membership_status is not CampaignMembershipStatus.ACTIVE:
        return (
            f"This contact's membership is {membership.membership_status.value}. "
            "Resume it before running an Agent for them."
        )
    if is_terminally_blocked(session, membership=membership):
        return (
            "Blocked by something authoritative — usually the suppression list. "
            "That outranks a re-run, so lift it first if it is wrong."
        )
    return None


def _next_generation(
    session: Session, *, campaign_contact_id: uuid.UUID, agent_id: AgentIdentifier
) -> int:
    """The generation for this stage's next job.

    Derived from how many jobs this stage has already had rather than from a stored
    counter, which makes it idempotent for free: two operators pressing the button at
    the same moment compute the same generation, produce the same key, and
    ``enqueue_job`` collapses them into one job instead of two.
    """

    existing = (
        session.scalar(
            select(func.count(AgentJob.id)).where(
                AgentJob.campaign_contact_id == campaign_contact_id,
                AgentJob.agent_id == agent_id,
            )
        )
        or 0
    )
    return int(existing) + 1


def _clean_reason(reason: str | None) -> str:
    text = (reason or "").strip()
    return text[:MAX_REASON_LEN] if text else "Operator ran this Agent again."


def rerun_stage(
    session: Session,
    *,
    campaign_id: uuid.UUID,
    agent_id: AgentIdentifier,
    actor: str = "operator",
    reason: str | None = None,
    campaign_contact_id: uuid.UUID | None = None,
    limit: int = MAX_PER_RERUN,
) -> RerunOutcome:
    """Queue one Agent again for the contacts it has stopped on.

    Whole-campaign by default; pass ``campaign_contact_id`` for a single contact. The
    guards are identical either way, so a single re-run cannot do anything a bulk one
    would have refused.
    """

    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise RerunError("That campaign does not exist.")

    spec = AGENT_SPECS[agent_id]
    if not spec.implemented:
        raise RerunError(
            f"The {spec.display_name} has no executable adapter, so there is nothing to run."
        )
    if not campaign.execution_enabled:
        raise RerunError(
            "This campaign's execution is off, which holds every Agent but Capture. "
            "Turn execution on first — otherwise the work would be queued and "
            "immediately held again."
        )

    control = effective_control(session, campaign=campaign, agent_id=agent_id)
    if control.status is not AgentControlStatus.ENABLED:
        raise RerunError(
            f"The {spec.display_name} is {control.status.value} "
            f"(from {control.source.replace('_', ' ')})"
            f"{': ' + control.reason if control.reason else ''}. "
            "Enable it first, or the re-queued work would be held straight away."
        )

    rows = _candidate_rows(session, campaign_id=campaign_id, agent_id=agent_id)
    if campaign_contact_id is not None:
        rows = [row for row in rows if row[1].id == campaign_contact_id]
        if not rows:
            raise RerunError(
                "That contact is not stopped at this Agent, so there is nothing to run again."
            )

    ceiling = max(1, min(limit, MAX_PER_RERUN))
    capped_at = ceiling if len(rows) > ceiling else None
    rows = rows[:ceiling]

    note = _clean_reason(reason)
    requeued: list[uuid.UUID] = []
    refusals: list[RerunRefusal] = []
    generation: int | None = None

    for state, membership, contact in rows:
        label = _label(contact)
        refusal = _refuse_membership(session, membership=membership, actor=actor, label=label)
        if refusal is not None:
            refusals.append(refusal)
            continue

        in_flight = session.scalars(
            select(AgentJob).where(
                AgentJob.campaign_contact_id == membership.id,
                AgentJob.agent_id == agent_id,
                AgentJob.status.in_(IN_FLIGHT),
            )
        ).first()
        if in_flight is not None:
            refusals.append(
                RerunRefusal(
                    campaign_contact_id=membership.id,
                    contact_label=label,
                    code="already_in_flight",
                    reason=("This stage already has work queued or running, so it was left alone."),
                )
            )
            continue

        # Retire whatever is left of the stopped turn before starting a new one, so
        # the queue never holds two jobs for one stage.
        agent_jobs.cancel_jobs_for_stage(
            session,
            campaign_contact_id=membership.id,
            agent_id=agent_id,
            reason=f"Superseded by an operator re-run: {note}",
            actor=actor,
        )

        generation = _next_generation(session, campaign_contact_id=membership.id, agent_id=agent_id)
        pipeline.transition_stage(
            session,
            membership=membership,
            agent_id=agent_id,
            target=PipelineStageStatus.WAITING,
            event_type=PipelineEventType.STAGE_WAITING,
            actor=actor,
            reason_code="operator_rerun",
            reason_detail=note,
            detail={
                "previous_status": state.status.value,
                "previous_reason_code": state.reason_code,
                "generation": generation,
            },
        )
        # The stage is the one to run next again. `schedule_next` reads these.
        membership.current_stage = agent_id
        membership.next_stage = agent_id
        membership.pipeline_status = PipelineStageStatus.WAITING
        session.flush()

        job = orchestrator.schedule_next(
            session, membership=membership, actor=actor, generation=generation
        )
        if job is None:
            refusals.append(_not_scheduled(session, membership=membership, label=label))
            continue
        requeued.append(membership.id)

    record_audit_event(
        session,
        actor=actor,
        action="agent.operator_rerun",
        entity_type="campaign",
        entity_id=str(campaign_id),
        reason=note,
        context={
            "agent_id": agent_id.value,
            "requeued": len(requeued),
            "refused": len(refusals),
            "capped_at": capped_at,
            "scope": "contact" if campaign_contact_id is not None else "campaign",
            "spends_per_contact": agent_id in SPENDS_PER_CONTACT,
        },
    )
    session.flush()
    return RerunOutcome(
        agent_id=agent_id,
        campaign_id=campaign_id,
        requeued=tuple(requeued),
        refusals=tuple(refusals),
        capped_at=capped_at,
        generation=generation,
    )


def _not_scheduled(session: Session, *, membership: CampaignContact, label: str) -> RerunRefusal:
    """Why the pipeline declined to queue a stage it was asked to.

    Almost always an unmet dependency — you cannot re-run Research for a contact whose
    Company stage never finished — and the pipeline *knows which one*: `schedule_next`
    records it on the stage as ``waiting_on_agent``. Reading it back turns a shrug into
    an instruction, which is the difference between the operator fixing this and
    filing a bug.
    """

    agent_id = membership.next_stage
    state = (
        pipeline.agent_state(
            session,
            campaign_contact_id=membership.id,
            agent_id=agent_id,
            create=False,
        )
        if agent_id is not None
        else None
    )
    blocker = state.waiting_on_agent if state is not None else None
    if blocker is not None:
        reason = (
            f"{AGENT_SPECS[blocker].display_name} has not completed for this contact, and "
            f"{AGENT_SPECS[agent_id].display_name if agent_id else 'this Agent'} cannot run "
            "before it. Deal with that stage first."
        )
    elif state is not None and state.reason_detail:
        reason = state.reason_detail
    else:
        reason = (
            "The pipeline declined to queue this stage and recorded no reason. Open the "
            "contact to see its stage history."
        )
    return RerunRefusal(
        campaign_contact_id=membership.id,
        contact_label=label,
        code=state.reason_code if state is not None and state.reason_code else "not_scheduled",
        reason=reason,
    )


def _refuse_membership(
    session: Session, *, membership: CampaignContact, actor: str, label: str
) -> RerunRefusal | None:
    """Per-contact guards. Each refusal names itself so the page can explain it."""

    if membership.membership_status is not CampaignMembershipStatus.ACTIVE:
        return RerunRefusal(
            campaign_contact_id=membership.id,
            contact_label=label,
            code="membership_" + membership.membership_status.value,
            reason=(
                f"This contact's membership is {membership.membership_status.value}. "
                "Resume it before running an Agent for them."
            ),
        )
    # Re-evaluated rather than trusted: a suppression may have been added since the
    # stage failed, and suppression outranks every operator action including this one.
    if refresh_eligibility(session, membership=membership, actor=actor):
        return RerunRefusal(
            campaign_contact_id=membership.id,
            contact_label=label,
            code="eligibility_blocked",
            reason=(
                "This contact is blocked by something authoritative — usually the "
                "suppression list. That outranks a re-run."
            ),
        )
    return None
