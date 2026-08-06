"""Imported-email evidence and imported source identifiers (IMP-001).

Two small, bounded tables backing the campaign-bound file import. Both exist for
the same reason: the facts they hold are *claims made by somebody else*, and the
existing tables all mean something stronger than that.

:class:`ImportedContactEmail` is not
:class:`~app.models.email_candidate.EmailCandidate` — a candidate is an address
this system decided to try, and the whole point of an imported address is that
nothing decided it. It is not
:class:`~app.models.email_evidence.ExactEmailVerification` either, and that
distinction is the one that matters most: that table means "a provider was asked
about this exact mailbox and answered", and an import asks nobody. Writing an
import into it would manufacture verification evidence out of a spreadsheet
cell, which is the single failure this whole area is built to prevent. So the
vendor's own words — its status, its verification source, its last-verified
timestamp — are stored here, labelled as the vendor's, next to the raw value
they were normalized from.

:class:`ImportSourceIdentifier` records the export's own primary keys (an Apollo
Contact Id, Account Id, Record Id). They make re-import idempotent and give
cross-source matching something exact to match on, but they are deliberately not
canonical identity: the same person may later arrive from Sales Navigator, the
browser extension, or by hand, and none of those know an Apollo id.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import (
    ImportedEmailSlot,
    ImportedEmailStageOutcome,
    ImportedVerificationOutcome,
)


class ImportedContactEmail(Base):
    """One address supplied by one imported row, with its provider's claims.

    Scoped to a Campaign as well as a Contact. The imported address is the
    address *this campaign* was told to use; a person legitimately reachable at a
    different address in another campaign is not a contradiction to resolve, and
    a contact-global row would have forced one.
    """

    __tablename__ = "imported_contact_emails"
    __table_args__ = (
        # One evidence record per address slot of one source row. Re-processing
        # the same row is therefore idempotent at the database, not only in
        # application code.
        UniqueConstraint(
            "import_row_id", "slot", name="uq_imported_contact_emails_import_row_slot"
        ),
        # At most one ACCEPTED address per source-row-content per Campaign. The
        # same row imported again — from a re-uploaded or edited file, which
        # produces a new batch and a new raw row — must not create a second
        # accepted record saying the same thing.
        #
        # Partial, and that is the whole point. A row the import REFUSED (held
        # for review, or a malformed address) also leaves evidence here, because
        # the operator resolving it needs to see what the file actually said. A
        # total constraint would have made that refusal permanent: once a held
        # row had written its evidence, the corrected file could never be
        # imported, and the failure would look like "already imported".
        Index(
            "uq_imported_contact_emails_accepted_row",
            "campaign_id",
            "row_fingerprint",
            "slot",
            unique=True,
            postgresql_where="email_stage_outcome = 'IMPORTED_EMAIL_ACCEPTED'",
        ),
        Index("ix_imported_contact_emails_contact_id", "contact_id"),
        Index("ix_imported_contact_emails_campaign_id", "campaign_id"),
        Index("ix_imported_contact_emails_batch_id", "import_batch_id"),
        Index("ix_imported_contact_emails_normalized_email", "normalized_email"),
        # The Email stage's lookup: the accepted primary address for one person
        # in one campaign.
        Index(
            "ix_imported_contact_emails_campaign_contact_slot",
            "campaign_id",
            "contact_id",
            "slot",
        ),
        # A rejected address never carries a bypass, and an accepted one always
        # does. Stated at the database because the pair is the whole truth model:
        # "we took this address" and "we asked nobody about it" have to arrive
        # together or neither is trustworthy.
        CheckConstraint(
            "(email_stage_outcome IS DISTINCT FROM 'IMPORTED_EMAIL_ACCEPTED')"
            " OR (verification_stage_outcome = 'VERIFICATION_BYPASSED_IMPORTED_EMAIL')",
            name="accepted_primary_records_bypass",
        ),
        # An accepted address must have survived normalization. A row that could
        # not produce a syntactically valid address cannot be the one we use.
        CheckConstraint(
            "(email_stage_outcome IS DISTINCT FROM 'IMPORTED_EMAIL_ACCEPTED')"
            " OR (normalized_email IS NOT NULL)",
            name="accepted_primary_normalized",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # --- Where it came from --------------------------------------------------
    import_batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("import_batches.id", ondelete="CASCADE"),
        nullable=False,
    )
    import_row_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("import_rows.id", ondelete="CASCADE"),
        nullable=False,
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
    )
    # NULL while a row is held for review: the address was supplied, but no
    # permanent person has been decided for it yet.
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="SET NULL"),
        nullable=True,
    )

    slot: Mapped[ImportedEmailSlot] = mapped_column(
        Enum(ImportedEmailSlot, name="imported_email_slot"),
        nullable=False,
    )

    # --- The address ---------------------------------------------------------
    # The verbatim cell, always kept. Normalization is lossless in meaning, but
    # "lossless" is a claim the original value is what lets anyone check.
    raw_email: Mapped[str] = mapped_column(String(512), nullable=False)
    # NULL when the supplied value could not be normalized to a valid address.
    normalized_email: Mapped[str | None] = mapped_column(String(320), nullable=True)

    # --- Source provenance ---------------------------------------------------
    source_row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    source_sheet_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: SHA-256 of the uploaded file, so an address can be traced to exact bytes.
    source_file_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The recognized source schema, e.g. ``apollo_contact_export_v1``.
    source_schema: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Deterministic fingerprint of the identity-bearing cells of the source row.
    row_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    # --- The provider's claims, labelled as the provider's --------------------
    #
    # Every column below is what the export said, never what this system found.
    # The raw value is preserved beside the case-folded one so "Valid" and
    # "valid" compare equal without the original wording being lost.
    provider_source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider_status_raw: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_status_normalized: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_verification_source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider_catch_all_raw: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_catch_all_normalized: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: The vendor's own last-verified timestamp, parsed when it could be. The
    #: unparsed text is kept regardless, because a timestamp we could not read is
    #: still evidence that the vendor claimed one.
    provider_last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    provider_last_verified_raw: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # --- What VMR did (never what a provider said) ---------------------------
    #
    # NULL on a secondary or tertiary slot: those are retained, not acted on.
    email_stage_outcome: Mapped[ImportedEmailStageOutcome | None] = mapped_column(
        Enum(ImportedEmailStageOutcome, name="imported_email_stage_outcome"),
        nullable=True,
    )
    verification_stage_outcome: Mapped[ImportedVerificationOutcome | None] = mapped_column(
        Enum(ImportedVerificationOutcome, name="imported_verification_outcome"),
        nullable=True,
    )
    #: Why an address was rejected, as a stable machine code.
    rejection_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    @property
    def is_accepted_primary(self) -> bool:
        """Whether this is the address the campaign was told to use."""

        return (
            self.slot is ImportedEmailSlot.PRIMARY
            and self.email_stage_outcome is ImportedEmailStageOutcome.IMPORTED_EMAIL_ACCEPTED
        )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"ImportedContactEmail(slot={self.slot.value!r}, "
            f"email={self.normalized_email!r}, outcome="
            f"{self.email_stage_outcome.value if self.email_stage_outcome else None!r})"
        )


class ImportSourceIdentifier(Base):
    """One external system's own key for a Contact or a Company (IMP-001).

    Exactly one of ``contact_id`` / ``company_id`` is set. Two nullable columns
    with a check constraint rather than a polymorphic ``subject_type`` string,
    because a real foreign key on each is what stops an identifier outliving the
    record it names.

    The unique constraint on ``(system, identifier_kind, identifier_value)`` is
    what makes the identifier usable for matching at all: without it, "the Apollo
    contact 42" could name two people and the import would have to guess which.
    A second claim on an identifier another record already holds is refused by
    the service and surfaced for review — never merged.
    """

    __tablename__ = "import_source_identifiers"
    __table_args__ = (
        UniqueConstraint(
            "system",
            "identifier_kind",
            "identifier_value",
            name="uq_import_source_identifiers_system_kind_value",
        ),
        Index("ix_import_source_identifiers_contact_id", "contact_id"),
        Index("ix_import_source_identifiers_company_id", "company_id"),
        CheckConstraint(
            "(contact_id IS NOT NULL) <> (company_id IS NOT NULL)",
            name="exactly_one_subject",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=True,
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=True,
    )
    #: The external system, e.g. ``apollo``. Lower-cased by the service.
    system: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Which of that system's keys this is, e.g. ``contact_id``, ``account_id``.
    identifier_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Stored VERBATIM apart from surrounding whitespace. Vendor keys are opaque
    #: and may be case-sensitive; folding case would silently merge two of them.
    identifier_value: Mapped[str] = mapped_column(String(256), nullable=False)

    #: The batch that first observed this identifier, for audit.
    # Named explicitly: the naming convention would generate a 63-character
    # identifier here, exactly at PostgreSQL's limit, where a later rename of
    # either table would silently truncate it and make `alembic check` disagree
    # with the models forever after.
    first_seen_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "import_batches.id",
            ondelete="SET NULL",
            name="fk_import_source_identifiers_first_seen_batch",
        ),
        nullable=True,
    )
    recorded_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"ImportSourceIdentifier(system={self.system!r}, "
            f"kind={self.identifier_kind!r}, value={self.identifier_value!r})"
        )
