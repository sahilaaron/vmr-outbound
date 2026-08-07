"""Import lineage for the Admin Workbench (IMP-001 seen from ADM-001).

The Admin Workbench answers "why is this Contact in this state?". For a Contact
that arrived through a Campaign-bound file import, two of those answers are not
reachable from Phase 2 state alone:

* the Email stage completed without generating a single candidate, and
* the Verification stage completed without calling any provider.

The Workbench's existing projections are right to refuse to explain those. The
Email projection reads ``terminal_outcome`` from the Email Agent's own
vocabulary, so it already reports ``imported_email_accepted`` correctly and —
importantly — its ``accepted`` property stays False, because that property means
*verified* and an imported address never is. The Verification projection reads
its decision from :class:`~app.services.verification.decisions.VerificationDecision`
and deliberately reports anything outside that vocabulary as undecided rather
than guessing; the import bypass writes ``bypassed``, which is not a
verification decision and must never become one.

Left alone, the two together render a truthful-but-silent page: the operator
sees "no committed decision" on a stage that did commit an outcome. This module
supplies the missing half from the authoritative import records, so the page can
say what actually happened without either service learning to lie.

Nothing here holds authority and nothing here writes. Every value is one the
import services committed, read back through the same public helpers the
customer screens use (:mod:`app.services.imports.campaign_import`), so the
Workbench never becomes a second implementation of the importer.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.contact import Contact
from app.models.enums import (
    ImportedEmailSlot,
    ImportedEmailStageOutcome,
    ImportRowOutcome,
)
from app.models.import_batch import ImportBatch, ImportRow, ImportRowValidation
from app.models.imported_email import ImportedContactEmail, ImportSourceIdentifier
from app.services.imports import apollo, campaign_import, display

#: Rendered wherever the import path's Verification outcome appears. Written
#: once, here, so no template can paraphrase it into something weaker.
BYPASS_STATEMENT = (
    "No verification provider was called for this address. It is an accepted "
    "imported record, not a provider-verified mailbox."
)

#: The same statement for the Email stage.
NO_DISCOVERY_STATEMENT = (
    "No candidate address was generated and no domain pattern was applied. The "
    "address came from the imported file."
)

#: A raw row that has no validation record at all.
#:
#: Not an ``ImportRowOutcome`` member, deliberately: it is the absence of one.
#: This surface previously substituted ``"rejected"`` here, which asserted that
#: the system had refused a row it had in fact never reached — the shape an
#: interrupted batch leaves behind.
UNPROCESSED_OUTCOME = "unprocessed"

#: How an import batch's row outcomes read on an operator surface. Keys are
#: ``ImportRowOutcome`` values as the importer commits them, plus
#: :data:`UNPROCESSED_OUTCOME`. Every member of the enum has an entry: a row
#: rendered with its raw machine name is a row the operator has to decode.
OUTCOME_LABELS: dict[str, str] = {
    "pending": "not yet processed",
    "accepted": "imported",
    "duplicate": "already present",
    "rejected": "refused",
    "suppressed": "suppressed",
    "ambiguous": "held for review",
    UNPROCESSED_OUTCOME: "no result recorded",
}

#: The outcomes that mean an operator has something to decide.
#:
#: Keyed on the outcome, never on ``error_code``. A benign disposition carries an
#: error code too — ``already_imported`` and ``duplicate_row_in_file`` both do —
#: so filtering on the code put rows that had successfully resolved to a Contact
#: on a page headed "no Contact was created".
ATTENTION_OUTCOMES: frozenset[str] = frozenset(
    {
        ImportRowOutcome.REJECTED.value,
        ImportRowOutcome.AMBIGUOUS.value,
        ImportRowOutcome.SUPPRESSED.value,
    }
)


def _safe(value: str | None) -> str | None:
    """Neutralize a spreadsheet-supplied string before it reaches a surface.

    The shared projection boundary, so this reader and every template filter
    agree about what neutralization means. See
    :mod:`app.services.imports.display`.
    """

    return display.safe_optional(value)


@dataclass(frozen=True)
class SourceIdentifierRow:
    """One external system's own key, as recorded — never as canonical identity."""

    system: str
    kind: str
    value: str
    recorded_by: str | None
    first_seen_batch_id: uuid.UUID | None
    created_at: datetime | None

    @property
    def label(self) -> str:
        return f"{self.system} {self.kind.replace('_', ' ')}"


