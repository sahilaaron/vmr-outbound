"""Reviewing and editing one sequence, one message at a time.

The rule this module exists to enforce is that **no decision is ever recorded
against "the sequence"**. Every approval, every discard and every edit names
one exact immutable message version. A bulk approval is not an exception to
that rule -- it is seven applications of it, stamped with one shared operation
id so the operator's single action stays reconstructable as a single action.

The aggregate is therefore always *derived*. There is no state a caller can set
that makes a sequence claim to be approved; a sequence is approved when all
seven of its current message versions carry an approval, and at no other time.
``EmailSequence.review_state`` is a cached copy of that derivation, refreshed
whenever a decision is written, and it is never the authority.

Approval is not sending authority. Nothing in this build can send, and
``approve`` says so in the audit trail rather than leaving it implied.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.email_sequence import (
    EmailSequence,
    EmailSequenceMessage,
    EmailSequenceMessageReview,
    EmailSequenceMessageVersion,
)
from app.models.enums import (
    SequenceGenerationStatus,
    SequenceMessageOrigin,
    SequenceReviewDecision,
    SequenceReviewState,
    SequenceStopState,
    SequenceValidationStatus,
)
from app.services.audit import record_audit_event

OPERATOR_ACTOR = "operator"
MAX_REASON_CHARS = 500
MAX_SUBJECT_CHARS = 300
MAX_BODY_CHARS = 20_000


class SequenceReviewError(RuntimeError):
    """A review or edit cannot be recorded as asked."""


@dataclass(frozen=True)
class MessageState:
    """One message's current version and the decision standing against it."""

    message_id: uuid.UUID
    position: int
    version_id: uuid.UUID
    message_version: int
    origin: SequenceMessageOrigin
    decision: SequenceReviewDecision | None
    decided_at: datetime | None
    decided_by: str | None

    @property
    def approved(self) -> bool:
        return self.decision is SequenceReviewDecision.APPROVED

    @property
    def discarded(self) -> bool:
        return self.decision is SequenceReviewDecision.DISCARDED

    @property
    def edited(self) -> bool:
        return self.origin is SequenceMessageOrigin.HUMAN_EDITED

    @property
    def awaiting(self) -> bool:
        return self.decision is None


@dataclass(frozen=True)
class SequenceAggregate:
    """The derived truth about a sequence's review state."""

    state: SequenceReviewState
    approved: int
    discarded: int
    edited: int
    awaiting: int
    total: int

    @property
    def decided(self) -> int:
        return self.approved + self.discarded

    @property
    def fully_approved(self) -> bool:
        return self.total > 0 and self.approved == self.total


def message_states(session: Session, *, sequence: EmailSequence) -> tuple[MessageState, ...]:
    """Every message's current version and standing decision, in one query.

    One statement for the whole sequence rather than one per message: "what is
    the state of this sequence" is a question about the set, and asking it seven
    times would issue seven queries for one page element.
    """

    rows = session.execute(
        select(EmailSequenceMessage, EmailSequenceMessageVersion, EmailSequenceMessageReview)
        .join(
            EmailSequenceMessageVersion,
            EmailSequenceMessageVersion.message_id == EmailSequenceMessage.id,
        )
        .outerjoin(
            EmailSequenceMessageReview,
            EmailSequenceMessageReview.message_version_id == EmailSequenceMessageVersion.id,
        )
        .where(
            EmailSequenceMessage.sequence_key == sequence.sequence_key,
            EmailSequenceMessageVersion.superseded_at.is_(None),
        )
        .order_by(EmailSequenceMessage.position)
    ).all()
    return tuple(
        MessageState(
            message_id=message.id,
            position=message.position,
            version_id=version.id,
            message_version=version.message_version,
            origin=version.origin,
            decision=review.decision if review is not None else None,
            decided_at=review.decided_at if review is not None else None,
            decided_by=review.decided_by if review is not None else None,
        )
        for message, version, review in rows
    )


