"""The seven-message outreach sequence (SEQ-001).

One Campaign Contact has at most one *logical* sequence. That sequence is
generated as a whole, versioned as a whole, and reviewed one message at a time.
Four tables carry it, and the split between them is the whole design:

``email_sequences``
    One row per **generation**. Immutable. Carries the input digest, the
    producer, the exact Personalization policy and strategy, the context
    decision, and the Research / Insights / Company Intelligence lineage that
    the whole sequence rests on. Regenerating supersedes the row and writes a
    new one; it never edits this one.

``email_sequence_messages``
    One row per **logical message** -- seven of them, positions 1 to 7. This is
    the identity that survives regeneration. It carries position, type,
    purpose, predecessor and future delivery state, and deliberately carries no
    text at all. A later Gmail adapter needs to say "the follow-up after *this*
    message", and it must be able to say that without depending on a position
    number or on whichever text version happens to be current.

``email_sequence_message_versions``
    One row per **immutable content version** of one logical message. Editing
    message 3 writes a new row for message 3 and supersedes the old one. It
    does not touch messages 1, 2 and 4 to 7, and it does not create a new
    sequence version.

``email_sequence_message_reviews``
    One row per **decision against one exact immutable message version**. A
    decision can therefore never drift onto text a human did not read. A bulk
    approval writes one row per message and stamps them all with a shared
    ``bulk_operation_id``, so "the operator approved the sequence" remains a
    statement about seven exact versions rather than an ambiguous claim.

Why not seven ``DraftVersion`` rows. ``draft_versions`` is unique on
``(contact_id, campaign_id, version_number)`` where ``version_number`` counts
*rewrites*, and :func:`app.services.drafts._decide` refuses to decide on
anything but the highest version for a ``(contact, campaign)`` pair. Seven
sibling messages stored there would leave six permanently un-approvable, and
would overload one column with two unrelated meanings. Historical drafts stay
exactly where they are, readable and truthful, and this module never touches
them.

**Nothing here sends anything.** No column addresses a mailbox, and no state in
this module is advanced by any code path in this build. ``delivery_state`` and
``stop_state`` are domain vocabulary for a delivery workflow that does not
exist yet -- see ``docs/EMAIL_SEQUENCE.md`` for why they are modelled now and
what is deliberately absent.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import (
    SequenceDeliveryState,
    SequenceGenerationStatus,
    SequenceMessageOrigin,
    SequenceMessagePurpose,
    SequenceMessageType,
    SequenceReviewDecision,
    SequenceReviewState,
    SequenceStopReason,
    SequenceStopState,
    SequenceValidationStatus,
)

#: A sequence is exactly seven messages. Not a default, not a policy setting --
#: the shape of the product decision this table records.
SEQUENCE_LENGTH = 7


class EmailSequence(Base):
    """One immutable generated sequence version for one Campaign Contact."""

    __tablename__ = "email_sequences"
    __table_args__ = (
        UniqueConstraint(
            "sequence_key",
            "sequence_version",
            name="uq_email_sequences_sequence_key",
        ),
        # At most one live sequence per Campaign Contact. A superseded row keeps
        # its campaign_contact_id -- history stays queryable -- but only one row
        # at a time may answer "what is the sequence for this contact".
        Index(
            "uq_email_sequences_current_membership",
            "campaign_contact_id",
            unique=True,
            postgresql_where=text("superseded_at IS NULL"),
        ),
        Index("ix_email_sequences_campaign_id", "campaign_id"),
        Index("ix_email_sequences_contact_id", "contact_id"),
        Index("ix_email_sequences_digest", "campaign_contact_id", "input_digest"),
        CheckConstraint("sequence_version > 0", name="sequence_version_positive"),
        CheckConstraint(
            "message_count >= 0 AND message_count <= 7",
            name="message_count_within_sequence",
        ),
        CheckConstraint(
            "current_actionable_position IS NULL "
            "OR (current_actionable_position >= 1 AND current_actionable_position <= 7)",
            name="actionable_position_within_sequence",
        ),
        CheckConstraint(
            "(stop_state = 'RUNNING' AND stop_reason IS NULL) "
            "OR (stop_state = 'STOPPED' AND stop_reason IS NOT NULL)",
            name="stop_reason_paired_with_stop_state",
        ),
        CheckConstraint("btrim(input_digest) <> ''", name="input_digest_not_blank"),
    )

    #: This row's identity as a *version*.
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: The sequence's identity as a *thing*, stable across every version of it.
    #: Regeneration keeps this and increments ``sequence_version``.
    sequence_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    sequence_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    campaign_contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("campaign_contacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    #: Nullable because a Contact may legitimately have no permanent Company
    #: record; generation refuses in that case, but the column must not lie.
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="SET NULL"), nullable=True
    )
    #: The Personalization Agent Job that produced this sequence. Nullable so a
    #: Policy Studio preview or a backfill can produce a sequence without
    #: inventing a job that never ran. The physical table is ``verification_jobs``
    #: -- see :class:`app.models.verification_job.AgentJob`, which kept the
    #: original name when the queue became general-purpose.
    agent_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("verification_jobs.id", ondelete="SET NULL"), nullable=True
    )

    #: SHA-256 over every sequence-relevant input. Two generations with the same
    #: digest are the same generation, and the second one must not spend.
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    producer: Mapped[str | None] = mapped_column(String(128), nullable=True)
    producer_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: The version of the sequence *builder* -- prompt shape, purpose framework
    #: and validation policy. Bumping it changes the digest, because the same
    #: inputs through a different builder are not the same output.
    sequence_producer_version: Mapped[str] = mapped_column(String(64), nullable=False)
    validation_policy_version: Mapped[str] = mapped_column(String(64), nullable=False)

    personalization_policy_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("personalization_policy_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    personalization_policy_version_number: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    personalization_strategy_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: ``ContextDecision.summary()`` -- the same shape ``draft_versions`` stores,
    #: so one reader can explain either.
    personalization_decision: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    research_lineage: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    insights_lineage: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    intelligence_lineage: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    #: Where the planned timing came from: the Campaign's cadence override or
    #: the bounded default ladder.
    cadence_source: Mapped[str] = mapped_column(String(32), nullable=False, default="default")
    planned_span_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    generation_status: Mapped[SequenceGenerationStatus] = mapped_column(
        Enum(SequenceGenerationStatus, name="sequence_generation_status"),
        nullable=False,
        default=SequenceGenerationStatus.COMPLETE,
    )
    validation_status: Mapped[SequenceValidationStatus] = mapped_column(
        Enum(SequenceValidationStatus, name="sequence_validation_status"),
        nullable=False,
        default=SequenceValidationStatus.PASSED,
    )
    #: Cached aggregate. Always recomputed from the seven exact message states by
    #: :func:`app.services.sequences.review.aggregate_state` before it is read
    #: for a decision; stored so a list page can sort and filter without
    #: loading seven messages per row.
    review_state: Mapped[SequenceReviewState] = mapped_column(
        Enum(SequenceReviewState, name="sequence_review_state"),
        nullable=False,
        default=SequenceReviewState.NEEDS_REVIEW,
    )
    validation_findings: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    stop_state: Mapped[SequenceStopState] = mapped_column(
        Enum(SequenceStopState, name="sequence_stop_state"),
        nullable=False,
        default=SequenceStopState.RUNNING,
    )
    stop_reason: Mapped[SequenceStopReason | None] = mapped_column(
        Enum(SequenceStopReason, name="sequence_stop_reason"), nullable=True
    )
    #: Which position a future delivery workflow would act on next. Computed
    #: from review state and predecessor order; it authorises nothing.
    current_actionable_position: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("email_sequences.id", ondelete="SET NULL"), nullable=True
    )


class EmailSequenceMessage(Base):
    """One logical message position, stable across every regeneration and edit.

    Deliberately carries no subject and no body. Text belongs to a version;
    identity belongs here. That separation is what lets a future Gmail adapter
    record "the sent message for *this* message" without that record being
    invalidated the moment an operator fixes a typo.
    """

    __tablename__ = "email_sequence_messages"
    __table_args__ = (
        UniqueConstraint(
            "sequence_key", "position", name="uq_email_sequence_messages_sequence_key"
        ),
        UniqueConstraint(
            "sequence_key", "purpose", name="uq_email_sequence_messages_sequence_key_purpose"
        ),
        Index("ix_email_sequence_messages_campaign_contact_id", "campaign_contact_id"),
        CheckConstraint("position >= 1 AND position <= 7", name="position_within_sequence"),
        # Position 1 is the initial message and has no predecessor; every other
        # position is a follow-up and must name the message before it. Both
        # halves are enforced together because either alone permits a chain that
        # starts twice or not at all.
        CheckConstraint(
            "(position = 1 AND message_type = 'INITIAL' AND predecessor_message_id IS NULL) "
            "OR (position > 1 AND message_type = 'FOLLOW_UP' "
            "AND predecessor_message_id IS NOT NULL)",
            name="initial_is_first_and_unchained",
        ),
        CheckConstraint(
            "predecessor_message_id IS NULL OR predecessor_message_id <> id",
            name="predecessor_is_not_self",
        ),
    )

    #: The stable logical message id. Never reissued.
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sequence_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    campaign_contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("campaign_contacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    message_type: Mapped[SequenceMessageType] = mapped_column(
        Enum(SequenceMessageType, name="sequence_message_type"), nullable=False
    )
    purpose: Mapped[SequenceMessagePurpose] = mapped_column(
        Enum(SequenceMessagePurpose, name="sequence_message_purpose"), nullable=False
    )
    predecessor_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("email_sequence_messages.id", ondelete="RESTRICT"),
        nullable=True,
    )
    #: Future delivery eligibility. Every row in this build is ``NOT_READY`` and
    #: nothing advances it. Kept apart from review state on purpose: a message
    #: can be approved text and still be nowhere near deliverable.
    delivery_state: Mapped[SequenceDeliveryState] = mapped_column(
        Enum(SequenceDeliveryState, name="sequence_delivery_state"),
        nullable=False,
        default=SequenceDeliveryState.NOT_READY,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EmailSequenceMessageVersion(Base):
    """One immutable content version of one logical sequence message."""

    __tablename__ = "email_sequence_message_versions"
    __table_args__ = (
        UniqueConstraint(
            "message_id",
            "message_version",
            name="uq_email_sequence_message_versions_message_id",
        ),
        # Exactly one live version per logical message.
        Index(
            "uq_email_sequence_message_versions_current",
            "message_id",
            unique=True,
            postgresql_where=text("superseded_at IS NULL"),
        ),
        Index("ix_email_sequence_message_versions_message_id", "message_id"),
        Index("ix_email_sequence_message_versions_sequence_id", "sequence_id"),
        CheckConstraint("message_version > 0", name="message_version_positive"),
        CheckConstraint("position >= 1 AND position <= 7", name="position_within_sequence"),
        CheckConstraint(
            "recommended_delay_days >= 0 AND recommended_delay_days <= 365",
            name="delay_within_bounds",
        ),
        CheckConstraint(
            "recommended_elapsed_day >= 0 AND recommended_elapsed_day <= 3650",
            name="elapsed_day_within_bounds",
        ),
        CheckConstraint(
            "(position = 1 AND recommended_delay_days = 0 AND recommended_elapsed_day = 0) "
            "OR position > 1",
            name="initial_starts_at_day_zero",
        ),
        CheckConstraint("btrim(subject) <> ''", name="subject_not_blank"),
        CheckConstraint("btrim(body) <> ''", name="body_not_blank"),
        # A human edit must keep the text it replaced. Without this, "edited"
        # would be a claim with nothing behind it.
        CheckConstraint(
            "origin <> 'HUMAN_EDITED' "
            "OR (original_subject IS NOT NULL AND original_body IS NOT NULL "
            "AND source_version_id IS NOT NULL)",
            name="human_edit_keeps_its_source",
        ),
    )

    #: The immutable message-version id. This is what a review decision points at.
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("email_sequence_messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: The sequence *version* this content was generated under. An edit keeps the
    #: same sequence version: editing one message does not re-version the other
    #: six, and pretending otherwise would falsify six lineage records.
    sequence_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("email_sequences.id", ondelete="CASCADE"), nullable=False
    )
    message_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    subject: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    recommended_delay_days: Mapped[int] = mapped_column(Integer, nullable=False)
    recommended_elapsed_day: Mapped[int] = mapped_column(Integer, nullable=False)

    origin: Mapped[SequenceMessageOrigin] = mapped_column(
        Enum(SequenceMessageOrigin, name="sequence_message_origin"),
        nullable=False,
        default=SequenceMessageOrigin.GENERATED,
    )
    generation_status: Mapped[SequenceGenerationStatus] = mapped_column(
        Enum(SequenceGenerationStatus, name="sequence_generation_status"),
        nullable=False,
        default=SequenceGenerationStatus.COMPLETE,
    )
    validation_status: Mapped[SequenceValidationStatus] = mapped_column(
        Enum(SequenceValidationStatus, name="sequence_validation_status"),
        nullable=False,
        default=SequenceValidationStatus.PASSED,
    )

    personalization_policy_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("personalization_policy_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    personalization_strategy_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: The exact context decision this message was written under.
    context_decision: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    #: What this message actually used, as distinct from what was available.
    #: ``{"research": [...], "insights": [...], "intelligence": [...]}``.
    context_used: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    #: Supplied evidence ids this message cited. A subset of the allow-list; a
    #: citation outside it is refused before anything is persisted.
    evidence_insight_ids: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    intelligence_accepted_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    intelligence_excluded_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    warnings: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)

    #: For a human edit: the generated version it was edited from, and that
    #: version's text, so the original is never lost.
    source_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("email_sequence_message_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    original_subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_body: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EmailSequenceMessageReview(Base):
    """A human decision recorded against one exact immutable message version.

    One row per version, and the version id is the whole point: a decision that
    named only "the message" or "the sequence" could silently come to apply to
    text nobody read. ``bulk_operation_id`` is how one operator action that
    covered several messages stays reconstructable as one action *and* as the
    exact set of versions it covered.
    """

    __tablename__ = "email_sequence_message_reviews"
    __table_args__ = (
        UniqueConstraint(
            "message_version_id",
            name="uq_email_sequence_message_reviews_message_version_id",
        ),
        Index("ix_email_sequence_message_reviews_bulk", "bulk_operation_id"),
        CheckConstraint("btrim(decided_by) <> ''", name="decided_by_not_blank"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    message_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("email_sequence_message_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("email_sequence_messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    decision: Mapped[SequenceReviewDecision] = mapped_column(
        Enum(SequenceReviewDecision, name="sequence_review_decision"), nullable=False
    )
    decided_by: Mapped[str] = mapped_column(Text, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Non-null when one bulk operation produced this decision alongside others.
    bulk_operation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
