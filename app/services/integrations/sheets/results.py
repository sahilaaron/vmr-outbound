"""Reading a submitted row's current state back out, in four words.

The whole read model. It writes nothing, it starts nothing, and it invents no
state of its own: every value below is projected from what the pipeline, the
verification policy and the sequence store already say.

The four words
--------------

``pending``            accepted, waiting its turn
``processing``         an Agent currently holds it
``ready``              a usable address **and** a validated sequence
``could_not_prepare``  it stopped, and a person has to do something

``pending`` is a promise that the row will move, so it is only ever said of a row
that still can. A row whose preparation has ended — however it ended — is
reported as stopped rather than left waiting; see :func:`_parked_reason`.

They are deliberately not the nine Agent names. A salesperson reading a
spreadsheet is deciding whether to wait, whether to act, or whether the row is
finished, and "waiting on the Insights Agent" answers none of those. The stage
detail still exists, is still authoritative and is one click away in the app.

Why ``ready`` requires *both* halves
------------------------------------

The output contract of this surface is an address to write to and seven messages
to send. An address with no sequence is not usable, and seven messages with
nowhere to send them are worse than nothing. So the row turns ``ready`` only when
both exist: the sequence generated complete, validated and exactly seven messages
long, and an address that is either ``VALID`` under the existing verification
policy — not catch-all, not role-based, not guessed — or one the operator
supplied themselves and the pipeline therefore never tried to verify. Anything
less stays ``processing``, because it is.

``ready`` is not, and has never been, a deliverability claim. It says a package
exists and where the operator asked it to go. Which of the two kinds of address a
row holds is recorded on the Verification stage and shown truthfully everywhere
verification status is displayed; see :func:`_usable_address`.

Why a stopped row is not simply "failed"
----------------------------------------

Three different things stop a row and they call for three different actions: a
suppressed identity is a decision the system is defending, a blocked stage is
usually missing configuration, and a failed stage is usually a bad input. Each
gets its own sentence, sanitized, and none of them names a provider, a key or an
internal identifier.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.campaign import CampaignContact
from app.models.contact import Contact
from app.models.email_sequence import SEQUENCE_LENGTH, EmailSequence
from app.models.enums import (
    AgentIdentifier,
    ContactWorkflowState,
    EmailVisualStatus,
    PipelineStageStatus,
    SequenceGenerationStatus,
    SequenceValidationStatus,
)
from app.models.pipeline import CampaignContactAgentState
from app.services import campaign_access, campaign_contacts, customer_status
from app.services.integrations.sheets.contract import RowStatus
from app.services.sequences import read as sequence_read
from app.services.verification import status as verification_status
from app.services.workbench_agents.sanitize import sanitize_text

#: Stage states that mean a person must act before the row can move again.
_STOPPED_STAGE_STATES = frozenset({PipelineStageStatus.FAILED, PipelineStageStatus.BLOCKED})

#: Stage states that mean an Agent is switched off rather than the row being
#: wrong. Reported as still pending, with a note, because the fix is a control an
#: administrator flips and not an edit the salesperson can make in the sheet.
_HELD_STAGE_STATES = frozenset({PipelineStageStatus.PAUSED, PipelineStageStatus.DISABLED})

_SUPPRESSED_MESSAGE = (
    "this person or their company is on the suppression list, so no outreach was prepared"
)
_GENERIC_STOP_MESSAGE = (
    "this row stopped before an outbound package was produced; open it in VMR Outbound to see why"
)


@dataclass(frozen=True)
class SequenceMessage:
    """One message of the canonical seven, as the sheet receives it."""

    sequence_index: int
    elapsed_day: int
    subject: str
    body: str


@dataclass(frozen=True)
class RowResult:
    """One submitted row's current answer."""

    submission_id: uuid.UUID
    status: RowStatus
    email_address: str | None = None
    messages: tuple[SequenceMessage, ...] = ()
    safe_failure_reason: str | None = None
    note: str | None = None
    contact_id: uuid.UUID | None = None
    campaign_id: uuid.UUID | None = None
    updated_at: datetime | None = None

    @property
    def ready(self) -> bool:
        return self.status is RowStatus.READY


def results_for(
    session: Session,
    *,
    submission_ids: list[uuid.UUID],
    actor: campaign_access.CampaignActor,
) -> list[RowResult]:
    """Project each submission this account may read. Unknown ids are omitted.

    Omitted rather than reported as missing: telling a caller that an id exists
    but belongs to somebody else is the difference between a result set and an
    enumeration oracle. An id this account cannot reach and an id that was never
    minted produce the same silence, and the add-on treats both as "no answer
    yet".
    """

    accessible = campaign_access.accessible_campaign_ids(session, actor)
    results: list[RowResult] = []
    for submission_id in submission_ids:
        membership = session.get(CampaignContact, submission_id)
        if membership is None:
            continue
        if accessible is not None and membership.campaign_id not in accessible:
            continue
        results.append(result_for(session, membership=membership))
    return results