def aggregate_state(sequence: EmailSequence, states: tuple[MessageState, ...]) -> SequenceAggregate:
    """Derive the sequence's review state from the seven exact message states.

    Order matters. A failed generation is failed whatever the messages say; a
    superseded sequence is superseded whatever its messages say; a stopped
    sequence is blocked. Only once none of those hold does the message tally
    decide anything, and the tally never invents an approval.
    """

    approved = sum(1 for state in states if state.approved)
    discarded = sum(1 for state in states if state.discarded)
    edited = sum(1 for state in states if state.edited)
    awaiting = sum(1 for state in states if state.awaiting)
    total = len(states)

    if sequence.superseded_at is not None:
        state = SequenceReviewState.SUPERSEDED
    elif sequence.generation_status is not SequenceGenerationStatus.COMPLETE:
        state = SequenceReviewState.FAILED
    elif sequence.validation_status is SequenceValidationStatus.FAILED:
        state = SequenceReviewState.FAILED
    elif sequence.stop_state is SequenceStopState.STOPPED:
        state = SequenceReviewState.BLOCKED
    elif total == 0:
        state = SequenceReviewState.GENERATED
    elif discarded:
        # A discarded message means the sequence is not ready, and saying
        # "partially approved" would let the approved count stand in for
        # readiness it does not have.
        state = SequenceReviewState.CONTAINS_DISCARDED
    elif approved == total:
        state = SequenceReviewState.APPROVED
    elif approved and awaiting:
        state = SequenceReviewState.PARTIALLY_APPROVED
    elif awaiting == total:
        state = SequenceReviewState.CONTAINS_EDITS if edited else SequenceReviewState.NEEDS_REVIEW
    else:
        state = SequenceReviewState.PARTIALLY_REVIEWED
    return SequenceAggregate(
        state=state,
        approved=approved,
        discarded=discarded,
        edited=edited,
        awaiting=awaiting,
        total=total,
    )


def _actionable_position(states: tuple[MessageState, ...]) -> int | None:
    """The first position a future delivery workflow could act on.

    Approved-and-not-yet-acted-on, in order. It authorises nothing: no code in
    this build reads it to do anything, and the delivery state that would gate a
    real action stays ``not_ready`` on every message.
    """

    for state in sorted(states, key=lambda item: item.position):
        if state.approved:
            return state.position
        if state.discarded:
            continue
        return None
    return None


def refresh_aggregate(session: Session, *, sequence: EmailSequence) -> SequenceAggregate:
    """Recompute and cache the aggregate. Returns the derived truth."""

    states = message_states(session, sequence=sequence)
    aggregate = aggregate_state(sequence, states)
    sequence.review_state = aggregate.state
    sequence.current_actionable_position = _actionable_position(states)
    session.flush()
    return aggregate


def _load_version(
    session: Session, *, message_version_id: uuid.UUID
) -> tuple[EmailSequenceMessageVersion, EmailSequenceMessage, EmailSequence]:
    version = session.get(EmailSequenceMessageVersion, message_version_id)
    if version is None:
        raise SequenceReviewError("That sequence message version no longer exists.")
    if version.superseded_at is not None:
        raise SequenceReviewError(
            f"Message {version.position} version {version.message_version} has been "
            "superseded by a newer version. Decide on the current text instead — "
            "approving text that has already been rewritten would approve something "
            "nobody is going to send."
        )
    message = session.get(EmailSequenceMessage, version.message_id)
    sequence = session.get(EmailSequence, version.sequence_id)
    if message is None or sequence is None:
        raise SequenceReviewError("That sequence message is no longer attached to a sequence.")
    if sequence.superseded_at is not None:
        raise SequenceReviewError(
            f"Sequence version {sequence.sequence_version} has been superseded. "
            "Decide on the current sequence instead."
        )
    return version, message, sequence


def _record(
    session: Session,
    *,
    version: EmailSequenceMessageVersion,
    message: EmailSequenceMessage,
    decision: SequenceReviewDecision,
    actor: str,
    reason: str | None,
    bulk_operation_id: uuid.UUID | None,
) -> EmailSequenceMessageReview:
    existing = session.scalars(
        select(EmailSequenceMessageReview).where(
            EmailSequenceMessageReview.message_version_id == version.id
        )
    ).first()
    trimmed = (reason or "").strip()[:MAX_REASON_CHARS] or None
    if existing is None:
        existing = EmailSequenceMessageReview(
            message_version_id=version.id,
            message_id=message.id,
            decision=decision,
            decided_by=actor,
            reason=trimmed,
            bulk_operation_id=bulk_operation_id,
        )
        session.add(existing)
    else:
        existing.decision = decision
        existing.decided_by = actor
        existing.reason = trimmed
        existing.bulk_operation_id = bulk_operation_id
    session.flush()
    return existing


