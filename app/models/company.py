"""Company model (DAT-001 representation, widened for the APP-003 workspace).

Represents an employer/organization as a first-class record: the permanent thing
that Contacts belong to and that company research attaches to. DAT-001 created
the row; APP-003 gives it an identity beyond a domain string, a research state,
and a place for research to land without overwriting what is already known.

Three boundaries this model holds:

* **Canonical fields are not research output.** ``industry``, ``country`` and
  ``company_size`` are operational values. What a dossier *claims* lives in
  :class:`~app.models.company_dossier.CompanyDossierVersion`; what won and why
  lives in :class:`~app.models.company_field_value.CompanyFieldValue`. Research
  is evidence, never an unconditional overwrite.
* **Unknown is not false.** Every firmographic column is nullable and NULL means
  "nobody has told us", which is a different claim from any value at all.
* **Research state is its own dimension**, not folded into a single overall
  status. A company can be researched and conflicted, or unresearched and
  perfectly well linked; one status column could not say either.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import ResearchState


class Company(Base):
    """A normalized company/organization."""

    __tablename__ = "companies"
    __table_args__ = (
        # A domain identifies a company; unique when present (partial index so
        # multiple domain-less companies can coexist).
        Index(
            "uq_companies_domain",
            "domain",
            unique=True,
            postgresql_where="domain IS NOT NULL",
        ),
        Index("ix_companies_name", "name"),
        # LinkedIn identity is deliberately NOT unique. Two companies legitimately
        # sharing an id would be a conflict to surface for review, not a write to
        # reject at 3am — see app.services.companies.conflicts.
        Index(
            "ix_companies_linkedin_company_id",
            "linkedin_company_id",
            postgresql_where="linkedin_company_id IS NOT NULL",
        ),
        Index("ix_companies_research_state", "research_state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(255), nullable=True)
    country: Mapped[str | None] = mapped_column(String(128), nullable=True)
    company_size: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # --- LinkedIn identity (APP-003) -----------------------------------------
    #
    # A second identity axis alongside the domain. Recorded because a company
    # page is often the only place a domain and an organization are stated
    # together by the same source, and because two companies claiming one
    # LinkedIn id is a conflict worth seeing.
    linkedin_company_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    linkedin_company_id: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # --- Research dimension (APP-003) ----------------------------------------
    #
    # No research engine exists yet; APP-004 owns it. Until then every company
    # reports NOT_REQUESTED, which is the truth rather than a placeholder.
    research_state: Mapped[ResearchState] = mapped_column(
        Enum(ResearchState, name="research_state"),
        nullable=False,
        default=ResearchState.NOT_REQUESTED,
        # Named by the PostgreSQL label (the enum member NAME, which is what
        # SQLAlchemy emits), not the Python value. Existing rows need this to
        # become NOT NULL without a rewrite.
        server_default=ResearchState.NOT_REQUESTED.name,
    )
    # When research last completed — NOT when it was last attempted. A failed run
    # leaves this alone, so "last researched" never overstates what is known.
    last_researched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"Company(id={self.id!r}, name={self.name!r}, domain={self.domain!r})"