def result_for(session: Session, *, membership: CampaignContact) -> RowResult:
    """Project one membership. The only place the four words are decided."""

    contact = session.get(Contact, membership.contact_id)

    def answer(
        status: RowStatus,
        *,
        email_address: str | None = None,
        messages: tuple[SequenceMessage, ...] = (),
        safe_failure_reason: str | None = None,
        note: str | None = None,
    ) -> RowResult:
        return RowResult(
            submission_id=membership.id,
            status=status,
            email_address=email_address,
            messages=messages,
            safe_failure_reason=safe_failure_reason,
            note=note,
            contact_id=membership.contact_id,
            campaign_id=membership.campaign_id,
            updated_at=membership.updated_at,
        )

    stop = _stop_reason(session, membership=membership)
    if stop is not None:
        return answer(RowStatus.COULD_NOT_PREPARE, safe_failure_reason=stop)

    address = _usable_address(session, membership=membership, contact=contact)
    sequence = sequence_read.sequence_for_membership(session, campaign_contact_id=membership.id)
    messages = _messages(session, sequence=sequence)

    if address is not None and messages:
        return answer(RowStatus.READY, email_address=address, messages=messages)

    # Asked only after Ready has been ruled out, because the questions are
    # different: "has preparation stopped" is not "did preparation succeed", and
    # a row holding a complete package is finished no matter which stage its
    # history ends on.
    parked = _parked_reason(session, membership=membership)
    if parked is not None:
        return answer(
            RowStatus.COULD_NOT_PREPARE, email_address=address, safe_failure_reason=parked
        )

    return answer(
        _in_flight_status(membership),
        email_address=address,
        note=_held_note(session, membership=membership),
    )


def _usable_address(
    session: Session, *, membership: CampaignContact, contact: Contact | None
) -> str | None:
    """The address this row may be sent to, or nothing.

    Two ways to have one, and the difference between them is never blurred.

    **Verified.** ``VALID`` and only ``VALID``. ``CATCH_ALL`` and ``UNKNOWN`` are
    unresolved by definition, ``ROLE_BASED`` is a real mailbox that policy still
    refuses, and a vendor's claim in a file is not a verification. Each of those
    is already decided by ``app/services/verification``; this reads the decision
    and does not restate it.

    **Supplied.** The operator handed this Campaign the address, so the pipeline
    was never going to acquire evidence about it — no candidate was generated and
    no provider was called, by design. Requiring a verdict here would have made
    that address unusable on the very surface that accepted it: the row would
    have sat at ``processing`` forever with its seven messages written and an
    address the operator typed themselves sitting beside them, waiting for a
    provider answer that nothing was ever going to ask for.

    The second branch reads the **durable Verification stage**, not the address
    and not the intake record. A stage that completed through a named bypass is
    the pipeline's own committed statement that the address requirement was
    satisfied without verification — the same fact, in the same place, whichever
    surface supplied the value, and one that a later re-run or correction moves
    on its own.

    What this deliberately does not do is call a supplied address verified. It
    returns an address; it does not touch ``EmailVisualStatus``, write evidence,
    or map anything to ``SUCCESSFUL``. Every screen reading verification status
    still says, correctly, that nobody checked this mailbox.
    """

    if contact is None:
        return None
    view = verification_status.derive_status_for_contact(session, contact)
    if view.visual is EmailVisualStatus.SUCCESSFUL:
        return view.email
    if contact.email and _verification_bypassed(session, membership=membership):
        return contact.email
    return None


#: Reason codes the Verification stage carries when it completed without a
#: provider ever being called. Both are truthful absences written by the
#: orchestrator; neither is a deliverability claim.
_VERIFICATION_BYPASSES = frozenset(
    {
        "verification_bypassed_imported_email",
        "verification_bypassed_supplied_email",
    }
)


def _verification_bypassed(session: Session, *, membership: CampaignContact) -> bool:
    """Whether Verification completed as a recorded bypass for this membership."""

    state = session.scalars(
        select(CampaignContactAgentState).where(
            CampaignContactAgentState.campaign_contact_id == membership.id,
            CampaignContactAgentState.agent_id == AgentIdentifier.VERIFICATION,
        )
    ).one_or_none()
    if state is None or state.status is not PipelineStageStatus.COMPLETED:
        return False
    return state.reason_code in _VERIFICATION_BYPASSES


