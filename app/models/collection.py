"""Reusable Contact Collections.

``Collection`` is the canonical backend term. The Chrome extension may call a
Collection a "Label", and the original schema used ``contact_labels`` table and
column names. Those physical names are deliberately retained for a safe,
additive migration; compatibility aliases in :mod:`app.models.contact_capture`
keep existing callers working while new services and APIs use Collection.

Collections are global reusable records. ``CampaignCollection`` associates a
global Collection with a Campaign; it does not transfer ownership or copy the
Collection. Contact membership and Campaign association are separate facts.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, synonym

from app.db.base import Base


class Collection(Base):
    """A persistent, reusable grouping of Contacts."""

    # Legacy physical name retained to avoid rewriting proven capture data.
    __tablename__ = "contact_labels"
    __table_args__ = (UniqueConstraint("slug", name="uq_contact_labels_slug"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(96), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
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
        return f"<Collection slug={self.slug!r}>"


class CollectionMembership(Base):
    """One Collection applied to a permanent Contact or pending capture.

    A pending capture has no Contact yet, so at least one of ``contact_id`` or
    ``capture_id`` must anchor the membership. On a Contact-anchored row,
    ``capture_id`` may additionally preserve acquisition provenance, so both
    may be present. Existing partial indexes make repeated writes idempotent
    under concurrency.
    """

    # Legacy physical name retained; ``label_id`` is exposed as a compatibility
    # synonym while new code uses ``collection_id``.
    __tablename__ = "contact_label_assignments"
    __table_args__ = (
        CheckConstraint(
            "contact_id IS NOT NULL OR capture_id IS NOT NULL",
            name="ck_contact_label_assignments_anchor",
        ),
        Index(
            "uq_contact_label_assignments_contact",
            "contact_id",
            "label_id",
            unique=True,
            postgresql_where=text("contact_id IS NOT NULL"),
        ),
        Index(
            "uq_contact_label_assignments_capture",
            "capture_id",
            "label_id",
            unique=True,
            postgresql_where=text("contact_id IS NULL"),
        ),
        Index("ix_contact_label_assignments_label_id", "label_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=True
    )
    collection_id: Mapped[uuid.UUID] = mapped_column(
        "label_id",
        UUID(as_uuid=True),
        ForeignKey("contact_labels.id", ondelete="CASCADE"),
        nullable=False,
    )
    label_id = synonym("collection_id")
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    capture_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("linkedin_profile_snapshots.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CampaignCollection(Base):
    """Association making a global Collection available to one Campaign."""

    __tablename__ = "campaign_collections"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id",
            "collection_id",
            name="uq_campaign_collections_campaign_collection",
        ),
        Index("ix_campaign_collections_campaign_id", "campaign_id"),
        Index("ix_campaign_collections_collection_id", "collection_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
    )
    collection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contact_labels.id", ondelete="CASCADE"),
        nullable=False,
    )
    association_role: Mapped[str] = mapped_column(
        String(32), nullable=False, default="audience", server_default="audience"
    )
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
