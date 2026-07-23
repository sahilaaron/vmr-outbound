"""Draft-version and approval models (DAT-001 representation).

Draft versions are immutable: any edit creates a new version. An approval
references exactly one immutable draft version; editing a draft (a new version)
invalidates a prior approval. This slice only represents the tables — no draft is
generated and no approval workflow is implemented here.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import ApprovalStatus


class DraftVersion(Base):
    """An immutable version of an email draft for a contact (in a campaign)."""

    __tablename__ = "draft_versions"
    __table_args__ = (
        UniqueConstraint(
            "contact_id",
            "campaign_id",
            "version_number",
            name="uq_draft_versions_contact_campaign_version",
        ),
        Index("ix_draft_versions_contact_id", "contact_id"),
        Index("ix_draft_versions_campaign_id", "campaign_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=True,
    )
    # Sequential per (contact, campaign); a new edit is a new version.
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"DraftVersion(contact_id={self.contact_id!r}, campaign_id={self.campaign_id!r}, "
            f"version={self.version_number!r})"
        )


class DraftApproval(Base):
    """An approval that references exactly one immutable draft version."""

    __tablename__ = "draft_approvals"
    __table_args__ = (
        # One approval record per draft version.
        UniqueConstraint("draft_version_id", name="uq_draft_approvals_draft_version"),
        Index("ix_draft_approvals_draft_version_id", "draft_version_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # RESTRICT: an approved draft version cannot be deleted out from under its approval.
    draft_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("draft_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    approved_by: Mapped[str] = mapped_column(Text, nullable=False)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[ApprovalStatus] = mapped_column(
        Enum(ApprovalStatus, name="approval_status"),
        nullable=False,
        default=ApprovalStatus.APPROVED,
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"DraftApproval(draft_version_id={self.draft_version_id!r}, "
            f"status={self.status.value!r})"
        )
