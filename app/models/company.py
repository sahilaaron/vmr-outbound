"""Company model (DAT-001 representation).

Represents an employer/organization as a first-class record so later phases can
attach company-level insights, scores, and email-pattern evidence. This slice
only *represents* companies; resolving contacts to companies and deduplicating
companies is DAT-004 and is deliberately not implemented here.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


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
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(255), nullable=True)
    country: Mapped[str | None] = mapped_column(String(128), nullable=True)
    company_size: Mapped[str | None] = mapped_column(String(64), nullable=True)

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