def _messages(session: Session, *, sequence: EmailSequence | None) -> tuple[SequenceMessage, ...]:
    """The seven messages, or nothing at all. Never a partial sequence."""

    if sequence is None:
        return ()
    if sequence.generation_status is not SequenceGenerationStatus.COMPLETE:
        return ()
    if sequence.validation_status is SequenceValidationStatus.FAILED:
        return ()
    details = sequence_read.message_details(session, sequence=sequence)
    if len(details) != SEQUENCE_LENGTH:
        # A sequence is never persisted partial, so this is a belt-and-braces
        # refusal rather than an expected branch: writing four messages into a
        # seven-column layout would read as a finished sequence with three blanks.
        return ()
    return tuple(
        SequenceMessage(
            sequence_index=detail.row.position,
            elapsed_day=detail.row.recommended_elapsed_day,
            subject=detail.row.subject,
            body=detail.body,
        )
        for detail in details
    )


def _stop_reason(session: Session, *, membership: CampaignContact) -> str | None:
    """Why this row stopped, in one operator sentence, or ``None`` if it has not."""

    if membership.state is ContactWorkflowState.SUPPRESSED:
        return _SUPPRESSED_MESSAGE
    if membership.state is ContactWorkflowState.EXCLUDED:
        return sanitize_text(_first_detail(membership)) or _GENERIC_STOP_MESSAGE
    if campaign_contacts.is_terminally_blocked(session, membership=membership):
        if _suppression_named(membership):
            return _SUPPRESSED_MESSAGE
        return sanitize_text(_first_detail(membership)) or _GENERIC_STOP_MESSAGE
    if membership.pipeline_status in _STOPPED_STAGE_STATES:
        stage = _current_stage(session, membership=membership)
        detail = stage.reason_detail if stage is not None else None
        return sanitize_text(detail) or _GENERIC_STOP_MESSAGE
    return None


def _parked_reason(session: Session, *, membership: CampaignContact) -> str | None:
    """Why this row will not move again, or ``None`` if it still might.

    ``pending`` is a promise: the row was accepted and is waiting its turn. A row
    that has run out of turns and produced no package is not waiting for
    anything, and reporting it as pending told a salesperson to keep refreshing a
    cell that would never change. That is exactly what the live campaign did — it
    walked into a Sending stage that is disabled, has no adapter and cannot be
    enabled, and every read after that said "pending" forever.

    Two questions, and the order matters.

    The canonical customer model is asked first, so the sheet and the app cannot
    disagree about whether somebody is finished — a stopped row is stopped in
    both vocabularies or in neither. Then the sheet asks its own, because it
    holds a stricter definition of a usable package: a ``VALID`` address, not
    merely an address. A pipeline that ran to its end is finished whatever the
    app makes of what it produced, and this surface has already ruled Ready out
    by the time it gets here.

    What that ordering protects is the ordinary case: a row whose address is
    still being resolved has a next stage, so neither question fires and it stays
    pending, which is exactly what it is.
    """

    if (
        customer_status.status_for_membership(session, campaign_contact_id=membership.id)
        is customer_status.CustomerContactStatus.COULD_NOT_PREPARE
    ) or (
        membership.next_stage is None
        and membership.pipeline_status is PipelineStageStatus.COMPLETED
    ):
        stage = _current_stage(session, membership=membership)
        detail = sanitize_text(stage.reason_detail) if stage is not None else None
        return detail or _GENERIC_STOP_MESSAGE
    return None


def _in_flight_status(membership: CampaignContact) -> RowStatus:
    if membership.pipeline_status in (
        PipelineStageStatus.RUNNING,
        PipelineStageStatus.RETRYING,
    ):
        return RowStatus.PROCESSING
    return RowStatus.PENDING


def _held_note(session: Session, *, membership: CampaignContact) -> str | None:
    """A note for a row that is waiting on a control rather than on a queue."""

    if membership.pipeline_status not in _HELD_STAGE_STATES:
        return None
    stage = _current_stage(session, membership=membership)
    detail = sanitize_text(stage.reason_detail) if stage is not None else None
    return detail or "an Agent this row needs is currently switched off"


def _current_stage(
    session: Session, *, membership: CampaignContact
) -> CampaignContactAgentState | None:
    if membership.current_stage is None:
        return None
    return session.scalars(
        select(CampaignContactAgentState).where(
            CampaignContactAgentState.campaign_contact_id == membership.id,
            CampaignContactAgentState.agent_id == membership.current_stage,
        )
    ).first()


def _first_detail(membership: CampaignContact) -> str | None:
    for reason in membership.blocking_reasons or []:
        if isinstance(reason, dict) and reason.get("detail"):
            return str(reason["detail"])
    return None


def _suppression_named(membership: CampaignContact) -> bool:
    for reason in membership.blocking_reasons or []:
        if isinstance(reason, dict) and "suppress" in str(reason.get("code", "")).lower():
            return True
    return False


__all__ = ["RowResult", "SequenceMessage", "result_for", "results_for"]