@dataclass(frozen=True)
class ImportedAddressRow:
    """One imported address slot with its provider's claims, kept as claims.

    Every provider field is named so that reading it aloud makes the ownership
    obvious. There is deliberately no attribute called ``verified``, and
    :attr:`provider_called` is a constant False rather than a stored flag: the
    import path cannot call a provider, so a template asking the question must
    get the same answer every time.
    """

    slot: str
    email: str | None
    raw_email: str
    accepted: bool
    rejection_code: str | None
    email_stage_outcome: str | None
    verification_stage_outcome: str | None
    provider_source: str | None
    provider_claimed_status: str | None
    provider_claimed_status_raw: str | None
    provider_claimed_verification_source: str | None
    provider_claimed_catch_all: str | None
    provider_claimed_last_verified_at: datetime | None
    provider_claimed_last_verified_raw: str | None
    source_row_number: int
    source_sheet_name: str | None
    source_file_checksum: str
    source_schema: str

    @property
    def provider_called(self) -> bool:
        """Always False. The import path has no provider to call."""

        return False

    @property
    def has_provider_claims(self) -> bool:
        return any(
            (
                self.provider_source,
                self.provider_claimed_status,
                self.provider_claimed_verification_source,
                self.provider_claimed_catch_all,
                self.provider_claimed_last_verified_at,
                self.provider_claimed_last_verified_raw,
            )
        )

    @property
    def provenance_label(self) -> str:
        return "supplied by the imported file"


@dataclass(frozen=True)
class ImportBatchRow:
    """One campaign-bound file import, summarised."""

    batch_id: uuid.UUID
    campaign_id: uuid.UUID
    filename: str | None
    sanitized_filename: str | None
    source_schema: str | None
    source_format: str
    sheet_name: str | None
    sheet_index: int | None
    status: str
    content_hash: str
    uploaded_by: str | None
    source_name: str | None
    created_at: datetime | None
    confirmed_at: datetime | None
    completed_at: datetime | None
    total_rows: int
    accepted_rows: int
    rejected_rows: int
    duplicate_rows: int
    suppressed_rows: int
    ambiguous_rows: int
    already_in_campaign_rows: int
    contacts_created: int
    error_detail: str | None

    @property
    def href(self) -> str:
        return f"/admin/imports/{self.batch_id}"

    @property
    def display_name(self) -> str:
        """The sanitized name, never the operator's original path or bytes."""

        return self.sanitized_filename or self.filename or "unnamed upload"

    @property
    def needs_attention(self) -> bool:
        return bool(self.rejected_rows or self.ambiguous_rows or self.error_detail)

    @property
    def counts(self) -> campaign_import.BatchCounts:
        """The row outcomes as buckets that actually partition the file.

        ``duplicate_rows`` covers three dispositions and
        ``already_in_campaign_rows`` is one of them, so rendering the durable
        columns side by side counted the same row twice under a heading that
        promised one outcome per row.
        """

        return campaign_import.batch_counts(self)

    @property
    def schema_label(self) -> str:
        """What the file was read as — never a claim about who produced it.

        A file is recognized on four required headers, which any hand-made CSV
        can satisfy. Calling that "an Apollo export" would manufacture vendor
        provenance out of a column list, which is the exact failure this whole
        area exists to prevent, so a file that lacks the distinctive Apollo
        headers is described as merely compatible with the schema.
        """

        if self.source_schema != apollo.APOLLO_SCHEMA_ID:
            return self.source_schema or "no recognized schema"
        recorded = self.source_name or campaign_import.APOLLO_COMPATIBLE_SOURCE_NAME
        return f"{recorded} (schema version 1)"


