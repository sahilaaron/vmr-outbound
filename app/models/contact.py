"""Contact model — the canonical, normalized person record.

The contact stores the **normalized** view of a person. The original, untouched
values are always retrievable from the immutable raw import row that produced or
last observed the contact (``import_rows.raw_data``) and from the per-observation
:class:`~app.models.provenance.ProvenanceRecord`, so normalized and original
values live side by side (contact-input contract; DAT-003 / DAT-005).

Deduplication keys are stored explicitly so matching is deterministic and
explainable (DAT-004): ``email`` is the normalized address (unique when present)
and ``natural_key`` is the exact ``first|last|domain`` fingerprint used only when
no email is available.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Contact(Base):
    """A normalized contact (person). Originals are preserved on the raw row."""

    __tablename__ = "contacts"
    __table_args__ = (
        # Same normalized email == same person (strong dedup key). Enforced at the
        # database as a partial unique index so two contacts can both be
        # email-less without colliding.
        Index(
            "uq_contacts_email",
            "email",
            unique=True,
            postgresql_where="email IS NOT NULL",
        ),
        # Natural-key lookups for email-less dedup. NOT unique: two different
        # people may share a natural key when they have distinct emails.
        Index("ix_contacts_natural_key", "natural_key"),
        Index("ix_contacts_company_domain", "company_domain"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # --- Normalized identity (required) --------------------------------------
    first_name: Mapped[str] = mapped_column(String(255), nullable=False)
    last_name: Mapped[str] = mapped_column(String(255), nullable=False)
    company_name: Mapped[str] = mapped_column(String(512), nullable=False)
    company_domain: Mapped[str] = mapped_column(String(255), nullable=False)

    # --- Normalized identity (optional) --------------------------------------
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    linkedin_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    country: Mapped[str | None] = mapped_column(String(128), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(255), nullable=True)
    company_size: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # --- Deterministic dedup fingerprint -------------------------------------
    # casefold(first_name)|casefold(last_name)|company_domain — computed at import.
    natural_key: Mapped[str] = mapped_column(String(1024), nullable=False)

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
            f"Contact(id={self.id!r}, name={self.first_name!r} {self.last_name!r}, "
            f"email={self.email!r}, domain={self.company_domain!r})"
        )
