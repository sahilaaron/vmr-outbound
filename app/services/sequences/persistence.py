"""Turning a validated sequence into durable rows, atomically.

Two properties this module exists to guarantee.

**A partial sequence never looks finished.** Every row for a sequence is added
in one ``flush``, inside whatever transaction the caller owns. If message 7
cannot be written, message 1 is not written either. There is no code path that
persists six messages and reports a complete sequence, because there is no code
path that persists messages one at a time.

**An unchanged input does not spend twice.** ``existing_for_digest`` is
consulted before the model is called, not after. Two runs with the same digest
resolve to the same sequence row, and the second one costs nothing. That is
also what makes a retry after a transient failure safe: the retry finds the
committed sequence and returns it.

The one case that *does* re-spend is a persistence failure after a successful
model call. Nothing intermediate is committed -- no raw model output, no
half-validated draft -- so a rolled-back transaction leaves no trace to replay
from, and the retry calls the model again. That is a deliberate choice: the
alternative is committing unvalidated model output outside the sequence
transaction, which buys back one call's cost by creating a row that is not yet
known to be safe. See ``docs/EMAIL_SEQUENCE.md`` for the full replay contract.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.campaign import CampaignContact
from app.models.contact import Contact
from app.models.email_sequence import (
    EmailSequence,
    EmailSequenceMessage,
    EmailSequenceMessageVersion,
)
from app.models.enums import (
    SequenceGenerationStatus,
    SequenceMessageOrigin,
    SequenceReviewState,
    SequenceStopState,
    SequenceValidationStatus,
)
from app.services.audit import record_audit_event
from app.services.personalization.sequence import GeneratedSequence

SYSTEM_ACTOR = "system:personalization-agent"


def existing_for_digest(
    session: Session, *, campaign_contact_id: uuid.UUID, input_digest: str
) -> EmailSequence | None:
    """The live sequence for this membership if it already has this digest.

    Only the *live* sequence counts. A superseded sequence carrying the same
    digest means the inputs came back around to a state they held before, and
    the operator who regenerated is entitled to a current sequence rather than
    a resurrected one.
    """

    return session.scalars(
        select(EmailSequence)
        .where(
            EmailSequence.campaign_contact_id == campaign_contact_id,
            EmailSequence.input_digest == input_digest,
            EmailSequence.superseded_at.is_(None),
        )
        .limit(1)
    ).first()


def current_sequence(session: Session, *, campaign_contact_id: uuid.UUID) -> EmailSequence | None:
    """The one live sequence for this membership, if any."""

    return session.scalars(
        select(EmailSequence)
        .where(
            EmailSequence.campaign_contact_id == campaign_contact_id,
            EmailSequence.superseded_at.is_(None),
        )
        .limit(1)
    ).first()


def _next_sequence_version(session: Session, *, sequence_key: uuid.UUID) -> int:
    existing = session.scalars(
        select(EmailSequence.sequence_version)
        .where(EmailSequence.sequence_key == sequence_key)
        .order_by(EmailSequence.sequence_version.desc())
        .limit(1)
    ).first()
    return int(existing) + 1 if existing else 1


def _next_message_version(session: Session, *, message_id: uuid.UUID) -> int:
    existing = session.scalars(
        select(EmailSequenceMessageVersion.message_version)
        .where(EmailSequenceMessageVersion.message_id == message_id)
        .order_by(EmailSequenceMessageVersion.message_version.desc())
        .limit(1)
    ).first()
    return int(existing) + 1 if existing else 1


def persist_sequence(
    session: Session,
    *,
    membership: CampaignContact,
    contact: Contact,
    generated: GeneratedSequence,
    agent_job_id: uuid.UUID | None = None,
    actor: str = SYSTEM_ACTOR,
) -> EmailSequence:
    """Persist one validated sequence and supersede any predecessor.

    The caller owns the transaction. This function adds and flushes; it does not
    commit, so a caller that fails afterwards rolls the whole sequence back
    rather than leaving a fragment.

    Regeneration reuses the existing ``sequence_key`` and the seven existing
    logical message rows. That is what makes the sequence's identity -- and each
    message's identity -- survive a regeneration, which a future delivery
    adapter depends on. Only the *content versions* are new.
    """

    previous = current_sequence(session, campaign_contact_id=membership.id)
    sequence_key = previous.sequence_key if previous is not None else uuid.uuid4()
    version_number = _next_sequence_version(session, sequence_key=sequence_key)

    if previous is not None:
        # Supersede before inserting: the partial unique index permits exactly
        # one live sequence per membership, so the old one must step aside in
        # the same statement order, not merely in the same transaction.
        previous.superseded_at = _now(session)
        previous.review_state = SequenceReviewState.SUPERSEDED
        session.flush()

    sequence = EmailSequence(
        sequence_key=sequence_key,
        sequence_version=version_number,
        campaign_contact_id=membership.id,
        campaign_id=membership.campaign_id,
        contact_id=membership.contact_id,
        company_id=contact.company_id,
        agent_job_id=agent_job_id,
        input_digest=generated.input_digest,
        producer=generated.producer,
        producer_version=generated.producer_version,
        sequence_producer_version=generated.sequence_producer_version,
        validation_policy_version=generated.validation_policy_version,
        personalization_policy_version_id=generated.policy_version_id,
        personalization_policy_version_number=generated.policy_version_number,
        personalization_strategy_id=generated.strategy_id,
        personalization_decision=generated.decision.summary(),
        research_lineage=generated.research_lineage,
        insights_lineage=generated.insights_lineage,
        intelligence_lineage=generated.intelligence_lineage,
        cadence_source=generated.cadence.source,
        planned_span_days=generated.cadence.span_days,
        message_count=len(generated.messages),
        generation_status=SequenceGenerationStatus.COMPLETE,
        validation_status=(
            SequenceValidationStatus.PASSED_WITH_WARNINGS
            if generated.warnings
            else SequenceValidationStatus.PASSED
        ),
        review_state=SequenceReviewState.NEEDS_REVIEW,
        validation_findings=generated.validation_findings,
        stop_state=SequenceStopState.RUNNING,
        # Deliberately null. Nothing is approved at generation time, so claiming
        # position 1 is actionable would be a claim about a decision nobody has
        # made. refresh_aggregate below computes the truthful value.
        current_actionable_position=None,
        created_by=actor,
    )
    session.add(sequence)
    session.flush()

    if previous is not None:
        previous.superseded_by_id = sequence.id

    logical = _ensure_logical_messages(session, membership=membership, sequence_key=sequence_key)
    intelligence = generated.decision.intelligence
    accepted = intelligence.accepted_count if intelligence is not None else 0
    excluded = intelligence.excluded_count if intelligence is not None else 0

    for message in generated.messages:
        row = logical[message.position]
        _supersede_current_version(session, message_id=row.id)
        session.add(
            EmailSequenceMessageVersion(
                message_id=row.id,
                sequence_id=sequence.id,
                message_version=_next_message_version(session, message_id=row.id),
                position=message.position,
                subject=message.subject,
                body=message.body,
                recommended_delay_days=message.recommended_delay_days,
                recommended_elapsed_day=message.recommended_elapsed_day,
                origin=(
                    SequenceMessageOrigin.REGENERATED
                    if previous is not None
                    else SequenceMessageOrigin.GENERATED
                ),
                generation_status=SequenceGenerationStatus.COMPLETE,
                validation_status=(
                    SequenceValidationStatus.PASSED_WITH_WARNINGS
                    if message.warnings
                    else SequenceValidationStatus.PASSED
                ),
                personalization_policy_version_id=generated.policy_version_id,
                personalization_strategy_id=generated.strategy_id,
                context_decision=generated.decision.summary(),
                context_used=message.context_used,
                evidence_insight_ids=list(message.evidence_insight_ids),
                intelligence_accepted_count=accepted,
                intelligence_excluded_count=excluded,
                warnings=list(message.warnings),
                created_by=actor,
            )
        )
    # One flush for all seven. A constraint violation on any of them -- a
    # duplicate position, an invalid predecessor, an out-of-range delay --
    # fails here, before anything reports a complete sequence.
    session.flush()

    from app.services.sequences.review import refresh_aggregate

    refresh_aggregate(session, sequence=sequence)

    record_audit_event(
        session,
        actor=actor,
        action="email_sequence.generated",
        entity_type="email_sequence",
        entity_id=str(sequence.id),
        new_state=f"v{version_number}",
        reason=(
            "seven-message sequence written by the Personalization Agent; "
            "not approved and not sendable"
        ),
        context={
            "campaign_contact_id": str(membership.id),
            "sequence_key": str(sequence_key),
            "sequence_version": version_number,
            "input_digest": generated.input_digest,
            "producer": generated.producer,
            "producer_version": generated.producer_version,
            "sequence_producer_version": generated.sequence_producer_version,
            "personalization_policy_version_number": generated.policy_version_number,
            "personalization_strategy_id": generated.strategy_id,
            "cadence_source": generated.cadence.source,
            "regenerated_from": str(previous.id) if previous is not None else None,
            "warning_count": len(generated.warnings),
        },
    )
    return sequence


def _now(session: Session) -> Any:
    """The database's wall clock, so supersession order is recoverable.

    ``clock_timestamp()`` rather than ``now()``. ``now()`` is transaction-start
    time in PostgreSQL and is constant for the whole transaction, so two
    supersessions in one transaction — regenerating a sequence supersedes its
    predecessor and then every one of its message versions — would receive
    identical timestamps and could not be ordered afterwards.

    This matches ``personalization_policy_activations``, which uses
    ``clock_timestamp()`` for the same reason and documents it.
    """

    from sqlalchemy import func

    return session.scalar(select(func.clock_timestamp()))


def _ensure_logical_messages(
    session: Session, *, membership: CampaignContact, sequence_key: uuid.UUID
) -> dict[int, EmailSequenceMessage]:
    """The seven stable message rows, created once and reused forever after.

    Reused rather than recreated on regeneration, because these ids are the
    durable relationship between messages. Recreating them would silently break
    any future external reference that pointed at one.
    """

    from app.services.personalization.sequence import PURPOSE_BY_POSITION

    existing = {
        row.position: row
        for row in session.scalars(
            select(EmailSequenceMessage)
            .where(EmailSequenceMessage.sequence_key == sequence_key)
            .order_by(EmailSequenceMessage.position)
        ).all()
    }
    if len(existing) == len(PURPOSE_BY_POSITION):
        return existing

    from app.models.enums import SequenceMessageType

    previous_id: uuid.UUID | None = None
    rows: dict[int, EmailSequenceMessage] = {}
    for position in sorted(PURPOSE_BY_POSITION):
        row = existing.get(position)
        if row is None:
            row = EmailSequenceMessage(
                sequence_key=sequence_key,
                campaign_contact_id=membership.id,
                position=position,
                message_type=(
                    SequenceMessageType.INITIAL if position == 1 else SequenceMessageType.FOLLOW_UP
                ),
                purpose=PURPOSE_BY_POSITION[position],
                predecessor_message_id=previous_id,
            )
            session.add(row)
            # Flushed one at a time only here, on first creation, because each
            # row's id is the next row's predecessor and the chain constraint
            # is checked on insert.
            session.flush()
        rows[position] = row
        previous_id = row.id
    return rows


def _supersede_current_version(session: Session, *, message_id: uuid.UUID) -> None:
    current = session.scalars(
        select(EmailSequenceMessageVersion).where(
            EmailSequenceMessageVersion.message_id == message_id,
            EmailSequenceMessageVersion.superseded_at.is_(None),
        )
    ).first()
    if current is not None:
        current.superseded_at = _now(session)
        session.flush()