def decide_message(
    session: Session,
    *,
    message_version_id: uuid.UUID,
    decision: SequenceReviewDecision,
    actor: str = OPERATOR_ACTOR,
    reason: str | None = None,
    bulk_operation_id: uuid.UUID | None = None,
) -> SequenceAggregate:
    """Record one decision against one exact immutable message version."""

    if decision is SequenceReviewDecision.INVALIDATED:
        raise SequenceReviewError("Invalidation is a consequence of editing, not a decision.")
    version, message, sequence = _load_version(session, message_version_id=message_version_id)
    _record(
        session,
        version=version,
        message=message,
        decision=decision,
        actor=actor,
        reason=reason,
        bulk_operation_id=bulk_operation_id,
    )
    record_audit_event(
        session,
        actor=actor,
        action=f"email_sequence_message.{decision.value}",
        entity_type="email_sequence_message_version",
        entity_id=str(version.id),
        new_state=decision.value,
        reason=(
            "recorded against this exact message version; nothing was sent, because "
            "no sending path exists"
        ),
        context={
            "sequence_id": str(sequence.id),
            "sequence_key": str(sequence.sequence_key),
            "sequence_version": sequence.sequence_version,
            "message_id": str(message.id),
            "position": message.position,
            "message_version": version.message_version,
            "bulk_operation_id": str(bulk_operation_id) if bulk_operation_id else None,
            "note": (reason or "").strip()[:MAX_REASON_CHARS] or None,
        },
    )
    return refresh_aggregate(session, sequence=sequence)


def approve_message(
    session: Session,
    *,
    message_version_id: uuid.UUID,
    actor: str = OPERATOR_ACTOR,
    reason: str | None = None,
) -> SequenceAggregate:
    return decide_message(
        session,
        message_version_id=message_version_id,
        decision=SequenceReviewDecision.APPROVED,
        actor=actor,
        reason=reason,
    )


def discard_message(
    session: Session,
    *,
    message_version_id: uuid.UUID,
    actor: str = OPERATOR_ACTOR,
    reason: str | None = None,
) -> SequenceAggregate:
    return decide_message(
        session,
        message_version_id=message_version_id,
        decision=SequenceReviewDecision.DISCARDED,
        actor=actor,
        reason=reason,
    )


def approve_sequence(
    session: Session,
    *,
    sequence_id: uuid.UUID,
    expected_version_ids: tuple[uuid.UUID, ...],
    actor: str = OPERATOR_ACTOR,
    reason: str | None = None,
) -> SequenceAggregate:
    """Approve every message in one operation, naming every exact version.

    ``expected_version_ids`` is what the operator's page was showing. If the
    stored current versions differ -- because something was edited or
    regenerated in another tab -- the whole operation is refused rather than
    partially applied. A bulk approval that silently approved a version the
    operator never saw would be exactly the ambiguity this model exists to
    prevent.
    """

    sequence = session.get(EmailSequence, sequence_id)
    if sequence is None:
        raise SequenceReviewError("That sequence no longer exists.")
    if sequence.superseded_at is not None:
        raise SequenceReviewError(
            f"Sequence version {sequence.sequence_version} has been superseded. "
            "Review the current sequence instead."
        )
    states = message_states(session, sequence=sequence)
    current_ids = tuple(state.version_id for state in states)
    if set(current_ids) != set(expected_version_ids):
        raise SequenceReviewError(
            "This sequence changed while you were reading it, so nothing was approved. "
            "Reload the sequence and decide on the messages as they now stand."
        )

    operation = uuid.uuid4()
    for state in states:
        version, message, _sequence = _load_version(session, message_version_id=state.version_id)
        _record(
            session,
            version=version,
            message=message,
            decision=SequenceReviewDecision.APPROVED,
            actor=actor,
            reason=reason,
            bulk_operation_id=operation,
        )
    record_audit_event(
        session,
        actor=actor,
        action="email_sequence.approved",
        entity_type="email_sequence",
        entity_id=str(sequence.id),
        new_state="approved",
        reason=(
            "one operation recording approval for every exact message version listed; "
            "nothing was sent, because no sending path exists"
        ),
        context={
            "bulk_operation_id": str(operation),
            "sequence_key": str(sequence.sequence_key),
            "sequence_version": sequence.sequence_version,
            "message_version_ids": [str(item) for item in current_ids],
            "note": (reason or "").strip()[:MAX_REASON_CHARS] or None,
        },
    )
    return refresh_aggregate(session, sequence=sequence)


