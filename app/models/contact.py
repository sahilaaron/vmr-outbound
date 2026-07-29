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

from sqlalchemy import DateTime, ForeignKey, Index, String, func
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
        # The permanent company edge (APP-003). Partial: the value is in listing
        # one company's people, and NULL means "not linked yet" rather than a
        # row worth indexing.
        Index(
            "ix_contacts_company_id",
            "company_id",
            postgresql_where="company_id IS NOT NULL",
        ),
        # A tombstoned duplicate points at its survivor. Indexed so a survivor's
        # merged-away duplicates are cheap to list on the contact record.
        Index("ix_contacts_merged_into_id", "merged_into_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # --- Normalized identity --------------------------------------------------
    # A Contact is the permanent person record, including while some observed
    # identity fields are unresolved. Missing values stay NULL; capture must not
    # invent a surname, company, or domain merely to satisfy storage.
    first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    company_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    company_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # --- The permanent company edge (APP-003) --------------------------------
    #
    # Until APP-003 the only contact-to-company link was ``company_domain``
    # compared against ``companies.domain`` at read time. That worked but tied
    # the edge to a mutable string: correcting a company's domain silently
    # re-parented everyone under it, with nothing recording that it had happened.
    #
    # ``company_id`` is the real edge. ``company_domain`` stays as identity and
    # dedup input when observed/resolved, but is NULL until that happens. The two
    # are allowed to disagree: that disagreement is a reviewable conflict, not a
    # bug to paper over (see app.services.companies.conflicts).
    #
    # Deliberately nullable, and deliberately left NULL rather than guessed when
    # no company matches, when several do, or when the domain is missing or
    # malformed. Making it NOT NULL is a later decision that needs the backfill
    # to have actually converged first.
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="SET NULL"),
        nullable=True,
    )

    # --- Normalized identity (optional) --------------------------------------
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    linkedin_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    country: Mapped[str | None] = mapped_column(String(128), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(255), nullable=True)
    company_size: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # --- Deterministic dedup fingerprint -------------------------------------
    # casefold(first_name)|casefold(last_name)|company_domain — computed at import.
    natural_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    # --- Merge tombstone (DAT-004 identity resolution) -----------------------
    # When two contacts are confirmed duplicates, the loser is NOT deleted (its
    # import history and provenance are preserved); it is tombstoned by pointing
    # at the surviving contact. A merged contact is excluded from dedup lookups
    # and the active contact list, but remains fully auditable.
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="SET NULL"),
        nullable=True,
    )

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
    def is_merged(self) -> bool:
        """True when this contact has been merged into a surviving duplicate."""

        return self.merged_into_id is not None

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"Contact(id={self.id!r}, name={self.first_name!r} {self.last_name!r}, "
            f"email={self.email!r}, domain={self.company_domain!r})"
        )
