"""Company-domain enrichment records (DAT-010, extended by DAT-014).

A LinkedIn or Sales Navigator capture never exposes ``company_domain``, so every
captured company must have a domain supplied before it can become canonical.
This table records the logo.dev lookup and the operator's explicit confirmation,
entirely separately from the immutable raw evidence
(:class:`~app.models.import_batch.ImportRow` and
:class:`~app.models.linkedin_profile.LinkedInProfileSnapshot`, never mutated) and
from a contact's :class:`~app.models.provenance.ProvenanceRecord`.

One record is owned by **exactly one** of three things, enforced by a check
constraint:

* a staged import batch (DAT-010) — one record per unique company per batch, so
  a confirmed domain propagates to every matching staged row exactly once;
* a contact capture (DAT-014) — one record per capture, resolving the single
  company that capture observed;
* a permanent contact — one record per contact, for a surface such as Google
  Sheets that produces the person directly and has no capture to hang the
  lookup off. Adding this owner is what lets an unseen company arriving from a
  spreadsheet enter the *same* lookup, candidate store and confirmation path a
  captured company uses, rather than a second one built beside it.

The table name is historical: it began as Sales-Navigator-only and is now the
one company-domain resolution store for every acquisition path. There is
deliberately no second candidate store.

Because a confirmation is keyed by ``company_key`` and read back by
:func:`app.services.captures.promotion.prior_confirmed_domains` regardless of
owner, a domain confirmed from one surface is immediately reusable by the
others. That is the point: the surfaces share evidence rather than each
accumulating their own.

It is provenance/audit metadata: it holds what was searched, what candidates
logo.dev returned, and which domain the operator confirmed (a candidate, a manual
override, or an explicit "leave unresolved"), with the actor and time. The
confirmed domain is applied to the batch's matching rows at preview/confirm time
as an overlay; the raw capture is not touched.

Nothing here is a secret: the logo.dev API key is never stored, serialized, or
referenced by this model.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import (
    EnrichmentConfirmationSource,
    EnrichmentConfirmationStatus,
    EnrichmentLookupStatus,
)


class SalesNavCompanyEnrichment(Base):
    """One company to resolve, plus its provider lookup and operator decision."""

    __tablename__ = "salesnav_company_enrichments"
    __table_args__ = (
        # One enrichment record per unique company (by normalized key) per batch,
        # so a confirmed domain propagates to every matching row exactly once and
        # a company is looked up at most once unless the operator refreshes.
        UniqueConstraint(
            "batch_id", "company_key", name="uq_salesnav_company_enrichments_batch_company"
        ),
        Index("ix_salesnav_company_enrichments_batch_id", "batch_id"),
        # DAT-014: one record per contact capture. A partial unique index (rather
        # than a constraint) so batch-owned rows, whose capture_id is NULL, are
        # unaffected.
        Index(
            "uq_salesnav_company_enrichments_capture",
            "capture_id",
            unique=True,
            postgresql_where="capture_id IS NOT NULL",
        ),
        # One record per permanent contact, for the surfaces that have no
        # capture. Partial unique index for the same reason as the capture one.
        Index(
            "uq_salesnav_company_enrichments_contact",
            "contact_id",
            unique=True,
            postgresql_where="contact_id IS NOT NULL",
        ),
        # Exactly one owner. A record with no owner would be unreachable
        # evidence; one with two would let a confirmation leak across paths.
        # Bare name: the convention prepends ``ck_<table>_``. See the note in
        # ``app/models/collection.py`` and migration ``b6d4e07a1f38``.
        CheckConstraint(
            "(batch_id IS NOT NULL)::int + (capture_id IS NOT NULL)::int "
            "+ (contact_id IS NOT NULL)::int = 1",
            name="single_owner",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("import_batches.id", ondelete="CASCADE"),
        nullable=True,
    )
    # DAT-014: the contact capture whose company this record resolves.
    capture_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("linkedin_profile_snapshots.id", ondelete="CASCADE"),
        nullable=True,
    )
    # The permanent contact whose stated employer this record resolves, for a
    # surface that produced the Contact without a capture.
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=True,
    )
    # Normalized grouping key (collapsed, case-folded company name). Rows whose
    # mapped company_name matches this key receive the confirmed domain.
    company_key: Mapped[str] = mapped_column(String(512), nullable=False)
    # The company name as first seen (for display); the raw values stay on the
    # immutable import rows.
    company_name: Mapped[str] = mapped_column(String(512), nullable=False)
    row_count: Mapped[int] = mapped_column(nullable=False, default=0)

    # --- Captured identity hints (DAT-014) -----------------------------------
    # What the capture actually showed about the company, preserved so a later
    # reviewer can judge a candidate without reopening LinkedIn. These are
    # HINTS: none of them resolves a domain on its own.
    company_linkedin_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    company_linkedin_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # The person's displayed location or the role's location, whichever the
    # capture showed. Context for disambiguating same-named companies.
    location_hint: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # --- Lookup state --------------------------------------------------------
    lookup_status: Mapped[EnrichmentLookupStatus] = mapped_column(
        Enum(EnrichmentLookupStatus, name="enrichment_lookup_status"),
        nullable=False,
        default=EnrichmentLookupStatus.NOT_STARTED,
    )
    # Candidates returned by the provider, as a list of objects carrying
    # ``domain``, ``name``, ``rank`` (1-based provider order) and ``confidence``.
    # logo.dev's Search Brands API returns no score, so ``confidence`` is null —
    # recorded explicitly rather than omitted, so nobody later mistakes rank for
    # confidence. Never includes a logo URL or the API key.
    candidates: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    # Candidates the operator explicitly rejected, each with the reason, actor
    # and time. Preserved as provenance: a rejection is a decision, not a gap.
    rejected_candidates: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    # The exact query string sent to the provider (the company name). Non-secret.
    lookup_query: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # The normalized form of that query, so an identical company is recognisable
    # across captures even when the visible spelling differs.
    normalized_query: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Which provider answered, and the version of the lookup contract used.
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lookup_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    looked_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Count of lookups run for this company (refresh/retry increments it).
    lookup_attempts: Mapped[int] = mapped_column(nullable=False, default=0)

    # --- Model fallback state (the searched answer behind the brand matcher) ---
    # Kept in its own columns rather than folded into the fields above, because
    # "the brand matcher found nothing" and "the model then found something" are
    # two separate facts about this company and an operator needs to see both.
    # Collapsing them would also make `lookup_attempts` ambiguous — a number that
    # sometimes counts provider calls and sometimes model calls is worse than no
    # number.
    model_lookup_status: Mapped[EnrichmentLookupStatus] = mapped_column(
        Enum(EnrichmentLookupStatus, name="enrichment_lookup_status"),
        nullable=False,
        default=EnrichmentLookupStatus.NOT_STARTED,
    )
    # The domain the model asserted. Stored even when the policy went on to reject
    # it as unsuitable: what the model said is provenance, and a reviewer deciding
    # whether to trust the fallback at all needs to see its misses too.
    model_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # A page the model says it read that shows the domain belongs to this company.
    # The single most useful thing on this record for an operator confirming a
    # provisional domain, and the model is asked for it explicitly.
    model_source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # The model's own words for declining, or the seam's words for a failure.
    # Operator-facing only; never an input to any decision.
    model_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_looked_up_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    model_lookup_attempts: Mapped[int] = mapped_column(nullable=False, default=0)

    # --- Operator confirmation ----------------------------------------------
    confirmation_status: Mapped[EnrichmentConfirmationStatus] = mapped_column(
        Enum(EnrichmentConfirmationStatus, name="enrichment_confirmation_status"),
        nullable=False,
        default=EnrichmentConfirmationStatus.UNCONFIRMED,
    )
    # The domain the operator confirmed (normalized hostname), or NULL when
    # unconfirmed or explicitly left unresolved.
    confirmed_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    confirmation_source: Mapped[EnrichmentConfirmationSource | None] = mapped_column(
        Enum(EnrichmentConfirmationSource, name="enrichment_confirmation_source"),
        nullable=True,
    )
    confirmed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Optional operator note explaining a manual override or an unresolved mark.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    @property
    def owner_label(self) -> str:
        """Which acquisition path owns this record ("batch", "capture" or "contact")."""

        if self.capture_id is not None:
            return "capture"
        if self.contact_id is not None:
            return "contact"
        return "batch"

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"SalesNavCompanyEnrichment(owner={self.owner_label}, "
            f"company_key={self.company_key!r}, lookup_status={self.lookup_status.value!r}, "
            f"confirmation_status={self.confirmation_status.value!r})"
        )
