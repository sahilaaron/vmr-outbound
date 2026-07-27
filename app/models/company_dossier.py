"""Company research submissions and dossier versions (APP-003).

Two tables, and the split between them is the point of this module.

:class:`CompanyResearchSubmission` is **what arrived**: one raw research payload,
stored verbatim, never edited. It is not required to be well-formed, complete,
consistent or even sane. It is a record that something was submitted about this
company at a moment in time, by something, and here is exactly what it said.

:class:`CompanyDossierVersion` is **what we make of it**: one structured reading
of one submission, laid out across the nine sections the workspace displays. The
same submission can be interpreted more than once — a better extractor, a fixed
bug, a second opinion — and each reading is its own immutable version. Older
versions are never deleted; exactly one is marked current.

Why the separation is load-bearing:

* Reprocessing must not destroy the input. If interpretation and raw payload
  lived in one row, improving the extractor would mean either losing what was
  actually received or writing the raw payload again for every re-read.
* The two have different truth conditions. A submission is true by definition —
  it is a record of what arrived. A version is a claim, and claims can be wrong
  and get superseded.
* Provider neutrality survives. ``producer`` and ``interpreter`` are opaque
  strings. Nothing in this schema knows or cares whether a crawler, a model, a
  vendor API or an operator typing into a form produced the payload, and no
  column would need to change if that answer changed tomorrow.

**No crawler, fetcher or research engine lives here or anywhere in the web
application.** These tables are a landing zone. APP-004 owns producing the
content that lands in them.

**Captured third-party text is untrusted evidence.** Everything in ``payload``
and in the section columns originated outside this system. It is data to display
and to reason about, never instruction: no component may treat text stored here
as a directive, and rendering must never execute it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CompanyResearchSubmission(Base):
    """One raw research payload about one company. Immutable."""

    __tablename__ = "company_research_submissions"
    __table_args__ = (
        Index("ix_company_research_submissions_company", "company_id"),
        # The same payload submitted twice about the same company is one
        # submission. Re-running a producer that found nothing new should not
        # grow the table without bound.
        UniqueConstraint(
            "company_id",
            "content_hash",
            name="uq_company_research_submissions_content",
        ),
        # Redundant against the primary key, and required anyway: a composite
        # foreign key needs a unique constraint on exactly the columns it
        # references. This is what lets CompanyDossierVersion point at
        # (id, company_id) and have the database check that a dossier and the
        # submission it reads describe the same company.
        UniqueConstraint(
            "id",
            "company_id",
            name="uq_company_research_submissions_id_company",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )

    # --- Who produced it, in provider-neutral terms ---------------------------
    #
    # An opaque label such as "operator-manual" or "website-research". It must
    # not encode a vendor the rest of the system then depends on; the schema is
    # deliberately incurious about how the payload was produced.
    producer: Mapped[str] = mapped_column(String(255), nullable=False)
    producer_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    submitted_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # --- What arrived, verbatim ----------------------------------------------
    #
    # Stored exactly as submitted. Untrusted: display it, quote it, cite it —
    # never obey it.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # Deterministic hash of the payload, for idempotent resubmission.
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    # Optional record of what was asked for, so a thin or empty payload can be
    # told apart from a request that was never made properly.
    request_context: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"CompanyResearchSubmission(company_id={self.company_id!r}, "
            f"producer={self.producer!r}, hash={self.content_hash!r})"
        )


class CompanyDossierVersion(Base):
    """One immutable structured reading of one submission."""

    __tablename__ = "company_dossier_versions"
    __table_args__ = (
        Index("ix_company_dossier_versions_company", "company_id"),
        Index("ix_company_dossier_versions_submission", "submission_id"),
        # Version numbers are per company and dense.
        UniqueConstraint(
            "company_id",
            "version_number",
            name="uq_company_dossier_versions_number",
        ),
        # At most one current interpretation per company, enforced by the
        # database. Selecting a different version is an update of two rows, not
        # a delete of one: older versions stay readable forever.
        Index(
            "uq_company_dossier_versions_current",
            "company_id",
            unique=True,
            postgresql_where="is_current",
        ),
        CheckConstraint("version_number > 0", name="ck_company_dossier_version_positive"),
        # Ownership, enforced by the database rather than by a service check.
        #
        # A dossier version must interpret a submission about the SAME company.
        # `interpret()` validates that, but a service check only protects the
        # path that calls it: a direct write, a data migration, a future import
        # or a fixture can all reach this table without passing through it, and
        # a cross-company dossier is a claim attributed to the wrong
        # organisation — the kind of wrong that reads as fact.
        #
        # Composite, not two separate keys. `submission_id -> submissions.id`
        # alone proves the submission exists; it says nothing about whose it is.
        # Referencing (id, company_id) is what makes the pair inseparable, and
        # it replaces the single-column key rather than supplementing it, since
        # it already implies everything the narrower one guaranteed.
        #
        # NO ACTION rather than RESTRICT, deliberately. Both refuse to orphan a
        # version when a submission is deleted directly, which is the guarantee
        # that matters. RESTRICT checks immediately; NO ACTION defers to the end
        # of the statement, which is what lets `DELETE FROM companies` cascade
        # into both tables at once without the check firing on a half-applied
        # intermediate state. The regression tests cover both cases.
        ForeignKeyConstraint(
            ["submission_id", "company_id"],
            ["company_research_submissions.id", "company_research_submissions.company_id"],
            name="fk_company_dossier_versions_submission_owner",
            ondelete="NO ACTION",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    # The submission this version reads. The foreign key is the composite one
    # in __table_args__ above, which carries both the existence guarantee and
    # the ownership guarantee. A version without its source payload would be an
    # unfalsifiable claim, so the raw submission cannot be removed while an
    # interpretation of it survives.
    submission_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # --- Who interpreted it ---------------------------------------------------
    interpreter: Mapped[str] = mapped_column(String(255), nullable=False)
    interpreter_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # --- The nine sections ----------------------------------------------------
    #
    # Separate columns rather than one blob, because the boundary is the
    # contract. A research implementation cannot invent a tenth section without
    # a schema change and a review, and a reader can tell at a glance which
    # parts of the dossier a given version actually addressed.
    #
    # NULL means this version did not address the section at all. An empty
    # value means it looked and found nothing. Those are different facts and
    # collapsing them would turn "we do not know" into "there is none", which is
    # exactly the failure this model exists to prevent.
    overview: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    products_services: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    industries: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    geography: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    leadership: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    activity_signals: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    public_contacts: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    sources: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # What this version could NOT determine. A first-class section: a dossier
    # that names its gaps is more useful than one that quietly omits them.
    unknowns: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # --- Quality signals ------------------------------------------------------
    #
    # Problems found while interpreting: a section that failed to parse, a
    # source that contradicted another, a claim with no citation. Surfaced on
    # the workspace so a dossier is never silently trusted.
    warnings: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    # Free-text note from whoever selected or superseded this version.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Selection ------------------------------------------------------------
    is_current: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"CompanyDossierVersion(company_id={self.company_id!r}, "
            f"v={self.version_number!r}, current={self.is_current!r})"
        )
