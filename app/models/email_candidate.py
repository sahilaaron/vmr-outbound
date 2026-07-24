"""Generated / imported email-candidate model (EML-002 / EML-004 / EML-005).

A candidate is one exact address the pipeline *might* verify for a contact,
together with the deterministic rule and engine version that produced it and the
transparent ranking evidence. A candidate is never proof a mailbox exists — it is
an address to check. The single selected candidate per contact is the address a
verification job targets; selection reasoning is stored so the operator can see
*why* that address was chosen (EML-006).

Candidates are kept structurally distinct from verification evidence: this table
says "we could try this address", never "this address is valid".
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
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import EmailCandidateSource


class EmailCandidate(Base):
    """One ranked, deterministic exact-address candidate for a contact."""

    __tablename__ = "email_candidates"
    __table_args__ = (
        # A contact never holds the same candidate address twice (EML-002:
        # "without duplicates"). Regenerating replaces the set idempotently.
        UniqueConstraint("contact_id", "email", name="uq_email_candidates_contact_email"),
        Index("ix_email_candidates_contact_id", "contact_id"),
        Index("ix_email_candidates_email", "email"),
        # At most one selected candidate per contact is enforced in the service
        # layer and asserted by tests; this partial unique index makes it a
        # database invariant too.
        Index(
            "uq_email_candidates_one_selected",
            "contact_id",
            unique=True,
            postgresql_where="selected",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    # The normalized (lower-cased) exact candidate address.
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    source: Mapped[EmailCandidateSource] = mapped_column(
        Enum(EmailCandidateSource, name="email_candidate_source"),
        nullable=False,
    )
    # The naming pattern that produced a generated candidate, e.g. "{first}.{last}".
    # Null for an imported exact address (no pattern was applied).
    pattern: Mapped[str | None] = mapped_column(String(255), nullable=True)
    local_part: Mapped[str] = mapped_column(String(255), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    # Versioned generation engine that produced this candidate set (EML-002).
    engine_version: Mapped[str] = mapped_column(String(50), nullable=False)
    # Lower rank == earlier / more likely (0 is best). Deterministic.
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    # Transparent numeric ranking score and its human explanation (EML-004/006).
    rank_score: Mapped[float] = mapped_column(Float, nullable=False)
    rank_reason: Mapped[str] = mapped_column(Text, nullable=False)
    # Exactly one candidate per contact is selected for verification (EML-005).
    selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    selection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"EmailCandidate(email={self.email!r}, source={self.source.value!r}, "
            f"rank={self.rank!r}, selected={self.selected!r})"
        )
