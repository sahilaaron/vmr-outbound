"""What a customer is told about one contact, and nothing more.

VMR Outbound is autonomous until Ready for Sending. The customer creates a
campaign, adds contacts, and waits; the system does the nine-Agent work. So the
customer-facing vocabulary for a contact is three words, not nine stages and not
a failure taxonomy:

* **Processing** — the system is still working on this person.
* **Ready for Sending** — the usable outbound package exists.
* **Could not prepare** — the system stopped and will not produce one.

This module is a *projection*. It reads committed state and writes nothing. It
adds no column, no table and no migration, and it does not replace the durable
Agent/job state machine underneath — ``PipelineStageStatus``,
``CampaignContactAgentState`` and the job queue are untouched and remain the
authority for Admin diagnostics. What changes is only what the customer is
asked to read.

## Why "Could not prepare" is a status and not a task

Every condition in the terminal bucket is a machine outcome: a suppression the
ledger recorded, a stage that failed in a way a retry cannot fix, a pipeline
that ran out of stages without producing messages. None of them is work the
customer owes the system. They are shown so nobody is left wondering where a
contact went — not so somebody clears them.

The one thing that genuinely *is* the customer's is campaign setup, and that is
deliberately not modelled here. See :mod:`app.services.agents.readiness` and the
seller Knowledge Base readiness report for that; keeping the two apart is the
point, because "you have not switched this campaign on" and "the Research Agent
failed" are different sentences and must never share a counter.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, and_, case, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import Case

from app.models.campaign import CampaignContact
from app.models.contact import Contact
from app.models.email_sequence import (
    SEQUENCE_LENGTH,
    EmailSequence,
    EmailSequenceMessage,
    EmailSequenceMessageVersion,
)
from app.models.enums import (
    CampaignContactEligibility,
    ContactWorkflowState,
    PipelineStageStatus,
    SequenceGenerationStatus,
    SequenceStopState,
    SequenceValidationStatus,
)
from app.models.pipeline import CampaignContactAgentState
from app.services.agents.registry import AGENTS_WITHOUT_ADAPTER


class CustomerContactStatus(enum.StrEnum):
    """The whole customer-facing vocabulary for one contact."""

    PROCESSING = "processing"
    READY_FOR_SENDING = "ready_for_sending"
    COULD_NOT_PREPARE = "could_not_prepare"


#: What each status is called on screen. One place, so two pages cannot drift.
STATUS_LABELS: dict[CustomerContactStatus, str] = {
    CustomerContactStatus.PROCESSING: "Processing",
    CustomerContactStatus.READY_FOR_SENDING: "Ready for Sending",
    CustomerContactStatus.COULD_NOT_PREPARE: "Could not prepare",
}

#: A one-line explanation per status, written as fact rather than instruction.
STATUS_NOTES: dict[CustomerContactStatus, str] = {
    CustomerContactStatus.PROCESSING: (
        "VMR is still working on this person. Nothing is needed from you."
    ),
    CustomerContactStatus.READY_FOR_SENDING: (
        "The seven-message sequence is written and valid. Read, edit or copy it "
        "whenever you like; sending is yours to do."
    ),
    CustomerContactStatus.COULD_NOT_PREPARE: (
        "VMR stopped on this person and will not produce messages for them. The "
        "recorded reason is on the contact."
    ),
}

#: The tone class each status carries in the customer templates. "Could not
#: prepare" is deliberately neutral rather than an alarm: it is an outcome, not
#: an obligation, and colouring it red would rebuild the red inbox this model
#: exists to remove.
STATUS_TONES: dict[CustomerContactStatus, str] = {
    CustomerContactStatus.PROCESSING: "",
    CustomerContactStatus.READY_FOR_SENDING: "ok",
    CustomerContactStatus.COULD_NOT_PREPARE: "",
}


@dataclass(frozen=True)
class CustomerProgress:
    """How many contacts stand in each of the three states."""

    total: int = 0
    processing: int = 0
    ready_for_sending: int = 0
    could_not_prepare: int = 0

    @property
    def has_contacts(self) -> bool:
        return self.total > 0


def _complete_package_statement() -> Select[tuple[uuid.UUID]]:
    """Memberships that hold a complete, valid, live seven-message sequence.

    This is the artifact test, and it is deliberately the artifact rather than a
    stage flag. Personalization is skippable: a campaign with the Agent switched
    off steps over it and every contact reports the chain complete while no
    message exists anywhere. Asking the messages themselves closes that hole —
    seven current versions, generated completely, validation not failed, nothing
    superseded, nothing stopped.

    Note what is *not* here: any review or approval row. A generated, valid
    sequence is ready. Nobody has to click anything for it to become ready, and
    the absence of a review row is not a backlog.
    """

    return (
        select(EmailSequence.campaign_contact_id)
        .join(
            EmailSequenceMessage,
            EmailSequenceMessage.sequence_key == EmailSequence.sequence_key,
        )
        .join(
            EmailSequenceMessageVersion,
            EmailSequenceMessageVersion.message_id == EmailSequenceMessage.id,
        )
        .where(
            EmailSequence.superseded_at.is_(None),
            EmailSequenceMessageVersion.superseded_at.is_(None),
            EmailSequence.generation_status == SequenceGenerationStatus.COMPLETE,
            EmailSequence.validation_status != SequenceValidationStatus.FAILED,
            EmailSequence.stop_state != SequenceStopState.STOPPED,
        )
        .group_by(EmailSequence.id, EmailSequence.campaign_contact_id)
        .having(func.count(EmailSequenceMessageVersion.id) == SEQUENCE_LENGTH)
    )


def _unrecoverable_stage_statement() -> Select[tuple[uuid.UUID]]:
    """Memberships holding a stage failure that no retry can clear.

    ``retryable`` is written by the adapter that failed, so this asks the record
    rather than guessing from an error class.
    """

    return select(CampaignContactAgentState.campaign_contact_id).where(
        CampaignContactAgentState.status == PipelineStageStatus.FAILED,
        CampaignContactAgentState.retryable.is_(False),
    )


def _permanently_disabled_stage_statement() -> Select[tuple[uuid.UUID]]:
    """Memberships parked at a stage no operator action can ever start.

    A disabled stage is normally a wait, not an outcome: an administrator flips
    the control and the work resumes, which is why "disabled" belongs in
    Processing. An Agent with no executable adapter is the exception — the
    control service refuses to enable one, so nothing about the deployment can
    change and the contact is not waiting for anything.

    That is exactly the state a finished contact used to be left in. It reported
    Processing forever while the package it had already produced sat beside it,
    which is why the boundary in
    :func:`~app.services.agents.registry.next_preparation_agent` exists. This
    stays anyway, and stays *after* the package test: a contact that reached this
    state before that boundary existed is still owed a truthful answer, and a
    contact holding a complete package is Ready regardless of what stage 9 says.
    """

    return select(CampaignContactAgentState.campaign_contact_id).where(
        CampaignContactAgentState.status == PipelineStageStatus.DISABLED,
        CampaignContactAgentState.agent_id.in_(AGENTS_WITHOUT_ADAPTER),
    )


def _status_expression() -> Case[str]:
    """The single rule, as one SQL expression used by every caller.

    Order matters and is the argument:

    1. **Policy stops first.** A suppressed, excluded or terminally blocked
       membership is "could not prepare" even if messages happen to exist, because
       a written sequence is not permission to contact somebody who is on the
       suppression ledger.
    2. **Then the package.** Company resolution, research, a usable address and
       verification are already prerequisites of generation under existing
       policy, so a complete valid sequence *is* the evidence that they passed.
       The address is re-asserted here anyway: a package with nowhere to send it
       is not ready.
    3. **Then the other terminal stops** — a blocked pipeline, an unrecoverable
       stage, a stage disabled on an Agent that can never be enabled, or a
       pipeline that reached its end without producing a package.
    4. **Everything else is Processing**, including waiting, running, retrying
       and paused — every state something can still move out of. The customer
       does not need to tell those apart; Admin does, and Admin still can.
    """

    policy_stopped = or_(
        CampaignContact.eligibility_status == CampaignContactEligibility.BLOCKED,
        CampaignContact.state.in_((ContactWorkflowState.EXCLUDED, ContactWorkflowState.SUPPRESSED)),
    )
    package_ready = and_(
        Contact.email.is_not(None),
        CampaignContact.id.in_(_complete_package_statement()),
    )
    stopped_without_package = or_(
        CampaignContact.pipeline_status == PipelineStageStatus.BLOCKED,
        CampaignContact.id.in_(_unrecoverable_stage_statement()),
        CampaignContact.id.in_(_permanently_disabled_stage_statement()),
        and_(
            CampaignContact.next_stage.is_(None),
            CampaignContact.pipeline_status == PipelineStageStatus.COMPLETED,
        ),
    )
    return case(
        (policy_stopped, CustomerContactStatus.COULD_NOT_PREPARE.value),
        (package_ready, CustomerContactStatus.READY_FOR_SENDING.value),
        (stopped_without_package, CustomerContactStatus.COULD_NOT_PREPARE.value),
        else_=CustomerContactStatus.PROCESSING.value,
    )


def status_expression() -> Case[str]:
    """The customer status of a ``CampaignContact`` row joined to its ``Contact``.

    Public so a caller building its own roster query can select or filter by
    the same rule rather than restating it. The statement must join ``Contact``.
    """

    return _status_expression()


def _scoped(statement: Select[Any], campaign_id: uuid.UUID | None) -> Select[Any]:
    joined: Select[Any] = statement.join(Contact, Contact.id == CampaignContact.contact_id)
    if campaign_id is not None:
        joined = joined.where(CampaignContact.campaign_id == campaign_id)
    return joined


def progress(session: Session, *, campaign_id: uuid.UUID | None = None) -> CustomerProgress:
    """The three counts, for one campaign or for everything the caller can see.

    One grouped query regardless of how many contacts exist, so the overview page
    costs the same on a campaign of ten and a campaign of ten thousand.
    """

    expression = _status_expression()
    rows = session.execute(
        _scoped(select(expression, func.count(CampaignContact.id)), campaign_id).group_by(
            expression
        )
    ).all()
    counts = {str(bucket): int(count) for bucket, count in rows}
    processing = counts.get(CustomerContactStatus.PROCESSING.value, 0)
    ready = counts.get(CustomerContactStatus.READY_FOR_SENDING.value, 0)
    stopped = counts.get(CustomerContactStatus.COULD_NOT_PREPARE.value, 0)
    return CustomerProgress(
        total=processing + ready + stopped,
        processing=processing,
        ready_for_sending=ready,
        could_not_prepare=stopped,
    )


def statuses_for_campaign(
    session: Session, *, campaign_id: uuid.UUID
) -> dict[uuid.UUID, CustomerContactStatus]:
    """Every membership's status in one campaign, in one query.

    Keyed by ``campaign_contact_id`` because that is what a roster row already
    carries, so rendering a page of contacts costs one statement rather than one
    per row.
    """

    rows = session.execute(
        _scoped(select(CampaignContact.id, _status_expression()), campaign_id)
    ).all()
    return {membership_id: CustomerContactStatus(bucket) for membership_id, bucket in rows}


def status_for_membership(
    session: Session, *, campaign_contact_id: uuid.UUID
) -> CustomerContactStatus | None:
    """One membership's status, or ``None`` when the membership does not exist."""

    row = session.execute(
        _scoped(select(_status_expression()), None).where(CampaignContact.id == campaign_contact_id)
    ).first()
    if row is None:
        return None
    return CustomerContactStatus(row[0])


def label(status: CustomerContactStatus) -> str:
    return STATUS_LABELS[status]