def edit_message(
    session: Session,
    *,
    message_version_id: uuid.UUID,
    subject: str,
    body: str,
    actor: str = OPERATOR_ACTOR,
    reason: str | None = None,
) -> EmailSequenceMessageVersion:
    """Write a new immutable version of one message, leaving the other six alone.

    Three things happen and nothing else. The edited message gets a new version
    that keeps the text it replaced. The superseded version's approval, if it
    had one, is marked invalidated -- not deleted, because the approval did
    happen; it simply no longer applies to text nobody approved. And the
    sequence aggregate is recomputed.

    What deliberately does *not* happen: the other six messages are untouched,
    their approvals stand, the sequence is not re-versioned, and no
    regeneration is triggered.
    """

    version, message, sequence = _load_version(session, message_version_id=message_version_id)
    clean_subject = subject.strip()[:MAX_SUBJECT_CHARS]
    clean_body = body.strip()[:MAX_BODY_CHARS]
    if not clean_subject:
        raise SequenceReviewError("An edited message still needs a subject line.")
    if not clean_body:
        raise SequenceReviewError("An edited message still needs a body.")
    if clean_subject == version.subject and clean_body == version.body:
        raise SequenceReviewError("Nothing changed, so no new version was written.")

    from app.services.sequences.persistence import _next_message_version, _now

    existing_review = session.scalars(
        select(EmailSequenceMessageReview).where(
            EmailSequenceMessageReview.message_version_id == version.id
        )
    ).first()
    previous_decision = existing_review.decision if existing_review is not None else None
    if existing_review is not None and existing_review.decision is SequenceReviewDecision.APPROVED:
        existing_review.decision = SequenceReviewDecision.INVALIDATED

    version.superseded_at = _now(session)
    session.flush()

    edited = EmailSequenceMessageVersion(
        message_id=message.id,
        sequence_id=sequence.id,
        message_version=_next_message_version(session, message_id=message.id),
        position=version.position,
        subject=clean_subject,
        body=clean_body,
        recommended_delay_days=version.recommended_delay_days,
        recommended_elapsed_day=version.recommended_elapsed_day,
        origin=SequenceMessageOrigin.HUMAN_EDITED,
        generation_status=version.generation_status,
        validation_status=version.validation_status,
        personalization_policy_version_id=version.personalization_policy_version_id,
        personalization_strategy_id=version.personalization_strategy_id,
        context_decision=version.context_decision,
        context_used=version.context_used,
        evidence_insight_ids=version.evidence_insight_ids,
        intelligence_accepted_count=version.intelligence_accepted_count,
        intelligence_excluded_count=version.intelligence_excluded_count,
        warnings=version.warnings,
        source_version_id=version.id,
        original_subject=version.original_subject or version.subject,
        original_body=version.original_body or version.body,
        created_by=actor,
    )
    session.add(edited)
    session.flush()

    record_audit_event(
        session,
        actor=actor,
        action="email_sequence_message.edited",
        entity_type="email_sequence_message_version",
        entity_id=str(edited.id),
        previous_state=f"v{version.message_version}",
        new_state=f"v{edited.message_version}",
        reason=(
            "a human edit is a new immutable version; the previous version and any "
            "approval against it are kept and marked, not rewritten"
        ),
        context={
            "sequence_id": str(sequence.id),
            "sequence_key": str(sequence.sequence_key),
            "message_id": str(message.id),
            "position": message.position,
            "superseded_version_id": str(version.id),
            "previous_decision": previous_decision.value if previous_decision else None,
            "note": (reason or "").strip()[:MAX_REASON_CHARS] or None,
        },
    )
    refresh_aggregate(session, sequence=sequence)
    return edited
