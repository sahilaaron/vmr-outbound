"""Durable configuration and evidence for Email/Verification Agent Studio.

These tables extend the existing AgentJob lifecycle; they do not create another
queue. Policy and credential history is append-only, provider calls are children
of an existing VerificationAttempt, and learned domain formats remain evidence
until the Email policy explicitly consumes them.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ProviderCredentialVersion(Base):
    """One encrypted, immutable provider credential version."""

    __tablename__ = "provider_credential_versions"
    __table_args__ = (
        Index("ix_provider_credentials_provider_created", "provider_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    encrypted_secret: Mapped[str] = mapped_column(Text, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ProviderCredentialActivation(Base):
    """Append-only activation/rotation ledger for provider credentials."""

    __tablename__ = "provider_credential_activations"
    __table_args__ = (
        Index("ix_provider_credential_activations_provider", "provider_id", "activated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False)
    credential_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("provider_credential_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    previous_credential_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("provider_credential_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    activated_by: Mapped[str] = mapped_column(String(160), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    activated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class VerificationWaterfallPolicyVersion(Base):
    """Immutable ordered provider-waterfall policy snapshot."""

    __tablename__ = "verification_waterfall_policy_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    based_on_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("verification_waterfall_policy_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    change_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class VerificationWaterfallActivation(Base):
    __tablename__ = "verification_waterfall_activations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    policy_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("verification_waterfall_policy_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    previous_policy_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("verification_waterfall_policy_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    activated_by: Mapped[str] = mapped_column(String(160), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    activated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EmailPatternPolicyVersion(Base):
    """Immutable candidate ordering and stop-policy snapshot."""

    __tablename__ = "email_pattern_policy_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    based_on_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("email_pattern_policy_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    change_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EmailPatternPolicyActivation(Base):
    __tablename__ = "email_pattern_policy_activations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    policy_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("email_pattern_policy_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    previous_policy_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("email_pattern_policy_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    activated_by: Mapped[str] = mapped_column(String(160), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    activated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LearnedDomainEmailFormat(Base):
    """Append-only governed snapshot derived from accepted exact-address evidence."""

    __tablename__ = "learned_domain_email_formats"
    __table_args__ = (
        Index("ix_learned_domain_formats_domain_observed", "domain", "last_observed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    pattern_id: Mapped[str] = mapped_column(String(64), nullable=False)
    human_format: Mapped[str] = mapped_column(String(160), nullable=False)
    support_count: Mapped[int] = mapped_column(Integer, nullable=False)
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confidence: Mapped[float] = mapped_column(nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    conflicts: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    source_verification_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("exact_email_verifications.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class VerificationProviderAttempt(Base):
    """One provider step within an existing Verification Agent attempt."""

    __tablename__ = "verification_provider_attempts"
    __table_args__ = (
        Index("ix_verification_provider_attempts_job", "job_id", "provider_order"),
        UniqueConstraint(
            "verification_attempt_id",
            "provider_order",
            name="uq_verification_provider_step",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    verification_attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("verification_attempts.id", ondelete="CASCADE"),
        nullable=False,
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("verification_jobs.id", ondelete="CASCADE"), nullable=False
    )
    provider_order: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False)
    adapter_version: Mapped[str] = mapped_column(String(64), nullable=False)
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    provider_called: Mapped[bool] = mapped_column(Boolean, nullable=False)
    precise_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    result: Mapped[str | None] = mapped_column(String(50), nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    conflict: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    verification_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("exact_email_verifications.id", ondelete="SET NULL")
    )
    usage_ledger_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("usage_ledger_entries.id", ondelete="SET NULL")
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProviderTestRun(Base):
    """Safe, bounded record of an explicit one-address Studio provider test."""

    __tablename__ = "provider_test_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False)
    credential_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("provider_credential_versions.id", ondelete="SET NULL")
    )
    normalized_email: Mapped[str] = mapped_column(String(320), nullable=False)
    live: Mapped[bool] = mapped_column(Boolean, nullable=False)
    result: Mapped[str | None] = mapped_column(String(50), nullable=True)
    precise_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    usage_ledger_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("usage_ledger_entries.id", ondelete="SET NULL")
    )
    actor: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ImmutableStudioHistoryError(RuntimeError):
    """A caller attempted to alter immutable Agent Studio history."""


def _reject_history_mutation(_mapper: Any, _connection: Any, _target: Any) -> None:
    raise ImmutableStudioHistoryError("Email and Verification Studio history is append-only.")


for _history_model in (
    ProviderCredentialVersion,
    ProviderCredentialActivation,
    VerificationWaterfallPolicyVersion,
    VerificationWaterfallActivation,
    EmailPatternPolicyVersion,
    EmailPatternPolicyActivation,
    LearnedDomainEmailFormat,
    VerificationProviderAttempt,
    ProviderTestRun,
):
    event.listen(_history_model, "before_update", _reject_history_mutation)
    event.listen(_history_model, "before_delete", _reject_history_mutation)