@dataclass(frozen=True)
class ImportRowLineageRow:
    """One committed source row and everything it produced."""

    row_id: uuid.UUID
    row_number: int
    sheet_name: str | None
    outcome: str
    error_code: str | None
    note: str | None
    warnings: tuple[tuple[str, str], ...]
    row_fingerprint: str | None
    contact_id: uuid.UUID | None
    contact_label: str | None
    contact_match_basis: str | None
    company_id: uuid.UUID | None
    company_label: str | None
    company_match_basis: str | None
    supplied_company_name: str | None
    campaign_contact_id: uuid.UUID | None
    membership_action: str | None
    primary_address: ImportedAddressRow | None
    campaign_id: uuid.UUID | None = None
    batch_id: uuid.UUID | None = None

    @property
    def batch_href(self) -> str | None:
        return f"/admin/imports/{self.batch_id}" if self.batch_id else None

    @property
    def outcome_label(self) -> str:
        return OUTCOME_LABELS.get(self.outcome, self.outcome)

    @property
    def held_for_review(self) -> bool:
        return self.outcome == "ambiguous"

    @property
    def imported(self) -> bool:
        return self.outcome == "accepted"

    @property
    def needs_attention(self) -> bool:
        """Whether this row is one an operator still has to decide about."""

        return self.outcome in ATTENTION_OUTCOMES

    @property
    def unprocessed(self) -> bool:
        """Whether no result was ever recorded for this row."""

        return self.outcome == UNPROCESSED_OUTCOME

    @property
    def company_name_disagrees(self) -> bool:
        """Whether the file's Company name differs from the resolved Company.

        Both are shown whenever they differ. The supplied name is evidence about
        what the export claimed; it is never promoted over domain evidence, and
        collapsing the two would hide exactly the case the hierarchy exists for.
        """

        if not self.supplied_company_name or not self.company_label:
            return False
        supplied = self.supplied_company_name.strip().casefold()
        return supplied != self.company_label.strip().casefold()

    @property
    def diagnosis_href(self) -> str | None:
        if self.campaign_id is None or self.campaign_contact_id is None:
            return None
        return f"/admin/campaigns/{self.campaign_id}/contacts/{self.campaign_contact_id}"


@dataclass(frozen=True)
class ContactImportOriginView:
    """Why this Campaign Contact exists, when a file import is the answer.

    Absent (the reader returns ``None``) for every Contact acquired any other
    way. That is the load-bearing case: a Sales Navigator capture, an extension
    capture or a hand-entered Contact has no import origin, must not grow one,
    and must keep the ordinary discovery and verification path.
    """

    batch: ImportBatchRow
    row: ImportRowLineageRow
    resolved_company_id: uuid.UUID | None
    resolved_company_name: str | None
    resolved_company_domain: str | None
    alternates: tuple[ImportedAddressRow, ...]
    contact_identifiers: tuple[SourceIdentifierRow, ...]
    company_identifiers: tuple[SourceIdentifierRow, ...]

    @property
    def primary_address(self) -> ImportedAddressRow | None:
        return self.row.primary_address

    @property
    def email_bypassed_discovery(self) -> bool:
        """True only when an accepted imported address is what the stage used."""

        address = self.row.primary_address
        return bool(address and address.accepted)

    @property
    def verification_bypassed(self) -> bool:
        return self.email_bypassed_discovery

    @property
    def contact_created(self) -> bool:
        return self.row.contact_match_basis == "created"

    @property
    def matched_identifiers(self) -> tuple[SourceIdentifierRow, ...]:
        """The identifiers whose kind matches the basis the resolver recorded."""

        basis = self.row.contact_match_basis
        if not basis or basis == "created":
            return ()
        return tuple(row for row in self.contact_identifiers if row.kind == basis)


