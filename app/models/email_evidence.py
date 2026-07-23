"""Email-intelligence evidence models (DAT-001 representation).

Three *structurally distinct* kinds of email evidence are kept in separate tables
so they can never be conflated (AGENTS.md "Email Intelligence Rules"):

* :class:`ExactEmailVerification` — evidence about one full email address.
* :class:`DomainPatternObservation` — evidence about a naming pattern at a domain.
* :class:`MailDomainObservation` — MX/provider/catch-all facts about a domain.

This slice only represents the tables. No MillionVerifier integration, candidate
generation, or verification behaviour is implemented here.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import EmailVerificationResult


class ExactEmailVerification(Base):
    """Cached evidence about one exact, normalized full email address."""

    __tablename__ = "exact_email_verifications"
    __table_args__ = (
        Index("ix_exact_email_verifications_email", "email"),
        Index("ix_exact_email_verifications_checked_at", "checked_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # The exact normalized (lower-cased) address the evidence is about.
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    result: Mapped[EmailVerificationResult] = mapped_column(
        Enum(EmailVerificationResult, name="email_verification_result"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    # The verification-policy version under which this evidence was produced
    # (required): it governs how the result may later be reused (TTLs, safe mode).
    policy_version: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_result_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    provider_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Controlled raw provider payload; never stores secrets.
    raw_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Optional association to a contact (kept nullable; evidence is about the address).
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"ExactEmailVerification(email={self.email!r}, result={self.result.value!r}, "
            f"provider={self.provider!r})"
        )


class DomainPatternObservation(Base):
    """Evidence about an email naming pattern at a company domain."""

    __tablename__ = "domain_pattern_observations"
    __table_args__ = (Index("ix_domain_pattern_observations_domain", "domain"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    # e.g. "{first}.{last}", "{f}{last}".
    pattern: Mapped[str] = mapped_column(String(255), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # An observed address that supports the pattern (evidence, not proof of others).
    sample_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"DomainPatternObservation(domain={self.domain!r}, pattern={self.pattern!r})"


class MailDomainObservation(Base):
    """MX / provider / catch-all observations about a mail domain."""

    __tablename__ = "mail_domain_observations"
    __table_args__ = (Index("ix_mail_domain_observations_domain", "domain"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    # Catch-all is uncertainty, never proof a mailbox exists (AGENTS.md).
    is_catch_all: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    accepts_all: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    mx_provider: Mapped[str | None] = mapped_column(String(255), nullable=True)
    raw_observation: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"MailDomainObservation(domain={self.domain!r}, catch_all={self.is_catch_all!r})"
