"""Contact-capture promotion records (DAT-014).

DAT-013 saves a captured person as permanent, immutable evidence but stops short
of a canonical :class:`~app.models.contact.Contact`, because a contact requires a
company **domain** and a LinkedIn page never shows one. DAT-014 is the bridge:
the operator resolves that domain through the existing DAT-010 logo.dev
candidate flow, and the capture is promoted.

:class:`ContactCapturePromotion` is the durable record of that bridge — one row
per capture. It holds the two outcomes **separately**, because "which company is
this?" and "which person is this?" fail independently and collapsing them would
hide which one blocked a promotion:

* ``company_outcome`` — how the company was (or was not) resolved;
* ``contact_outcome`` — what happened to the person.

It also holds the durable links the operator needs afterwards: the enrichment
record carrying the provider candidates and the confirmation decision, the
resolved company, and the promoted contact.

The capture itself is never rewritten. Its payload, profile fields, content hash
and experience observations stay exactly as submitted; this row is the mutable
part, kept beside the evidence rather than inside it.

Nothing here makes a contact outreach-eligible: promotion creates identity, not
permission. No campaign membership, email candidate, verification, score, or
approval is produced by this model or the service that writes it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import CompanyResolutionOutcome, ContactPromotionOutcome


class ContactCapturePromotion(Base):
    """The company-resolution and promotion state of one contact capture."""

    __tablename__ = "contact_capture_promotions"
    __table_args__ = (
        # One promotion record per capture. The database — not just application
        # code — is what makes a retry idempotent and a double promotion
        # impossible.
        UniqueConstraint("capture_id", name="uq_contact_capture_promotions_capture_id"),
        Index("ix_contact_capture_promotions_contact_id", "promoted_contact_id"),
        Index("ix_contact_capture_promotions_company_id", "resolved_company_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    capture_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("linkedin_profile_snapshots.id", ondelete="CASCADE"),
        nullable=False,
    )
    # The DAT-010 enrichment record holding this capture's provider candidates
    # and the operator's confirmation. Null only before a lookup is prepared.
    enrichment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("salesnav_company_enrichments.id", ondelete="SET NULL"),
        nullable=True,
    )

    # --- The two outcomes, kept separate --------------------------------------
    company_outcome: Mapped[CompanyResolutionOutcome] = mapped_column(
        Enum(CompanyResolutionOutcome, name="company_resolution_outcome"),
        nullable=False,
        default=CompanyResolutionOutcome.PENDING_LOOKUP,
    )
    contact_outcome: Mapped[ContactPromotionOutcome] = mapped_column(
        Enum(ContactPromotionOutcome, name="contact_promotion_outcome"),
        nullable=False,
        default=ContactPromotionOutcome.PENDING,
    )

    # --- Resolved records -----------------------------------------------------
    # The canonical company this capture's employer resolved to. Contacts carry
    # company_name/company_domain strings rather than a company foreign key, so
    # this is where the resolved company relationship is retained (see
    # docs/CAPTURE_PROMOTION.md on the APP-001 dependency).
    resolved_company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="SET NULL"), nullable=True
    )
    resolved_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    promoted_contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True
    )

    # --- What carried over ----------------------------------------------------
    # Label names actually applied to the promoted contact, and how many capture
    # notes were linked to it. Both are reported truthfully: a suppressed or
    # blocked promotion carries nothing over.
    labels_applied: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    notes_linked: Mapped[int] = mapped_column(nullable=False, default=0)

    # --- Why a promotion did not happen ---------------------------------------
    # Operator-facing explanation for a blocked promotion, plus a structured
    # detail blob (candidate counts, ambiguous contact ids, suppression reason).
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    promoted_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    @property
    def is_promoted(self) -> bool:
        """True once a canonical contact exists for this capture."""

        return self.promoted_contact_id is not None

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"ContactCapturePromotion(capture_id={self.capture_id!r}, "
            f"company={self.company_outcome.value!r}, contact={self.contact_outcome.value!r})"
        )