class ImportLineageReader:
    """Read-only projection of import lineage. Never writes, never resolves.

    Every query is bounded and keyed, and the batch-level readers fetch their
    Contacts and Companies in one statement each rather than per row, so a large
    batch costs a fixed number of queries.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- shared builders -----------------------------------------------------

    def _batch_row(self, batch: ImportBatch) -> ImportBatchRow:
        return ImportBatchRow(
            batch_id=batch.id,
            campaign_id=batch.campaign_id,
            filename=_safe(batch.filename),
            sanitized_filename=_safe(batch.sanitized_filename),
            source_schema=batch.source_schema,
            source_format=batch.source_format.value,
            sheet_name=_safe(batch.selected_sheet_name),
            sheet_index=batch.selected_sheet_index,
            status=batch.status.value,
            content_hash=batch.content_hash,
            uploaded_by=batch.uploaded_by,
            source_name=batch.source_name,
            created_at=batch.created_at,
            confirmed_at=batch.confirmed_at,
            completed_at=batch.completed_at,
            total_rows=batch.total_rows,
            accepted_rows=batch.accepted_rows,
            rejected_rows=batch.rejected_rows,
            duplicate_rows=batch.duplicate_rows,
            suppressed_rows=batch.suppressed_rows,
            ambiguous_rows=batch.ambiguous_rows,
            already_in_campaign_rows=batch.already_in_campaign_rows,
            contacts_created=batch.contacts_created,
            error_detail=_safe(batch.error_detail),
        )

    def _address_row(self, record: ImportedContactEmail) -> ImportedAddressRow:
        summary = campaign_import.imported_email_summary(record)
        return ImportedAddressRow(
            slot=summary["slot"],
            email=_safe(summary["email"]),
            raw_email=summary["raw_email"],
            accepted=record.email_stage_outcome
            is ImportedEmailStageOutcome.IMPORTED_EMAIL_ACCEPTED,
            rejection_code=record.rejection_code,
            email_stage_outcome=summary["vmr_email_stage_outcome"],
            verification_stage_outcome=summary["vmr_verification_stage_outcome"],
            provider_source=_safe(summary["provider_source"]),
            provider_claimed_status=_safe(summary["provider_claimed_status"]),
            provider_claimed_status_raw=_safe(summary["provider_claimed_status_raw"]),
            provider_claimed_verification_source=_safe(
                summary["provider_claimed_verification_source"]
            ),
            provider_claimed_catch_all=_safe(summary["provider_claimed_catch_all"]),
            provider_claimed_last_verified_at=record.provider_last_verified_at,
            provider_claimed_last_verified_raw=_safe(record.provider_last_verified_raw),
            source_row_number=record.source_row_number,
            source_sheet_name=_safe(record.source_sheet_name),
            source_file_checksum=record.source_file_checksum,
            source_schema=record.source_schema,
        )

    def _identifier_rows(
        self,
        *,
        contact_ids: Sequence[uuid.UUID] = (),
        company_ids: Sequence[uuid.UUID] = (),
    ) -> tuple[list[SourceIdentifierRow], list[SourceIdentifierRow]]:
        contact_rows: list[SourceIdentifierRow] = []
        company_rows: list[SourceIdentifierRow] = []
        if not contact_ids and not company_ids:
            return contact_rows, company_rows
        clauses: list[Any] = []
        if contact_ids:
            clauses.append(ImportSourceIdentifier.contact_id.in_(list(contact_ids)))
        if company_ids:
            clauses.append(ImportSourceIdentifier.company_id.in_(list(company_ids)))
        statement = select(ImportSourceIdentifier).where(or_(*clauses))
        for record in self._session.scalars(
            statement.order_by(
                ImportSourceIdentifier.system, ImportSourceIdentifier.identifier_kind
            )
        ).all():
            row = SourceIdentifierRow(
                system=_safe(record.system) or record.system,
                kind=record.identifier_kind,
                # Opaque vendor value: neutralized for display, never re-cased.
                value=_safe(record.identifier_value) or "",
                recorded_by=record.recorded_by,
                first_seen_batch_id=record.first_seen_batch_id,
                created_at=record.created_at,
            )
            if record.contact_id is not None:
                contact_rows.append(row)
            else:
                company_rows.append(row)
        return contact_rows, company_rows

    @staticmethod
    def _warnings(validation: ImportRowValidation | None) -> tuple[tuple[str, str], ...]:
        if validation is None:
            return ()
        entries: list[tuple[str, str]] = []
        for entry in validation.warnings or []:
            if isinstance(entry, dict):
                code = str(entry.get("code") or "")
                message = _safe(str(entry.get("message") or "")) or ""
                if code or message:
                    entries.append((code, message))
        return tuple(entries)

    @staticmethod
    def _supplied_company_name(validation: ImportRowValidation | None) -> str | None:
        if validation is None or not validation.normalized_data:
            return None
        raw = validation.normalized_data.get("company_name")
        return _safe(str(raw)) if isinstance(raw, str) and raw else None

    def _row_lineage(
        self,
        *,
        row: ImportRow,
        validation: ImportRowValidation | None,
        contact_label: str | None,
        company_label: str | None,
        primary: ImportedContactEmail | None,
        campaign_id: uuid.UUID | None,
    ) -> ImportRowLineageRow:
        return ImportRowLineageRow(
            row_id=row.id,
            row_number=row.row_number,
            sheet_name=_safe(row.sheet_name),
            outcome=validation.outcome.value if validation else UNPROCESSED_OUTCOME,
            error_code=validation.error_code if validation else None,
            note=_safe(validation.note) if validation else None,
            warnings=self._warnings(validation),
            row_fingerprint=validation.row_fingerprint if validation else None,
            contact_id=validation.contact_id if validation else None,
            contact_label=contact_label,
            contact_match_basis=validation.contact_match_basis if validation else None,
            company_id=validation.company_id if validation else None,
            company_label=company_label,
            company_match_basis=validation.company_match_basis if validation else None,
            supplied_company_name=self._supplied_company_name(validation),
            campaign_contact_id=validation.campaign_contact_id if validation else None,
            membership_action=validation.membership_action if validation else None,
            primary_address=self._address_row(primary) if primary is not None else None,
            campaign_id=campaign_id,
            batch_id=row.batch_id,
        )

    # -- campaign-level ------------------------------------------------------

    def campaign_batches(self, campaign_id: uuid.UUID) -> tuple[ImportBatchRow, ...]:
        """Every file import into one Campaign, newest first."""

        return tuple(
            self._batch_row(batch)
            for batch in campaign_import.campaign_batches(self._session, campaign_id)
        )

    # -- batch-level ---------------------------------------------------------

    def batch(self, batch_id: uuid.UUID) -> ImportBatchRow | None:
        batch = campaign_import.get_batch(self._session, batch_id)
        if batch is None or batch.source_schema is None:
            # A batch with no recognized schema belongs to the generic
            # contact-contract importer, which this surface does not describe.
            return None
        return self._batch_row(batch)

    def batch_rows(
        self, batch_id: uuid.UUID, *, limit: int = 100, offset: int = 0
    ) -> tuple[tuple[ImportRowLineageRow, ...], int]:
        """One page of a batch's rows and what each produced.

        Delegates the read to :func:`campaign_import.batch_rows`, which is the
        same helper the customer result screen uses, so the two screens can never
        disagree about what a row did.
        """

        batch = campaign_import.get_batch(self._session, batch_id)
        campaign_id = batch.campaign_id if batch is not None else None
        views, total = campaign_import.batch_rows(
            self._session, batch_id=batch_id, limit=limit, offset=offset
        )
        rows = tuple(
            self._row_lineage(
                row=view.row,
                validation=view.validation,
                contact_label=_contact_label(view.contact),
                company_label=_safe(view.company.name) if view.company else None,
                primary=view.imported_email,
                campaign_id=campaign_id,
            )
            for view in views
        )
        return rows, total

    # -- contact-level -------------------------------------------------------

    def contact_origin(
        self, *, campaign_id: uuid.UUID, campaign_contact_id: uuid.UUID
    ) -> ContactImportOriginView | None:
        """The import that produced one Campaign Contact, if one did.

        ``None`` — the common case — means this membership did not come from a
        file import. Callers must treat that as "ordinary acquisition", never as
        "lineage unavailable": the absence is a fact, not a gap.
        """

        validation = self._session.scalars(
            select(ImportRowValidation)
            .where(ImportRowValidation.campaign_contact_id == campaign_contact_id)
            .order_by(ImportRowValidation.created_at.desc())
            .limit(1)
        ).first()
        if validation is None:
            return None
        row = self._session.get(ImportRow, validation.import_row_id)
        if row is None:  # pragma: no cover - FK makes this unreachable
            return None
        batch = self._session.get(ImportBatch, row.batch_id)
        if batch is None or batch.source_schema is None:
            return None

        contact_id = validation.contact_id
        primary = (
            self._session.get(ImportedContactEmail, validation.imported_email_id)
            if validation.imported_email_id
            else None
        )
        alternates = (
            campaign_import.retained_alternates(
                self._session, campaign_id=campaign_id, contact_id=contact_id
            )
            if contact_id
            else []
        )
        company = (
            self._session.get(Company, validation.company_id) if validation.company_id else None
        )
        contact = self._session.get(Contact, contact_id) if contact_id else None

        contact_identifiers, company_identifiers = self._identifier_rows(
            contact_ids=[contact_id] if contact_id else [],
            company_ids=[company.id] if company else [],
        )

        return ContactImportOriginView(
            batch=self._batch_row(batch),
            row=self._row_lineage(
                row=row,
                validation=validation,
                contact_label=_contact_label(contact),
                company_label=_safe(company.name) if company else None,
                primary=primary,
                campaign_id=campaign_id,
            ),
            resolved_company_id=company.id if company else None,
            resolved_company_name=_safe(company.name) if company else None,
            resolved_company_domain=_safe(company.domain) if company else None,
            alternates=tuple(self._address_row(record) for record in alternates),
            contact_identifiers=tuple(contact_identifiers),
            company_identifiers=tuple(company_identifiers),
        )

    def contact_identifiers(self, contact_id: uuid.UUID) -> tuple[SourceIdentifierRow, ...]:
        """Source identifiers held for one permanent Contact, across campaigns."""

        contact_rows, _ = self._identifier_rows(contact_ids=[contact_id])
        return tuple(contact_rows)

    def company_identifiers(self, company_id: uuid.UUID) -> tuple[SourceIdentifierRow, ...]:
        _, company_rows = self._identifier_rows(company_ids=[company_id])
        return tuple(company_rows)

    # -- failures and held rows ---------------------------------------------

    def unresolved_rows(
        self, *, campaign_id: uuid.UUID | None = None, limit: int = 200
    ) -> tuple[ImportRowLineageRow, ...]:
        """Rows a file import refused, held or suppressed, newest batch first.

        Held and refused rows are exactly the ones an operator has to find, and
        they are invisible from every Phase 2 surface: a row that never became a
        Campaign Contact has no stage, no Agent Job and no failure to inherit.
        """

        statement = (
            select(ImportRowValidation, ImportRow, ImportBatch)
            .join(ImportRow, ImportRow.id == ImportRowValidation.import_row_id)
            .join(ImportBatch, ImportBatch.id == ImportRow.batch_id)
            .where(
                ImportBatch.source_schema.is_not(None),
                # By outcome, not by error_code. The 200-row cap below is applied
                # AFTER this predicate, so a large re-import of already-present
                # rows can no longer evict the genuinely refused ones from the
                # page by filling it first.
                ImportRowValidation.outcome.in_(sorted(ATTENTION_OUTCOMES)),
            )
            .order_by(ImportBatch.created_at.desc(), ImportRow.row_number)
            .limit(limit)
        )
        if campaign_id is not None:
            statement = statement.where(ImportBatch.campaign_id == campaign_id)

        records = list(self._session.execute(statement).all())
        if not records:
            return ()

        # One query each for the addresses and the companies these rows name,
        # rather than one per row.
        row_ids = [row.id for _, row, _ in records]
        addresses = {
            record.import_row_id: record
            for record in self._session.scalars(
                select(ImportedContactEmail).where(
                    ImportedContactEmail.import_row_id.in_(row_ids),
                    ImportedContactEmail.slot == ImportedEmailSlot.PRIMARY,
                )
            ).all()
        }
        company_ids = [v.company_id for v, _, _ in records if v.company_id]
        companies = _by_id(
            self._session.scalars(select(Company).where(Company.id.in_(company_ids))).all()
            if company_ids
            else []
        )

        return tuple(
            self._row_lineage(
                row=row,
                validation=validation,
                contact_label=None,
                company_label=(
                    _safe(companies[validation.company_id].name)
                    if validation.company_id in companies
                    else None
                ),
                primary=addresses.get(row.id),
                campaign_id=batch.campaign_id,
            )
            for validation, row, batch in records
        )


def _contact_label(contact: Any) -> str | None:
    if contact is None:
        return None
    name = " ".join(part for part in (contact.first_name, contact.last_name) if part).strip()
    return _safe(name or contact.email or str(contact.id))


def _by_id(records: Iterable[Any]) -> dict[uuid.UUID, Any]:
    return {record.id: record for record in records}
