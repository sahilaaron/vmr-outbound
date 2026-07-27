"""Field-level provenance for canonical company fields (APP-003).

:class:`~app.models.contact_field_value.ContactFieldValue` answers *"why does
this contact's title show what it shows?"*. This is the same question for a
company, and it exists as its own table rather than a generalization of that one
for a reason worth stating: the contact ledger is accepted, tested APP-002
behaviour with a partial unique index the database enforces, and widening its key
from ``(contact_id, field_name)`` to ``(entity_type, entity_id, field_name)``
would rewrite a working subsystem to save a file. The two ledgers are read by
different services and will diverge — a company field can be claimed by a
research dossier, and a contact field cannot.

What differs from the contact ledger, and why:

* **Origin is not an import.** A company field is observed by an operator, by a
  captured LinkedIn company page, by capture promotion, or by a research
  dossier. ``source_kind`` names which, and ``dossier_version_id`` points at the
  dossier when one is responsible — so a claim can always be traced back to the
  submission that made it.
* **A dossier claim is evidence, not an overwrite.** Recording an observation
  here does not change ``companies.industry``; reconciliation does, and only
  when the versioned policy says this observation now wins. That is the whole
  point of the separation: research proposes, the policy disposes, and both are
  visible afterwards.
* **NULL value means observed-as-empty.** A source that looked and found nothing
  is a real observation, and it is not the same as never having looked. Never
  having looked is the absence of a row.

Append-only. A new observation never edits or deletes an older one, so an older
source can never silently erase newer evidence and superseded values stay
auditable.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import CompanyFieldSource


class CompanyFieldValue(Base):
    """One observation of one canonical company field (append-only)."""

    __tablename__ = "company_field_values"
    __table_args__ = (
        Index("ix_company_field_values_company_field", "company_id", "field_name"),
        # Exactly one current winner per (company, field), enforced by the
        # database so the current value is never ambiguous however many
        # observations accumulate.
        Index(
            "uq_company_field_values_winner",
            "company_id",
            "field_name",
            unique=True,
            postgresql_where="is_current_winner",
        ),
        # "What did this dossier claim?" is a question the workspace asks
        # directly, so the dossier link is indexed rather than scanned.
        Index(
            "ix_company_field_values_dossier",
            "dossier_version_id",
            postgresql_where="dossier_version_id IS NOT NULL",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    # One of app.services.companies.provenance.TRACKED_COMPANY_FIELDS.
    field_name: Mapped[str] = mapped_column(String(64), nullable=False)
    # The observed value. NULL means the source observed it as empty.
    value: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Origin ---------------------------------------------------------------
    #
    # Provider-neutral by construction: the ledger records that *a* research
    # dossier claimed something, never which engine, model or vendor produced it.
    # Swapping the research implementation must not require a schema change.
    source_kind: Mapped[CompanyFieldSource] = mapped_column(
        Enum(CompanyFieldSource, name="company_field_source"),
        nullable=False,
    )
    # Free-form pointer to the origin: a snapshot id, a capture id, a URL. Kept
    # as text because the referents live in different tables and a real FK per
    # kind would mean one nullable column per source that ever exists.
    source_reference: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # Set when a research dossier is responsible for this observation.
    dossier_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        # Named explicitly; the generated name would exceed PostgreSQL's
        # 63-character identifier limit.
        ForeignKey(
            "company_dossier_versions.id",
            ondelete="SET NULL",
            name="fk_company_field_values_dossier_version",
        ),
        nullable=True,
    )
    # An explicit operator decision outranks every automatic source until a newer
    # operator decision replaces it.
    is_manual_override: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # --- Timestamps -----------------------------------------------------------
    # When the SOURCE observed the value. NULL when the source gave no time.
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When THIS system ingested the observation. Always known.
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Optional source confidence (0..1), recorded for auditability. The launch
    # policy does not weight by it; a source claiming 0.99 has not thereby
    # earned precedence over a more recent observation.
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    # --- Reconciliation result ------------------------------------------------
    is_current_winner: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    policy_version: Mapped[str] = mapped_column(String(50), nullable=False)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"CompanyFieldValue(company_id={self.company_id!r}, field={self.field_name!r}, "
            f"value={self.value!r}, winner={self.is_current_winner!r})"
        )
