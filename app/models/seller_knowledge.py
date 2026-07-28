"""Seller-side knowledge records and their associations (KB-001).

Four record types an operator maintains — offerings, proof points, restricted
claims, and personas — plus the join tables that relate them to each other and
to campaigns.

Three schema decisions carry most of the meaning here.

**Proof points, claims and personas are global rows, related by reference.**
A proof point such as "we have covered this market since 2009" is one fact
about the company; it does not become a different fact because a second
offering also wants to use it. So it is stored once and linked, never copied
per offering. The repository does snapshot elsewhere — captured evidence is
frozen at capture time — but that convention exists to preserve what an
*external* source said at a moment in time. There is no such moment here: an
operator corrects a proof point precisely because the corrected version is the
one they want everywhere. Copying would have made a correction a hunt.

**Archiving, not deleting.** Every record carries
:class:`~app.models.enums.SellerRecordState`. Nothing in this module has a
delete path, so an offering a campaign already references cannot disappear
underneath it. Archiving withdraws a record from readiness counts and from the
pickers used to build new context; it changes nothing that already points at it.

**Associations are explicit models, not ``relationship(secondary=...)``.**
That matches ``CampaignContact`` and ``ContactLabelAssignment``: each link is a
row an operator created, with its own timestamp and author, and is worth being
able to query and audit on its own terms.
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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import SellerClaimScope, SellerOfferingType, SellerRecordState


class SellerOffering(Base):
    """A product, service, subscription, report, or engagement we may promote."""

    __tablename__ = "seller_offerings"
    __table_args__ = (
        # Names are the operator's handle on an offering, so two live offerings
        # may not share one. Archived rows are excluded: reusing the name of
        # something withdrawn is legitimate, and blocking it would have forced
        # operators to rename history.
        Index(
            "uq_seller_offerings_active_name",
            "name",
            unique=True,
            postgresql_where=text("state = 'ACTIVE'"),
        ),
        Index("ix_seller_offerings_state", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    offering_type: Mapped[SellerOfferingType] = mapped_column(
        Enum(SellerOfferingType, name="seller_offering_type"),
        nullable=False,
        default=SellerOfferingType.OTHER,
    )
    short_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # What this offering is for. Lists of short statements; ``None`` means not
    # filled in, ``[]`` means considered and deliberately empty.
    problems_addressed: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    use_cases: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    differentiators: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[SellerRecordState] = mapped_column(
        Enum(SellerRecordState, name="seller_record_state"),
        nullable=False,
        default=SellerRecordState.ACTIVE,
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"SellerOffering(id={self.id!r}, name={self.name!r}, state={self.state.value!r})"


class SellerProofPoint(Base):
    """An operator-entered factual statement that may support messaging.

    Not evidence in the ``insight_evidence`` sense, and the difference is not
    cosmetic. An insight is a claim about a prospect read off an external
    source, and it carries a URL, a retrieval time, and a confidence because
    nobody here vouches for it. A proof point is a first-party statement about
    us that an operator is asserting. ``source_reference`` is therefore an
    internal pointer — the report, the contract, the internal page the operator
    would cite if challenged — and it is optional, because the operator's entry
    is itself the authority (KB-001).
    """

    __tablename__ = "seller_proof_points"
    __table_args__ = (Index("ix_seller_proof_points_state", "state"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    supporting_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Where an operator would look this up internally. Free text on purpose: it
    # may be a URL, a document name, or a person.
    source_reference: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    state: Mapped[SellerRecordState] = mapped_column(
        Enum(SellerRecordState, name="seller_record_state"),
        nullable=False,
        default=SellerRecordState.ACTIVE,
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"SellerProofPoint(id={self.id!r}, state={self.state.value!r})"


class SellerRestrictedClaim(Base):
    """A statement, or class of statement, that generated copy must not make.

    Stored as its own record type rather than as guidance text on the profile
    because a prohibition has to be checkable. Future drafting logic needs to
    retrieve "everything that is forbidden for this campaign" as a list, and
    an operator needs to be able to withdraw one restriction without editing a
    paragraph that contains the rest.
    """

    __tablename__ = "seller_restricted_claims"
    __table_args__ = (
        Index("ix_seller_restricted_claims_state", "state"),
        Index("ix_seller_restricted_claims_scope", "scope"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    # Illustrations of the prohibited wording. A list of strings; optional,
    # because a rule can be clear without an example.
    examples: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    scope: Mapped[SellerClaimScope] = mapped_column(
        Enum(SellerClaimScope, name="seller_claim_scope"),
        nullable=False,
        default=SellerClaimScope.GLOBAL,
    )
    state: Mapped[SellerRecordState] = mapped_column(
        Enum(SellerRecordState, name="seller_record_state"),
        nullable=False,
        default=SellerRecordState.ACTIVE,
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"SellerRestrictedClaim(id={self.id!r}, title={self.title!r}, "
            f"scope={self.scope.value!r})"
        )


class SellerPersona(Base):
    """A reusable buyer persona defined by the seller.

    Not a :class:`~app.models.contact.Contact`. A contact is a real person who
    was captured or imported, carries provenance and suppression state, and can
    be written to. A persona is a description of a role we sell to; nobody is
    ever emailed because of one. Keeping them in separate tables keeps that
    from being a convention someone can forget.
    """

    __tablename__ = "seller_personas"
    __table_args__ = (
        Index(
            "uq_seller_personas_active_name",
            "name",
            unique=True,
            postgresql_where=text("state = 'ACTIVE'"),
        ),
        Index("ix_seller_personas_state", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    role_function: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Free text rather than an enum: seniority vocabulary differs by market and
    # nothing deterministic keys off it.
    seniority: Mapped[str | None] = mapped_column(String(120), nullable=True)
    responsibilities: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    challenges: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    use_cases: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    messaging_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[SellerRecordState] = mapped_column(
        Enum(SellerRecordState, name="seller_record_state"),
        nullable=False,
        default=SellerRecordState.ACTIVE,
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"SellerPersona(id={self.id!r}, name={self.name!r}, state={self.state.value!r})"


class SellerOfferingProofPoint(Base):
    """Links a proof point to an offering it may support."""

    __tablename__ = "seller_offering_proof_points"
    __table_args__ = (
        UniqueConstraint(
            "offering_id",
            "proof_point_id",
            name="uq_seller_offering_proof_points_pair",
        ),
        Index("ix_seller_offering_proof_points_offering_id", "offering_id"),
        Index("ix_seller_offering_proof_points_proof_point_id", "proof_point_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    offering_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "seller_offerings.id",
            ondelete="CASCADE",
            name="fk_seller_offering_proof_points_offering",
        ),
        nullable=False,
    )
    proof_point_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "seller_proof_points.id",
            ondelete="CASCADE",
            name="fk_seller_offering_proof_points_proof_point",
        ),
        nullable=False,
    )
    created_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"SellerOfferingProofPoint(offering_id={self.offering_id!r}, "
            f"proof_point_id={self.proof_point_id!r})"
        )


class SellerOfferingRestrictedClaim(Base):
    """Links an offering-scoped restricted claim to the offering it restricts."""

    __tablename__ = "seller_offering_restricted_claims"
    __table_args__ = (
        UniqueConstraint(
            "offering_id",
            "restricted_claim_id",
            name="uq_seller_offering_restricted_claims_pair",
        ),
        Index("ix_seller_offering_restricted_claims_offering_id", "offering_id"),
        Index(
            "ix_seller_offering_restricted_claims_restricted_claim_id",
            "restricted_claim_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    offering_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "seller_offerings.id",
            ondelete="CASCADE",
            name="fk_seller_offering_restricted_claims_offering",
        ),
        nullable=False,
    )
    restricted_claim_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "seller_restricted_claims.id",
            ondelete="CASCADE",
            name="fk_seller_offering_restricted_claims_claim",
        ),
        nullable=False,
    )
    created_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"SellerOfferingRestrictedClaim(offering_id={self.offering_id!r}, "
            f"restricted_claim_id={self.restricted_claim_id!r})"
        )


class SellerOfferingPersona(Base):
    """Links a persona to an offering that is relevant to it."""

    __tablename__ = "seller_offering_personas"
    __table_args__ = (
        UniqueConstraint(
            "offering_id",
            "persona_id",
            name="uq_seller_offering_personas_pair",
        ),
        Index("ix_seller_offering_personas_offering_id", "offering_id"),
        Index("ix_seller_offering_personas_persona_id", "persona_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    offering_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "seller_offerings.id",
            ondelete="CASCADE",
            name="fk_seller_offering_personas_offering",
        ),
        nullable=False,
    )
    persona_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "seller_personas.id",
            ondelete="CASCADE",
            name="fk_seller_offering_personas_persona",
        ),
        nullable=False,
    )
    created_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"SellerOfferingPersona(offering_id={self.offering_id!r}, "
            f"persona_id={self.persona_id!r})"
        )


class CampaignOffering(Base):
    """Records that a campaign concerns an offering.

    Association only. It says what a campaign is about, for organisation,
    tracking, reporting, and later context retrieval. It does not rank the
    offerings, does not nominate a primary one, does not write or change email
    copy, and does not choose a call to action — the campaign operator owns all
    of that directly (KB-001). Zero, one, or many associations are all valid
    states for a campaign, including permanently.

    There is no snapshot of the offering's name or wording here. The link is a
    reference, so a campaign always shows the offering as it currently reads,
    and archiving an offering leaves every campaign that references it intact.
    """

    __tablename__ = "campaign_offerings"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id",
            "offering_id",
            name="uq_campaign_offerings_campaign_offering",
        ),
        Index("ix_campaign_offerings_campaign_id", "campaign_id"),
        Index("ix_campaign_offerings_offering_id", "offering_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "campaigns.id",
            ondelete="CASCADE",
            name="fk_campaign_offerings_campaign",
        ),
        nullable=False,
    )
    offering_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "seller_offerings.id",
            ondelete="CASCADE",
            name="fk_campaign_offerings_offering",
        ),
        nullable=False,
    )
    created_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"CampaignOffering(campaign_id={self.campaign_id!r}, offering_id={self.offering_id!r})"
        )
