"""Campaign-scoped offering research read from a URL, versioned.

One row is **both** the durable run and the artifact it produced. That is
deliberate and is the narrowest shape that satisfies what this feature has to
remember, so it is worth saying why rather than mirroring the two-table split
Company Intelligence uses.

Company Intelligence separates job from version because one company accumulates
many versions from many *different* inputs, and a job may legitimately produce
nothing new. Here the input is one URL an operator typed, every run is a version
of that answer, and a run that produced nothing is exactly the thing the customer
has to be told about — "could not prepare this offering". Splitting those into
two tables would mean a failed run lived in one table and a successful one in
two, and the question the product actually asks — what is this Campaign leading
with, and how did it get there — would need a join to answer.

Four properties are the whole contract:

* **It never touches the Library.** No row here writes, updates or archives a
  :class:`~app.models.seller_knowledge.SellerOffering`. The Library remains the
  seller's permanent knowledge; this is one Campaign's override of what to lead
  with. ``supporting_offering_id`` is a *reference* to the Library offering that
  was supporting at the time, recorded so an audit can reconstruct the pitch.
* **Only ``READY`` may be current.** ``is_current`` is enforced against the
  status by a check constraint, and a partial unique index allows at most one
  current row per Campaign. A failed re-analysis therefore cannot displace the
  last good context by accident — it would have to violate the schema.
* **A version is never rewritten.** Re-analyze inserts the next
  ``version_number``; the previous row keeps its answer, its URL and its
  timestamps, and is marked superseded when (and only when) a newer one becomes
  current.
* **No secret ever lands here.** The stored payload is the validated structured
  answer, plus provenance. The prompt is not stored, the raw model output is not
  stored, and no credential, token or connection string is part of any field.

``offering_context`` holds the validated structure defined by
``app.services.campaign_offering.contracts``; ``context_policy_version`` records
which version of that contract validated it, so a later contract change can tell
an old row apart from a new one instead of guessing from its keys.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
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
from app.models.enums import CampaignOfferingResearchStatus

#: Statuses a run is still expected to move out of on its own. A Campaign may
#: have at most one of these at a time: two concurrent reads of the same page
#: would spend two model calls to answer one question.
ACTIVE_RESEARCH_STATUSES: tuple[CampaignOfferingResearchStatus, ...] = (
    CampaignOfferingResearchStatus.QUEUED,
    CampaignOfferingResearchStatus.READING,
    CampaignOfferingResearchStatus.ANALYZING,
    CampaignOfferingResearchStatus.CONNECTING,
)

#: Statuses a worker may claim.
CLAIMABLE_RESEARCH_STATUSES: tuple[CampaignOfferingResearchStatus, ...] = (
    CampaignOfferingResearchStatus.QUEUED,
)

TERMINAL_RESEARCH_STATUSES: tuple[CampaignOfferingResearchStatus, ...] = (
    CampaignOfferingResearchStatus.READY,
    CampaignOfferingResearchStatus.FAILED,
    CampaignOfferingResearchStatus.CANCELLED,
)

_ACTIVE_SQL = (
    "status IN ('QUEUED'::campaign_offering_research_status,"
    "'READING'::campaign_offering_research_status,"
    "'ANALYZING'::campaign_offering_research_status,"
    "'CONNECTING'::campaign_offering_research_status)"
)


class CampaignOfferingResearch(Base):
    """One versioned attempt to understand a Campaign's offering from a URL."""

    __tablename__ = "campaign_offering_research"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id",
            "version_number",
            name="uq_campaign_offering_research_campaign_version",
        ),
        UniqueConstraint("idempotency_key", name="uq_campaign_offering_research_idempotency_key"),
        # At most one run in flight per Campaign.
        Index(
            "uq_campaign_offering_research_active_campaign",
            "campaign_id",
            unique=True,
            postgresql_where=text(_ACTIVE_SQL),
        ),
        # At most one current version per Campaign.
        Index(
            "uq_campaign_offering_research_current_campaign",
            "campaign_id",
            unique=True,
            postgresql_where=text("is_current"),
        ),
        Index("ix_campaign_offering_research_claimable", "status", "next_run_at"),
        Index("ix_campaign_offering_research_campaign_id", "campaign_id"),
        CheckConstraint("version_number >= 1", name="version_number_positive"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint("max_attempts >= 1 AND max_attempts <= 10", name="max_attempts_range"),
        # The two invariants that make "a failed re-analysis keeps the last good
        # context" a schema fact rather than a service convention.
        CheckConstraint("NOT is_current OR status = 'READY'", name="only_ready_is_current"),
        CheckConstraint(
            "status <> 'READY' OR offering_context IS NOT NULL",
            name="ready_has_context",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "campaigns.id",
            ondelete="CASCADE",
            name="fk_campaign_offering_research_campaign",
        ),
        nullable=False,
    )
    #: 1-based, per Campaign. Re-analyze and Change URL both take the next one.
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The URL exactly as it was validated and handed to the model — normalized
    #: (scheme, host case, fragment removed) but never rewritten to a different
    #: address. What the model reports it actually read is recorded separately in
    #: ``offering_context`` so a redirect is visible rather than assumed away.
    source_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    source_host: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[CampaignOfferingResearchStatus] = mapped_column(
        Enum(CampaignOfferingResearchStatus, name="campaign_offering_research_status"),
        nullable=False,
        default=CampaignOfferingResearchStatus.QUEUED,
        server_default=CampaignOfferingResearchStatus.QUEUED.name,
    )
    is_current: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: The validated structured answer. NULL until the run reaches READY.
    offering_context: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    #: A digest of the validated structure, so two runs over the same page can be
    #: compared without storing the model's prose twice.
    context_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Which version of the structured contract accepted this payload.
    context_policy_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    #: Provenance. Provider-neutral names: nothing branches on these.
    producer: Mapped[str | None] = mapped_column(String(64), nullable=True)
    producer_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    producer_model: Mapped[str | None] = mapped_column(String(120), nullable=True)

    #: The Library offering that was this Campaign's supporting context when the
    #: run succeeded. A reference, never a copy, and SET NULL so retiring an
    #: offering does not delete the record of what was pitched.
    supporting_offering_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "seller_offerings.id",
            ondelete="SET NULL",
            name="fk_campaign_offering_research_supporting_offering",
        ),
        nullable=True,
    )

    failure_code: Mapped[str | None] = mapped_column(String(96), nullable=True)
    #: Operator-facing, already sanitized by the service that wrote it. Never a
    #: stack trace and never raw model output.
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Queue state. The run is its own job, so the lease lives here.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default="2"
    )
    next_run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(400), nullable=False)

    requested_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    @property
    def is_active(self) -> bool:
        """Whether this run is still expected to move on its own."""

        return self.status in ACTIVE_RESEARCH_STATUSES

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"CampaignOfferingResearch(campaign_id={self.campaign_id!r}, "
            f"version={self.version_number!r}, status={self.status.value!r})"
        )
