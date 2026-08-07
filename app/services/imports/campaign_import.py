"""Campaign-bound contact file import (IMP-001).

The operator opens a Campaign, uploads an Apollo-style export, looks at exactly
what will happen, and confirms. This module is everything between those two
moments.

It is built on the existing staged-import machinery rather than beside it.
:mod:`app.services.imports.parsing` already renders CSV and XLSX into one neutral
row shape; :class:`~app.models.import_batch.ImportBatch` is already Campaign-bound
and already keeps immutable raw rows with one outcome each;
:func:`~app.services.campaign_contacts.enrol_contact` is already the authoritative
way a person joins a Campaign. None of that is re-implemented here. What is new
is the reading of a vendor schema (:mod:`app.services.imports.apollo`), the
resolution of a permanent Company and Contact from it, and the one thing this
path exists for: an address that arrives already known.

**The imported address is carried, never discovered.** No candidate format is
generated, no domain pattern is applied, and no verification provider is called —
not MillionVerifier, not ZeroBounce, not any other. The address is recorded as
what it is, an operator-supplied claim, alongside whatever the vendor said about
it, and the Email and Verification stages complete through two explicit outcomes
that say so: ``imported_email_accepted`` and
``verification_bypassed_imported_email``. Neither is a deliverability claim, and
no import ever writes a row into the table that holds real verification evidence.

**Preview writes nothing.** :func:`preview` and :func:`confirm` share one
decision function, :func:`plan_row`, which performs read-only lookups and returns
what would happen. Confirmation is the first durable mutation, and because the
commit path has no decision logic of its own, the preview cannot promise
something the confirm then does differently.

**One bad row is one bad row.** Every row is committed inside its own SAVEPOINT,
so a database failure on row 40 rolls back row 40 alone — never leaving a Contact
without its Company, or a membership without its person — and rows 1 to 39 stand.
"""

from __future__ import annotations

import enum
import hashlib
import json
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.campaign import Campaign, CampaignContact
from app.models.company import Company
from app.models.contact import Contact
from app.models.enums import (
    AgentIdentifier,
    ImportBatchStatus,
    ImportedEmailSlot,
    ImportedEmailStageOutcome,
    ImportedVerificationOutcome,
    ImportRowOutcome,
    ImportSourceFormat,
)
from app.models.import_batch import ImportBatch, ImportRow, ImportRowError, ImportRowValidation
from app.models.imported_email import ImportedContactEmail
from app.models.provenance import ProvenanceRecord
from app.services.audit import record_audit_event
from app.services.campaign_contacts import enrol_contact
from app.services.imports import apollo, company_resolution, contact_resolution, parsing
from app.services.provenance.service import record_import_observations
from app.services.suppressions import find_active_suppression

#: The actor recorded on everything this path writes.
IMPORT_ACTOR = "campaign-file-import"

#: Hard structural ceilings, checked before any row is stored. A spreadsheet
#: within the byte limit can still expand to something absurd — a compressed
#: workbook is a zip, and a million empty-but-styled rows costs almost nothing to
#: encode. These bound the expansion rather than the download.
MAX_DATA_ROWS = 50_000
MAX_COLUMNS = 512

#: Filenames arrive from an operator's disk and are stored and displayed. Path
#: separators, traversal segments and control characters are removed rather than
#: rejected: the original is kept verbatim on the batch, and this is the version
#: anything else is allowed to use.
_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._ \-()]+")


#: The widest address ``imported_contact_emails.normalized_email`` can hold, and
#: the practical RFC ceiling. Enforced in application code so a row that exceeds
#: it is refused with a reason rather than by the database with a driver error.
MAX_EMAIL_CHARS = 320
#: Column widths for the vendor's own claim fields. Truncated rather than
#: refused: a claim we could not store whole is still worth storing, and it never
#: participates in identity.
MAX_PROVIDER_TEXT_CHARS = 255
MAX_PROVIDER_TOKEN_CHARS = 128
_TOKEN = MAX_PROVIDER_TOKEN_CHARS

#: Recorded on a batch whose header carries the columns only a genuine Apollo
#: export tends to have.
APOLLO_SOURCE_NAME = "Apollo contact export"
#: Recorded on a file that satisfies the same schema without evidencing that
#: Apollo produced it.
APOLLO_COMPATIBLE_SOURCE_NAME = "Apollo-compatible contact file"


class CampaignImportError(Exception):
    """A safe, operator-facing failure of the campaign file import."""


class FeatureDisabledError(CampaignImportError):
    """Raised when the import is attempted while its feature switch is off."""


class CampaignNotFound(CampaignImportError):
    """Raised when the target Campaign does not exist."""


class UnreadableFileError(CampaignImportError):
    """Raised when the upload cannot be read, or is not a recognized schema."""


def source_name_for(detection: apollo.SchemaDetection) -> str:
    """What to record as the batch's source, without inventing a vendor.

    A file is recognized on four required headers, and any hand-made CSV can
    carry those. Writing "Apollo contact export" onto every recognized file
    manufactured vendor provenance out of a column list — the same failure class
    as writing a spreadsheet cell into a verification table, and durable, since
    it is stored on the batch and read back on every operator surface.

    ``is_apollo_export`` already distinguishes the two: it counts headers only a
    genuine export tends to carry. It just was not consulted anywhere.
    """

    if detection.is_apollo_export:
        return APOLLO_SOURCE_NAME
    return APOLLO_COMPATIBLE_SOURCE_NAME


def sanitize_filename(filename: str | None) -> str:
    """Reduce an uploaded filename to something safe to store and display."""

    if not filename:
        return "upload"
    # Take the last path segment under BOTH separator conventions: a Windows
    # client sends backslashes and a POSIX one sends slashes, and a name
    # containing the other separator would otherwise survive as a path.
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = _UNSAFE_FILENAME.sub("_", base).strip(". ")
    return (cleaned or "upload")[:255]


def _require_feature() -> None:
    if not get_settings().features.csv_import:
        raise FeatureDisabledError(
            "Contact file import is disabled. Set FEATURES__CSV_IMPORT=true and restart "
            "the application to enable it."
        )


def file_checksum(content: bytes) -> str:
    """SHA-256 of the uploaded bytes, exactly as received."""

    return hashlib.sha256(content).hexdigest()


def batch_content_hash(content: bytes, *, sheet_index: int, schema_id: str) -> str:
    """Identify one *interpretation* of one file for idempotent confirmation.

    The same bytes confirmed against a different worksheet are a different
    import, so the sheet participates. Re-confirming the same bytes and the same
    sheet into the same Campaign returns the batch that already exists rather
    than importing twice.
    """

    digest = hashlib.sha256(content)
    digest.update(f"|sheet={sheet_index}|schema={schema_id}".encode())
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Inspection: what does this file contain?
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SheetOption:
    """One worksheet the operator could import from."""

    index: int
    name: str | None
    header: tuple[str, ...]
    data_row_count: int
    detection: apollo.SchemaDetection

    @property
    def importable(self) -> bool:
        return self.detection.recognized and self.data_row_count > 0


@dataclass(frozen=True)
class FileInspection:
    """A parsed upload, before anything has been decided about its rows."""

    source_format: str
    parser_version: str
    checksum: str
    sanitized_filename: str
    sheets: tuple[SheetOption, ...]

    @property
    def importable_sheets(self) -> tuple[SheetOption, ...]:
        return tuple(sheet for sheet in self.sheets if sheet.importable)

    @property
    def needs_sheet_choice(self) -> bool:
        return len(self.importable_sheets) > 1

    def sheet(self, index: int | None) -> SheetOption | None:
        """The sheet to work with: the chosen one, or the best default.

        With no explicit choice, the first importable sheet wins. When none is
        importable the FIRST sheet is returned anyway, deliberately: it carries
        the detection that explains *why* nothing is importable, and an operator
        told "no worksheet is recognizable" has been told far less than one told
        "this file has no Email column".
        """

        if index is None:
            candidates = self.importable_sheets or self.sheets
            return candidates[0] if candidates else None
        return next((sheet for sheet in self.sheets if sheet.index == index), None)


def inspect(content: bytes, filename: str | None) -> FileInspection:
    """Parse an upload and report every worksheet with what it was recognized as.

    Raises :class:`UnreadableFileError` for anything that is not a readable
    ``.csv`` or ``.xlsx``, is empty, or exceeds the structural ceilings. The
    message is always actionable and never contains a stack trace or a path.
    """

    _require_feature()
    try:
        parsed = parsing.parse_file(content, filename)
    except (parsing.UnsupportedFormatError, parsing.MalformedFileError) as exc:
        raise UnreadableFileError(str(exc)) from exc

    if len(parsed.rows) > MAX_DATA_ROWS:
        raise UnreadableFileError(
            f"This file has {len(parsed.rows):,} data rows, which is more than the "
            f"{MAX_DATA_ROWS:,}-row limit for one import. Split it into smaller files "
            "and import them one at a time."
        )
    for sheet in parsed.sheets:
        # Physical positions, not the filtered display header. Blank columns are
        # columns: they cost a Python object on every row and they were how a
        # 516-column file passed a 512-column limit. The parser now refuses this
        # at the header row as well; this check remains so the operator gets the
        # import's own wording rather than the parser's.
        physical = len(sheet.columns) or len(sheet.header)
        if physical > MAX_COLUMNS:
            where = f"Sheet {sheet.name!r}" if sheet.name else "The file"
            raise UnreadableFileError(
                f"{where} has {physical:,} columns, which is more than the "
                f"{MAX_COLUMNS:,}-column limit. Blank and unnamed columns count: delete "
                "the unused columns to the right of your data and try again."
            )

    sheets = tuple(
        SheetOption(
            index=sheet.index,
            name=sheet.name,
            header=tuple(sheet.header),
            data_row_count=sheet.data_row_count,
            detection=apollo.detect_schema(sheet.columns or tuple(sheet.header)),
        )
        for sheet in parsed.sheets
    )
    return FileInspection(
        source_format=parsed.source_format,
        parser_version=parsed.parser_version,
        checksum=file_checksum(content),
        sanitized_filename=sanitize_filename(filename),
        sheets=sheets,
    )


def _parsed_rows(content: bytes, filename: str | None, sheet_index: int) -> list[parsing.ParsedRow]:
    parsed = parsing.parse_file(content, filename)
    return parsed.rows_for_sheets([sheet_index])


# ---------------------------------------------------------------------------
# Per-row planning: one decision function, shared by preview and confirm
# ---------------------------------------------------------------------------


class RowDisposition(enum.StrEnum):
    """What confirming one row would do, in the operator's vocabulary.

    Richer than the durable :class:`~app.models.enums.ImportRowOutcome` on
    purpose, and mapped onto it by :func:`durable_outcome`. "This person is
    already in this campaign" and "this file lists this person twice" both store
    as ``duplicate`` because neither creates anything, but they call for
    completely different operator reactions and a single count would hide that.
    """

    IMPORTED = "imported"
    MATCHED_EXISTING = "matched_existing"
    ALREADY_IN_CAMPAIGN = "already_in_campaign"
    SKIPPED_DUPLICATE = "skipped_duplicate"
    REVIEW_REQUIRED = "review_required"
    SUPPRESSED = "suppressed"
    FAILED = "failed"


_DURABLE_OUTCOME: dict[RowDisposition, ImportRowOutcome] = {
    RowDisposition.IMPORTED: ImportRowOutcome.ACCEPTED,
    RowDisposition.MATCHED_EXISTING: ImportRowOutcome.DUPLICATE,
    RowDisposition.ALREADY_IN_CAMPAIGN: ImportRowOutcome.DUPLICATE,
    RowDisposition.SKIPPED_DUPLICATE: ImportRowOutcome.DUPLICATE,
    RowDisposition.REVIEW_REQUIRED: ImportRowOutcome.AMBIGUOUS,
    RowDisposition.SUPPRESSED: ImportRowOutcome.SUPPRESSED,
    RowDisposition.FAILED: ImportRowOutcome.REJECTED,
}


def durable_outcome(disposition: RowDisposition) -> ImportRowOutcome:
    return _DURABLE_OUTCOME[disposition]


@dataclass
class RowPlan:
    """Everything decided about one row, without a single write."""

    row_number: int
    sheet_name: str | None
    apollo_row: apollo.ApolloRow
    fingerprint: str
    disposition: RowDisposition
    company: company_resolution.CompanyPlan | None = None
    contact: contact_resolution.ContactPlan | None = None
    #: The address this row would make the Campaign's address for the person.
    accepted_email: str | None = None
    error_code: str | None = None
    error_detail: str | None = None
    warnings: list[tuple[str, str]] = field(default_factory=list)
    #: The membership that already exists, when one does.
    existing_membership_id: uuid.UUID | None = None
    #: Evidence from a previous import of this exact row into this Campaign.
    prior_evidence_id: uuid.UUID | None = None

    @property
    def creates_contact(self) -> bool:
        return (
            self.disposition is RowDisposition.IMPORTED
            and self.contact is not None
            and self.contact.action is contact_resolution.ContactAction.CREATE
        )

    @property
    def expected_pipeline(self) -> str:
        """One sentence naming what will happen to this person after import."""

        if self.disposition in {
            RowDisposition.REVIEW_REQUIRED,
            RowDisposition.FAILED,
        }:
            return "Nothing. The row is not imported."
        if self.disposition is RowDisposition.SUPPRESSED:
            return "Nothing. The suppression ledger blocks this identity."
        if self.disposition in {
            RowDisposition.ALREADY_IN_CAMPAIGN,
            RowDisposition.SKIPPED_DUPLICATE,
        }:
            return "Nothing new. The existing Campaign Contact keeps its current progress."
        return (
            "Enrolled, then Identity, Company and Research run normally. The Email stage "
            "completes as imported_email_accepted without generating a candidate, "
            "Verification completes as verification_bypassed_imported_email without "
            "calling a provider, and Insights and Personalization follow their existing "
            "prerequisites. Sending remains unavailable."
        )


@dataclass
class _Registers:
    """In-memory state simulating rows this same file already contributed.

    Every identity signal the resolver matches on is registered, not just the
    fingerprint. A preview holds no transaction and creates nothing, so without
    these a second row naming the same person by a *different* signal would be
    predicted as a fresh import and then behave completely differently at commit
    — where the first row has by then written the identifier the second one
    matches. The registers are what keep the preview's promise true.
    """

    fingerprints: set[str] = field(default_factory=set)
    emails: set[str] = field(default_factory=set)
    apollo_contact_ids: set[str] = field(default_factory=set)
    linkedin_identities: set[str] = field(default_factory=set)


def _existing_evidence(
    session: Session, *, campaign_id: uuid.UUID, fingerprint: str
) -> ImportedContactEmail | None:
    """An ACCEPTED address already imported from this exact row content.

    Deliberately restricted to accepted evidence. A row that was previously held
    for review also left a record here, and treating that as "already imported"
    would make the refusal permanent: the operator fixes the file, re-imports,
    and is told the row was already done. Only an address this Campaign actually
    took makes a repeat a duplicate.
    """

    return session.scalars(
        select(ImportedContactEmail).where(
            ImportedContactEmail.campaign_id == campaign_id,
            ImportedContactEmail.row_fingerprint == fingerprint,
            ImportedContactEmail.slot == ImportedEmailSlot.PRIMARY,
            ImportedContactEmail.email_stage_outcome
            == ImportedEmailStageOutcome.IMPORTED_EMAIL_ACCEPTED,
        )
    ).first()


def _existing_restatement(
    session: Session, *, campaign_id: uuid.UUID, fingerprint: str
) -> ImportedContactEmail | None:
    """A correction from this exact row content that is already on record.

    Separate from :func:`_existing_evidence` because the two answer different
    questions, and conflating them was how a re-upload of a corrected file
    reached enrolment a second time with an idempotency key it had already used
    for a different batch.

    Restricted to RESTATED evidence. A row that was HELD or whose address was
    REJECTED must stay re-importable — that is the whole reason the accepted
    lookup is narrow — but a correction that has already been retained is simply
    a repeat, and uploading it twice must add nothing.
    """

    return session.scalars(
        select(ImportedContactEmail).where(
            ImportedContactEmail.campaign_id == campaign_id,
            ImportedContactEmail.row_fingerprint == fingerprint,
            ImportedContactEmail.slot == ImportedEmailSlot.PRIMARY,
            ImportedContactEmail.email_stage_outcome.is_(None),
            ImportedContactEmail.rejection_code == RESTATED_CODE,
        )
    ).first()


def _membership(
    session: Session, *, campaign_id: uuid.UUID, contact_id: uuid.UUID
) -> CampaignContact | None:
    return session.scalars(
        select(CampaignContact).where(
            CampaignContact.campaign_id == campaign_id,
            CampaignContact.contact_id == contact_id,
        )
    ).first()


def plan_row(
    session: Session,
    *,
    campaign_id: uuid.UUID,
    raw: dict[str, str],
    detection: apollo.SchemaDetection,
    row_number: int,
    sheet_index: int,
    sheet_name: str | None,
    registers: _Registers,
    cells: Sequence[str] = (),
) -> RowPlan:
    """Decide one row's fate using read-only lookups only.

    The order of the checks is the order in which a reason *stops mattering*.
    A row already imported into this Campaign needs no identity work at all; a
    suppressed identity must be refused before anything is resolved for it; and
    identity is settled before enrolment because enrolment needs a person.
    """

    row = apollo.read_row(
        raw,
        detection,
        row_number=row_number,
        sheet_index=sheet_index,
        sheet_name=sheet_name,
        cells=cells,
    )
    fingerprint = apollo.row_fingerprint(row)
    plan = RowPlan(
        row_number=row_number,
        sheet_name=sheet_name,
        apollo_row=row,
        fingerprint=fingerprint,
        disposition=RowDisposition.IMPORTED,
    )
    plan.warnings.extend(row.warnings)

    # 1. The same row content, twice in this one file.
    if fingerprint in registers.fingerprints:
        plan.disposition = RowDisposition.SKIPPED_DUPLICATE
        plan.error_code = "duplicate_row_in_file"
        plan.error_detail = "An earlier row in this file states the same person identically."
        return plan

    # 2. The same row content, already imported into this Campaign before.
    prior = _existing_evidence(session, campaign_id=campaign_id, fingerprint=fingerprint)
    if prior is not None:
        plan.prior_evidence_id = prior.id
        plan.disposition = RowDisposition.SKIPPED_DUPLICATE
        plan.error_code = "already_imported"
        plan.error_detail = (
            "This exact row was already imported into this Campaign by an earlier batch."
        )
        registers.fingerprints.add(fingerprint)
        return plan

    # 2b. The same row content already recorded as a correction. Uploading a
    #     corrected file twice must add nothing — including no second trip
    #     through enrolment, which would reuse this row's idempotency key.
    restated = _existing_restatement(session, campaign_id=campaign_id, fingerprint=fingerprint)
    if restated is not None:
        plan.prior_evidence_id = restated.id
        plan.disposition = RowDisposition.SKIPPED_DUPLICATE
        plan.error_code = "already_recorded_as_a_correction"
        plan.error_detail = (
            "This exact row was already recorded as a correction to an address this "
            "Campaign already holds for the person."
        )
        registers.fingerprints.add(fingerprint)
        return plan

    # 3. A usable person, before anything is resolved for them.
    if not row.has_person_identity:
        plan.disposition = RowDisposition.FAILED
        plan.error_code = "person_identity_missing"
        plan.error_detail = (
            "The row does not name a person: both First Name and Last Name are required."
        )
        return plan

    primary = row.primary
    if primary is None:
        plan.disposition = RowDisposition.FAILED
        plan.error_code = "email_missing"
        plan.error_detail = (
            "The row has no Email. This import path carries a supplied address; a row "
            "without one cannot use it."
        )
        return plan
    if not primary.is_valid_syntax:
        plan.disposition = RowDisposition.FAILED
        plan.error_code = "email_malformed"
        plan.error_detail = f"The Email value {primary.raw!r} is not a valid address."
        return plan
    if primary.normalized is not None and len(primary.normalized) > MAX_EMAIL_CHARS:
        # Checked here rather than left to the column width. The per-row
        # savepoint meant an over-long address never took the batch down, but the
        # operator was shown "database_error", which says nothing about what to
        # fix. An address is refused for a reason the operator can act on.
        plan.disposition = RowDisposition.FAILED
        plan.error_code = "email_too_long"
        plan.error_detail = (
            f"The Email value is {len(primary.normalized):,} characters, which is longer "
            f"than the {MAX_EMAIL_CHARS}-character maximum for an address."
        )
        return plan

    normalized_email = primary.normalized
    assert normalized_email is not None

    # 4. Suppression is authoritative and is asked before any identity is created.
    suppression = find_active_suppression(
        session, email=normalized_email, domain=row.website_domain or primary.domain
    )
    if suppression is not None:
        plan.disposition = RowDisposition.SUPPRESSED
        plan.error_code = "suppressed"
        plan.error_detail = (
            f"The suppression ledger blocks this identity "
            f"({suppression.suppression_type.value}: {suppression.reason.value})."
        )
        return plan

    # 5. The same PERSON, twice in this one file, stated differently each time.
    #    The first occurrence wins — file order, so the answer is the same on
    #    every run — and the later one is reported rather than silently dropped.
    same_person: str | None = None
    if normalized_email in registers.emails:
        same_person = "duplicate_email_in_file"
    elif row.apollo_contact_id and row.apollo_contact_id in registers.apollo_contact_ids:
        same_person = "duplicate_apollo_contact_in_file"
    elif row.person_linkedin_identity and row.person_linkedin_identity in (
        registers.linkedin_identities
    ):
        same_person = "duplicate_linkedin_profile_in_file"
    if same_person is not None:
        plan.disposition = RowDisposition.SKIPPED_DUPLICATE
        plan.error_code = same_person
        plan.error_detail = (
            "An earlier row in this file already names this person. The first row was "
            "used; this one was not imported."
        )
        registers.fingerprints.add(fingerprint)
        return plan

    # 6. Company, then Contact. Company first because the Contact's dedup
    #    fingerprint is built from the domain the Company settled on.
    plan.company = company_resolution.plan(session, row)
    plan.warnings.extend(plan.company.warnings)
    if plan.company.needs_review:
        plan.disposition = RowDisposition.REVIEW_REQUIRED
        plan.error_code = plan.company.review_code
        plan.error_detail = plan.company.review_detail
        return plan

    plan.contact = contact_resolution.plan(session, row, company_domain=plan.company.domain)
    plan.warnings.extend(plan.contact.warnings)
    if plan.contact.needs_review:
        plan.disposition = RowDisposition.REVIEW_REQUIRED
        plan.error_code = plan.contact.review_code
        plan.error_detail = plan.contact.review_detail
        return plan

    plan.accepted_email = normalized_email
    registers.fingerprints.add(fingerprint)
    registers.emails.add(normalized_email)
    if row.apollo_contact_id:
        registers.apollo_contact_ids.add(row.apollo_contact_id)
    if row.person_linkedin_identity:
        registers.linkedin_identities.add(row.person_linkedin_identity)

    # 7. Is this person already in this Campaign?
    if plan.contact.action is contact_resolution.ContactAction.MATCH_EXISTING:
        assert plan.contact.contact_id is not None
        existing = _membership(session, campaign_id=campaign_id, contact_id=plan.contact.contact_id)
        if existing is not None:
            plan.existing_membership_id = existing.id
            plan.disposition = RowDisposition.ALREADY_IN_CAMPAIGN
            return plan
        plan.disposition = RowDisposition.MATCHED_EXISTING
        return plan

    plan.disposition = RowDisposition.IMPORTED
    return plan


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DuplicateFileNote:
    """What an identical upload was previously used for."""

    #: ``already_imported`` | ``imported_into_another_campaign``
    code: str
    message: str
    batch_id: uuid.UUID | None = None


@dataclass
class ImportPreview:
    """Everything the preview screen shows. Produced without a single write."""

    campaign_id: uuid.UUID
    schema_id: str | None = None
    is_apollo_export: bool = False
    sheet_index: int = 0
    sheet_name: str | None = None
    headers: tuple[str, ...] = ()
    recognized_columns: dict[str, str] = field(default_factory=dict)
    unmapped_columns: tuple[str, ...] = ()
    duplicate_columns: tuple[tuple[str, str], ...] = ()
    total_rows: int = 0
    rows: list[RowPlan] = field(default_factory=list)
    structure_error: str | None = None
    duplicate_file: DuplicateFileNote | None = None
    checksum: str = ""

    def count(self, disposition: RowDisposition) -> int:
        return sum(1 for row in self.rows if row.disposition is disposition)

    @property
    def counts(self) -> dict[str, int]:
        return {disposition.value: self.count(disposition) for disposition in RowDisposition}

    @property
    def warning_rows(self) -> int:
        return sum(1 for row in self.rows if row.warnings)

    @property
    def valid_rows(self) -> int:
        return self.count(RowDisposition.IMPORTED) + self.count(RowDisposition.MATCHED_EXISTING)

    @property
    def invalid_rows(self) -> int:
        return self.count(RowDisposition.FAILED)

    @property
    def is_importable(self) -> bool:
        return self.structure_error is None and self.total_rows > 0


def _duplicate_file_note(
    session: Session, *, campaign_id: uuid.UUID, checksum: str, content_hash: str
) -> DuplicateFileNote | None:
    """Say plainly whether these exact bytes have been imported before.

    A duplicate upload must never simply vanish into "0 rows imported": the
    operator needs to know whether it was already done here, done somewhere else,
    or merely resembles something done before.
    """

    same_interpretation = session.scalars(
        select(ImportBatch).where(
            ImportBatch.campaign_id == campaign_id,
            ImportBatch.content_hash == content_hash,
            ImportBatch.status == ImportBatchStatus.COMPLETED,
        )
    ).first()
    if same_interpretation is not None:
        return DuplicateFileNote(
            code="already_imported",
            message=(
                "This exact file and worksheet were already imported into this Campaign. "
                "Confirming again will show the existing batch instead of importing twice."
            ),
            batch_id=same_interpretation.id,
        )

    # The same bytes, in a different Campaign. Matched on the file checksum
    # rather than the batch's content hash, because the content hash folds in the
    # worksheet and would miss the same file imported from a different sheet.
    other = session.execute(
        select(ImportBatch, Campaign)
        .join(Campaign, Campaign.id == ImportBatch.campaign_id)
        .join(
            ImportedContactEmail,
            ImportedContactEmail.import_batch_id == ImportBatch.id,
        )
        .where(
            ImportBatch.campaign_id != campaign_id,
            ImportedContactEmail.source_file_checksum == checksum,
        )
        .limit(1)
    ).first()
    if other is not None:
        batch, _campaign = other
        return DuplicateFileNote(
            code="imported_into_another_campaign",
            message=(
                "This exact file was already imported into another Campaign. Importing it "
                "here as well is allowed — the same person may belong to more than one "
                "Campaign — and will not duplicate any Contact or Company."
            ),
            # Deliberately no campaign_id and no campaign name. This is the one
            # query in the flow that deliberately crosses the Campaign boundary,
            # and telling the uploader which other Campaign holds the same file
            # discloses a name they may have no other route to. That the file was
            # seen before is the useful part; whose it was is not.
            batch_id=None,
        )
    return None


def preview(
    session: Session,
    *,
    campaign_id: uuid.UUID,
    content: bytes,
    filename: str | None,
    sheet_index: int | None = None,
) -> ImportPreview:
    """Predict the whole import. Performs no writes of any kind."""

    _require_feature()
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise CampaignNotFound(f"campaign {campaign_id} does not exist")

    inspection = inspect(content, filename)
    result = ImportPreview(campaign_id=campaign_id, checksum=inspection.checksum)

    sheet = inspection.sheet(sheet_index)
    if sheet is None:
        result.structure_error = (
            "No worksheet in this file carries a recognizable contact header row."
            if sheet_index is None
            else "The selected worksheet does not exist in this file."
        )
        return result

    result.sheet_index = sheet.index
    result.sheet_name = sheet.name
    result.headers = sheet.header
    result.recognized_columns = dict(sheet.detection.field_columns)
    result.unmapped_columns = sheet.detection.unmapped_columns
    result.duplicate_columns = sheet.detection.duplicate_columns
    result.schema_id = sheet.detection.schema_id
    result.is_apollo_export = sheet.detection.is_apollo_export

    if not sheet.detection.recognized:
        result.structure_error = apollo.missing_header_message(sheet.detection)
        return result
    if sheet.data_row_count == 0:
        where = f"Worksheet “{sheet.name}”" if sheet.name else "This file"
        result.structure_error = f"{where} has a valid header row but no data rows."
        return result

    assert result.schema_id is not None
    result.duplicate_file = _duplicate_file_note(
        session,
        campaign_id=campaign_id,
        checksum=inspection.checksum,
        content_hash=batch_content_hash(
            content, sheet_index=sheet.index, schema_id=result.schema_id
        ),
    )

    rows = _parsed_rows(content, filename, sheet.index)
    result.total_rows = len(rows)
    registers = _Registers()
    for parsed_row in rows:
        result.rows.append(
            plan_row(
                session,
                campaign_id=campaign_id,
                raw=parsed_row.raw,
                cells=parsed_row.cells,
                detection=sheet.detection,
                row_number=parsed_row.row_number,
                sheet_index=parsed_row.sheet_index,
                sheet_name=parsed_row.sheet_name,
                registers=registers,
            )
        )
    return result


# ---------------------------------------------------------------------------
# Confirmation: the first durable mutation
# ---------------------------------------------------------------------------


@dataclass
class ImportResult:
    """Truthful counts for one confirmed batch."""

    batch_id: uuid.UUID
    status: ImportBatchStatus
    total_rows: int = 0
    imported: int = 0
    matched_existing: int = 0
    already_in_campaign: int = 0
    skipped_duplicate: int = 0
    review_required: int = 0
    suppressed: int = 0
    failed: int = 0
    contacts_created: int = 0
    companies_created: int = 0
    reused_existing_batch: bool = False
    error_detail: str | None = None


class _Tally:
    def __init__(self) -> None:
        self.by_disposition: dict[RowDisposition, int] = dict.fromkeys(RowDisposition, 0)
        self.contacts_created = 0
        self.companies_created = 0

    def record(self, disposition: RowDisposition) -> None:
        self.by_disposition[disposition] += 1


def _result_from_batch(
    session: Session, batch: ImportBatch, *, reused: bool = False
) -> ImportResult:
    """Rebuild a batch's counts from what it actually wrote.

    ``duplicate_rows`` is one durable column covering three different things —
    matched an existing contact, already in this campaign, and a repeated row —
    and splitting it back by arithmetic would guess. The per-row outcomes are on
    record, so they are read instead: a returned batch reports the same numbers
    it reported when it was created.
    """

    rows = list(
        session.scalars(
            select(ImportRowValidation)
            .join(ImportRow, ImportRow.id == ImportRowValidation.import_row_id)
            .where(ImportRow.batch_id == batch.id)
        ).all()
    )
    skipped = sum(1 for row in rows if row.membership_action is None and row.error_code is not None)
    already = sum(1 for row in rows if row.membership_action == "existing")
    matched = sum(
        1
        for row in rows
        if row.membership_action == "created" and row.outcome is ImportRowOutcome.DUPLICATE
    )
    return ImportResult(
        batch_id=batch.id,
        status=batch.status,
        total_rows=batch.total_rows,
        imported=batch.accepted_rows,
        matched_existing=matched,
        already_in_campaign=already,
        skipped_duplicate=max(
            skipped - batch.ambiguous_rows - batch.suppressed_rows - batch.rejected_rows, 0
        ),
        review_required=batch.ambiguous_rows,
        suppressed=batch.suppressed_rows,
        failed=batch.rejected_rows,
        contacts_created=batch.contacts_created,
        reused_existing_batch=reused,
        error_detail=batch.error_detail,
    )


def confirm(
    session: Session,
    *,
    campaign_id: uuid.UUID,
    content: bytes,
    filename: str | None,
    sheet_index: int | None = None,
    uploaded_by: str | None = None,
    actor: str = IMPORT_ACTOR,
) -> ImportResult:
    """Import the file into the Campaign. The first point anything is written."""

    _require_feature()
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise CampaignNotFound(f"campaign {campaign_id} does not exist")

    inspection = inspect(content, filename)
    sheet = inspection.sheet(sheet_index)
    if sheet is None or not sheet.detection.recognized:
        raise UnreadableFileError(
            apollo.missing_header_message(sheet.detection)
            if sheet is not None
            else "No worksheet in this file carries a recognizable contact header row."
        )
    if sheet.data_row_count == 0:
        raise UnreadableFileError("The selected worksheet has a header row but no data rows.")

    schema_id = sheet.detection.schema_id
    assert schema_id is not None
    content_hash = batch_content_hash(content, sheet_index=sheet.index, schema_id=schema_id)

    # Idempotent confirm: the same bytes, the same worksheet, the same Campaign.
    existing = session.scalars(
        select(ImportBatch).where(
            ImportBatch.campaign_id == campaign_id,
            ImportBatch.content_hash == content_hash,
            ImportBatch.status == ImportBatchStatus.COMPLETED,
        )
    ).first()
    if existing is not None:
        return _result_from_batch(session, existing, reused=True)

    rows = _parsed_rows(content, filename, sheet.index)

    # --- Stage 1: durable raw capture, committed before anything is decided ---
    #
    # Same staging discipline the existing importer uses, for the same reason: if
    # processing dies, the operator's rows are still on record and re-processable
    # rather than lost with the request.
    batch = ImportBatch(
        campaign_id=campaign_id,
        filename=filename,
        sanitized_filename=inspection.sanitized_filename,
        content_hash=content_hash,
        status=ImportBatchStatus.VALIDATING,
        source_format=(
            ImportSourceFormat.XLSX
            if inspection.source_format == "xlsx"
            else ImportSourceFormat.CSV
        ),
        source_schema=schema_id,
        selected_sheet_index=sheet.index,
        selected_sheet_name=sheet.name,
        detected_headers=list(sheet.header),
        uploaded_by=uploaded_by,
        mime_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            if inspection.source_format == "xlsx"
            else "text/csv"
        ),
        parser_version=inspection.parser_version,
        mapper_version=apollo.APOLLO_READER_VERSION,
        column_mapping={
            column: canonical for canonical, column in sheet.detection.field_columns.items()
        },
        source_name=source_name_for(sheet.detection),
        source_reference=inspection.sanitized_filename,
        exported_by=uploaded_by,
        total_rows=len(rows),
    )
    session.add(batch)
    session.flush()
    record_audit_event(
        session,
        actor=actor,
        action="import.batch_created",
        entity_type="import_batch",
        entity_id=str(batch.id),
        new_state=ImportBatchStatus.VALIDATING.value,
        reason="campaign-bound contact file import received",
        context={
            "campaign_id": str(campaign_id),
            "total_rows": len(rows),
            "source_schema": schema_id,
            "source_format": inspection.source_format,
            "sheet_index": sheet.index,
        },
    )
    # The positional cells travel beside the durable ``raw_data`` payload rather
    # than replacing it. JSONB cannot hold two entries under one key, so a file
    # with two columns of the same name has to lose one there; the tuple is what
    # the reading uses so that neither the loss nor the choice is silent.
    import_rows: list[tuple[ImportRow, dict[str, str], tuple[str, ...]]] = []
    for parsed_row in rows:
        import_row = ImportRow(
            batch_id=batch.id,
            row_number=parsed_row.row_number,
            sheet_index=parsed_row.sheet_index,
            sheet_name=parsed_row.sheet_name,
            raw_data=parsed_row.raw,
        )
        session.add(import_row)
        import_rows.append((import_row, parsed_row.raw, parsed_row.cells))
    session.flush()
    session.commit()

    # --- Stage 2: one SAVEPOINT per row --------------------------------------
    tally = _Tally()
    registers = _Registers()
    for import_row, raw, cells in import_rows:
        _process_one_row(
            session,
            campaign=campaign,
            batch=batch,
            import_row=import_row,
            raw=raw,
            cells=cells,
            detection=sheet.detection,
            checksum=inspection.checksum,
            schema_id=schema_id,
            registers=registers,
            tally=tally,
            actor=actor,
        )

    batch.status = ImportBatchStatus.COMPLETED
    batch.accepted_rows = tally.by_disposition[RowDisposition.IMPORTED]
    batch.duplicate_rows = (
        tally.by_disposition[RowDisposition.MATCHED_EXISTING]
        + tally.by_disposition[RowDisposition.ALREADY_IN_CAMPAIGN]
        + tally.by_disposition[RowDisposition.SKIPPED_DUPLICATE]
    )
    batch.already_in_campaign_rows = tally.by_disposition[RowDisposition.ALREADY_IN_CAMPAIGN]
    batch.ambiguous_rows = tally.by_disposition[RowDisposition.REVIEW_REQUIRED]
    batch.suppressed_rows = tally.by_disposition[RowDisposition.SUPPRESSED]
    batch.rejected_rows = tally.by_disposition[RowDisposition.FAILED]
    batch.contacts_created = tally.contacts_created
    batch.confirmed_at = datetime.now(UTC)
    batch.completed_at = datetime.now(UTC)
    record_audit_event(
        session,
        actor=actor,
        action="import.completed",
        entity_type="import_batch",
        entity_id=str(batch.id),
        previous_state=ImportBatchStatus.VALIDATING.value,
        new_state=ImportBatchStatus.COMPLETED.value,
        reason="campaign-bound contact file import processed",
        context={key.value: value for key, value in tally.by_disposition.items()},
    )
    session.commit()

    return ImportResult(
        batch_id=batch.id,
        status=batch.status,
        total_rows=batch.total_rows,
        imported=tally.by_disposition[RowDisposition.IMPORTED],
        matched_existing=tally.by_disposition[RowDisposition.MATCHED_EXISTING],
        already_in_campaign=tally.by_disposition[RowDisposition.ALREADY_IN_CAMPAIGN],
        skipped_duplicate=tally.by_disposition[RowDisposition.SKIPPED_DUPLICATE],
        review_required=tally.by_disposition[RowDisposition.REVIEW_REQUIRED],
        suppressed=tally.by_disposition[RowDisposition.SUPPRESSED],
        failed=tally.by_disposition[RowDisposition.FAILED],
        contacts_created=tally.contacts_created,
        companies_created=tally.companies_created,
    )


def _process_one_row(
    session: Session,
    *,
    campaign: Campaign,
    batch: ImportBatch,
    import_row: ImportRow,
    raw: dict[str, str],
    detection: apollo.SchemaDetection,
    checksum: str,
    schema_id: str,
    registers: _Registers,
    tally: _Tally,
    actor: str,
    cells: Sequence[str] = (),
) -> None:
    """Plan and commit one row inside its own SAVEPOINT.

    The two-savepoint shape is deliberate. The first holds every write the row
    produces, so a database failure part-way through discards the Company, the
    Contact, the membership and the evidence together — a half-created identity
    is worse than no identity. The second records the failure itself, on a clean
    savepoint, so the reason survives even though the work did not.
    """

    plan: RowPlan | None = None
    try:
        with session.begin_nested():
            plan = plan_row(
                session,
                campaign_id=campaign.id,
                raw=raw,
                cells=cells,
                detection=detection,
                row_number=import_row.row_number,
                sheet_index=import_row.sheet_index,
                sheet_name=import_row.sheet_name,
                registers=registers,
            )
            _commit_row(
                session,
                campaign=campaign,
                batch=batch,
                import_row=import_row,
                plan=plan,
                checksum=checksum,
                schema_id=schema_id,
                tally=tally,
                actor=actor,
            )
        session.commit()
        return
    except SQLAlchemyError as exc:
        session.rollback()
        _record_row_failure(
            session,
            import_row=import_row,
            plan=plan,
            code="database_error",
            detail=(
                "This row could not be written and was rolled back on its own; every "
                f"other row is unaffected ({type(exc).__name__})."
            ),
            tally=tally,
        )
        session.commit()


def _record_row_failure(
    session: Session,
    *,
    import_row: ImportRow,
    plan: RowPlan | None,
    code: str,
    detail: str,
    tally: _Tally,
) -> None:
    """Store a row-level failure with no stack trace and no raw PII.

    ``detail`` is written by this module and names the exception *type* at most.
    A driver's own message routinely quotes the offending row back, which for
    this data means an email address and a person's name in a place designed to
    be read by anyone who can open the batch.
    """

    existing = session.scalars(
        select(ImportRowValidation).where(ImportRowValidation.import_row_id == import_row.id)
    ).first()
    if existing is not None:
        return
    validation = ImportRowValidation(
        import_row_id=import_row.id,
        outcome=ImportRowOutcome.REJECTED,
        row_fingerprint=plan.fingerprint if plan is not None else None,
        error_code=code,
        note=detail,
        warnings=[],
    )
    session.add(validation)
    session.add(
        ImportRowError(
            import_row_id=import_row.id,
            column_name=None,
            code=code,
            message=f"row {import_row.row_number}: {detail}",
        )
    )
    tally.record(RowDisposition.FAILED)
    session.flush()


def _commit_row(
    session: Session,
    *,
    campaign: Campaign,
    batch: ImportBatch,
    import_row: ImportRow,
    plan: RowPlan,
    checksum: str,
    schema_id: str,
    tally: _Tally,
    actor: str,
) -> None:
    """Apply one planned row. Runs inside the caller's SAVEPOINT."""

    row = plan.apollo_row
    warnings_payload = [{"code": code, "message": message} for code, message in plan.warnings]
    validation = ImportRowValidation(
        import_row_id=import_row.id,
        outcome=durable_outcome(plan.disposition),
        normalized_data=apollo.bounded_source_payload(row),
        row_fingerprint=plan.fingerprint,
        error_code=plan.error_code,
        note=plan.error_detail,
        warnings=warnings_payload,
        contact_match_basis=(
            plan.contact.basis.value if plan.contact and plan.contact.basis else None
        ),
        company_match_basis=(
            plan.company.basis.value if plan.company and plan.company.basis else None
        ),
    )
    session.add(validation)

    if plan.disposition in {
        RowDisposition.FAILED,
        RowDisposition.REVIEW_REQUIRED,
        RowDisposition.SUPPRESSED,
        RowDisposition.SKIPPED_DUPLICATE,
    }:
        if plan.error_code and plan.disposition is RowDisposition.FAILED:
            session.add(
                ImportRowError(
                    import_row_id=import_row.id,
                    column_name=None,
                    code=plan.error_code,
                    message=f"row {import_row.row_number}: {plan.error_detail}",
                )
            )
        # A row held for review still keeps its supplied addresses as evidence:
        # the operator deciding it needs to see what the file actually said.
        if plan.disposition is RowDisposition.REVIEW_REQUIRED:
            _write_addresses(
                session,
                batch=batch,
                import_row=import_row,
                campaign_id=campaign.id,
                contact_id=None,
                plan=plan,
                checksum=checksum,
                schema_id=schema_id,
                accepted=False,
                rejection_code="row_held_for_review",
            )
        tally.record(plan.disposition)
        session.flush()
        return

    assert plan.company is not None and plan.contact is not None
    company_was_new = plan.company.action is company_resolution.CompanyAction.CREATE
    company: Company | None = company_resolution.apply(
        session,
        company_plan=plan.company,
        row=row,
        batch_id=batch.id,
        actor=actor,
    )
    if company_was_new and plan.company.action is company_resolution.CompanyAction.CREATE:
        tally.companies_created += 1

    contact_was_new = plan.contact.action is contact_resolution.ContactAction.CREATE
    contact: Contact | None = contact_resolution.apply(
        session,
        contact_plan=plan.contact,
        row=row,
        company_id=company.id if company else None,
        company_domain=plan.company.domain,
        batch_id=batch.id,
        actor=actor,
    )
    assert contact is not None
    if contact_was_new:
        tally.contacts_created += 1
    # Warnings raised while applying (a disputed LinkedIn identifier, an address
    # another Contact already owns) are only known after the write.
    if plan.contact.warnings:
        merged = {(code, message) for code, message in plan.warnings}
        for code, message in plan.contact.warnings:
            if (code, message) not in merged:
                plan.warnings.append((code, message))
        validation.warnings = [
            {"code": code, "message": message} for code, message in plan.warnings
        ]

    # --- Enrolment through the authoritative service --------------------------
    enrolment = enrol_contact(
        session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="import",
        source_reference=str(batch.id),
        import_batch_id=batch.id,
        # Stable across re-confirmation of the same row content into the same
        # Campaign, so a repeated confirm reuses the source record instead of
        # appending a second one that says exactly the same thing.
        idempotency_key=f"file-import:{campaign.id}:{contact.id}:{plan.fingerprint}",
        actor=actor,
        enqueue=True,
    )
    membership = enrolment.membership

    imported_email = _write_addresses(
        session,
        batch=batch,
        import_row=import_row,
        campaign_id=campaign.id,
        contact_id=contact.id,
        plan=plan,
        checksum=checksum,
        schema_id=schema_id,
        accepted=True,
        rejection_code=None,
    )

    session.add(
        ProvenanceRecord(
            contact_id=contact.id,
            import_batch_id=batch.id,
            import_row_id=import_row.id,
            source_name=batch.source_name,
            source_reference=batch.source_reference,
            exported_by=batch.exported_by,
            exported_at=batch.exported_at,
        )
    )
    record_import_observations(
        session,
        contact=contact,
        normalized={
            "title": row.title,
            "company_name": row.company_name,
            "company_size": row.employee_count,
            "industry": row.industry,
            "country": row.country,
            "linkedin_url": row.person_linkedin_identity or row.person_linkedin_url,
        },
        batch_id=batch.id,
        row_id=import_row.id,
        resolved_provenance={
            "source_name": batch.source_name,
            "source_reference": batch.source_reference,
            "exported_by": batch.exported_by,
            "exported_at": batch.exported_at,
        },
        actor=actor,
    )

    validation.company_id = company.id if company else None
    validation.campaign_contact_id = membership.id
    validation.imported_email_id = imported_email.id if imported_email else None
    validation.contact_id = contact.id
    validation.membership_action = "created" if enrolment.created else "existing"
    validation.note = plan.error_detail
    tally.record(plan.disposition)
    session.flush()


#: Recorded on an address a later file supplied for somebody who already has an
#: accepted address in this Campaign. Not a rejection: nothing is wrong with it.
RESTATED_CODE = "retained_person_already_has_an_accepted_address"

#: Recorded on the address of a row an operator still has to decide about. Also
#: not a rejection — the row was held, and the address may well be the right one.
HELD_CODE = "row_held_for_review"

#: Columns of :class:`ImportedContactEmail` that say WHERE a statement came from,
#: or what VMR did with it, rather than WHAT was stated.
#:
#: Stated as an exclusion and read off the model's own columns, for the reason
#: the row fingerprint is: a hand-maintained inclusion list drifts. The previous
#: version of :func:`_retain_restated_address` compared two fields — the address
#: and the normalized status — so a vendor correcting its verification date, its
#: source, its verification source or its catch-all claim produced a new row
#: fingerprint, was correctly recognized as a new statement by the preview, and
#: was then silently dropped at commit. A column added to this table later is
#: part of the statement by default.
_STATEMENT_EXCLUDED_COLUMNS: frozenset[str] = frozenset(
    {
        "id",
        # Provenance: which upload, which row, which Campaign, which person.
        "import_batch_id",
        "import_row_id",
        "campaign_id",
        "contact_id",
        "source_row_number",
        "source_sheet_name",
        "source_file_checksum",
        # The row's fingerprint covers the whole ROW, including fields that are
        # not about the address at all. Two rows differing only in job title
        # make the same statement about the address, and storing a second copy
        # of it would be noise rather than evidence.
        "row_fingerprint",
        # What VMR decided, which is not part of what the vendor said.
        "email_stage_outcome",
        "verification_stage_outcome",
        "rejection_code",
        "created_at",
    }
)


def statement_digest(record: ImportedContactEmail) -> str:
    """A deterministic digest of everything one imported-address record states.

    Covers the slot, both forms of the address, the schema it was read under and
    every ``provider_*`` claim — and nothing about where it came from or what was
    done with it. Two records with the same digest say the same thing, so the
    second adds nothing; two with different digests are different statements and
    both have to survive.
    """

    payload: dict[str, str | None] = {}
    for column in ImportedContactEmail.__table__.columns:
        if column.name in _STATEMENT_EXCLUDED_COLUMNS:
            continue
        value = getattr(record, column.name, None)
        if value is None:
            payload[column.name] = None
        elif isinstance(value, datetime):
            payload[column.name] = value.astimezone(UTC).isoformat()
        elif isinstance(value, enum.Enum):
            payload[column.name] = value.value
        else:
            payload[column.name] = str(value)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _statements_on_record(
    session: Session, *, campaign_id: uuid.UUID, contact_id: uuid.UUID, slot: ImportedEmailSlot
) -> set[str]:
    """Every statement already durably held about this person's address here."""

    return {
        statement_digest(existing)
        for existing in session.scalars(
            select(ImportedContactEmail).where(
                ImportedContactEmail.campaign_id == campaign_id,
                ImportedContactEmail.contact_id == contact_id,
                ImportedContactEmail.slot == slot,
            )
        ).all()
    }


def _retain_restated_address(
    session: Session,
    *,
    record: ImportedContactEmail,
    campaign_id: uuid.UUID,
    contact_id: uuid.UUID,
) -> ImportedContactEmail | None:
    """Store a restated address as retained evidence, never as the one in use.

    Returns the record if it was written, ``None`` if this exact statement is
    already held — which is what keeps a repeated upload of the same corrected
    file idempotent, however many times it is repeated.

    "Retained" is a real state and not a soft rejection: no stage outcome, no
    verification outcome, and a code that says why it is not the active address.
    Exactly one address is the one this Campaign uses for this person, and
    swapping it is an operator's decision rather than a consequence of somebody
    uploading a newer file.
    """

    held = _statements_on_record(
        session, campaign_id=campaign_id, contact_id=contact_id, slot=record.slot
    )
    record.email_stage_outcome = None
    record.verification_stage_outcome = None
    record.rejection_code = RESTATED_CODE
    if statement_digest(record) in held:
        return None
    try:
        with session.begin_nested():
            session.add(record)
            session.flush()
    except IntegrityError:
        # The same source row processed twice inside one batch.
        return None
    return record


def _bounded(value: str | None, limit: int) -> str | None:
    """Trim a vendor-supplied string to what its column can hold."""

    return value[:limit] if value is not None else None


def _write_addresses(
    session: Session,
    *,
    batch: ImportBatch,
    import_row: ImportRow,
    campaign_id: uuid.UUID,
    contact_id: uuid.UUID | None,
    plan: RowPlan,
    checksum: str,
    schema_id: str,
    accepted: bool,
    rejection_code: str | None,
) -> ImportedContactEmail | None:
    """Persist every supplied address, and mark at most the primary as accepted.

    The secondary and tertiary addresses are stored with their vendor metadata
    and **no** stage outcome. That is the whole of IMP-001 §13: they are retained
    so nothing is lost, and they are never promoted, because deciding that a
    secondary address is the one to use is a judgement about a person the file
    does not license anyone to make.
    """

    row = plan.apollo_row
    accepted_record: ImportedContactEmail | None = None

    for address in row.addresses:
        slot = ImportedEmailSlot(address.slot)
        is_primary = slot is ImportedEmailSlot.PRIMARY
        if is_primary and accepted:
            stage_outcome: ImportedEmailStageOutcome | None = (
                ImportedEmailStageOutcome.IMPORTED_EMAIL_ACCEPTED
            )
            verification_outcome: ImportedVerificationOutcome | None = (
                ImportedVerificationOutcome.VERIFICATION_BYPASSED_IMPORTED_EMAIL
            )
            code = None
        elif is_primary and address.normalized is None:
            # The address itself could not be used. This is the one case that is
            # genuinely a refusal of the address rather than of the row.
            stage_outcome = ImportedEmailStageOutcome.IMPORTED_EMAIL_REJECTED
            verification_outcome = ImportedVerificationOutcome.VERIFICATION_NOT_PERFORMED
            code = rejection_code or plan.error_code
        elif is_primary:
            # A row held for review. Its address was not refused — an operator has
            # simply not decided about it yet — so it is retained in the same
            # neutral state a secondary address is, with a code saying why. Marking
            # it REJECTED said the file was wrong about an address that may well be
            # right, and made the held row's evidence look like a verdict.
            stage_outcome = None
            verification_outcome = None
            code = rejection_code or plan.error_code
        else:
            stage_outcome = None
            verification_outcome = None
            code = None

        record = ImportedContactEmail(
            import_batch_id=batch.id,
            import_row_id=import_row.id,
            campaign_id=campaign_id,
            contact_id=contact_id,
            slot=slot,
            raw_email=address.raw[:512],
            normalized_email=(address.normalized[:MAX_EMAIL_CHARS] if address.normalized else None),
            source_row_number=import_row.row_number,
            source_sheet_name=import_row.sheet_name,
            source_file_checksum=checksum,
            source_schema=schema_id,
            row_fingerprint=plan.fingerprint,
            provider_source=_bounded(address.provider_source, MAX_PROVIDER_TEXT_CHARS),
            provider_status_raw=_bounded(address.provider_status_raw, _TOKEN),
            provider_status_normalized=_bounded(address.provider_status_normalized, _TOKEN),
            provider_verification_source=_bounded(
                address.provider_verification_source, MAX_PROVIDER_TEXT_CHARS
            ),
            provider_catch_all_raw=_bounded(address.provider_catch_all_raw, _TOKEN),
            provider_catch_all_normalized=_bounded(address.provider_catch_all_normalized, _TOKEN),
            provider_last_verified_at=address.provider_last_verified_at,
            provider_last_verified_raw=_bounded(address.provider_last_verified_raw, _TOKEN),
            email_stage_outcome=stage_outcome,
            verification_stage_outcome=verification_outcome,
            rejection_code=code,
        )
        try:
            with session.begin_nested():
                session.add(record)
                session.flush()
        except IntegrityError:
            if not is_primary:
                continue
            # One of two partial unique indexes refused this write, and both are
            # doing their job. Either this exact row content already produced
            # accepted evidence for this Campaign, or this PERSON already has an
            # accepted address here and a later file has restated them.
            existing_accepted = _existing_evidence(
                session, campaign_id=campaign_id, fingerprint=plan.fingerprint
            ) or (
                accepted_primary_email(session, campaign_id=campaign_id, contact_id=contact_id)
                if contact_id is not None
                else None
            )
            if existing_accepted is None or not accepted or contact_id is None:
                accepted_record = accepted_record or existing_accepted
                continue
            # The row said something new — a corrected vendor claim, a different
            # address — and it has to be recorded or the correction is lost.
            retained = _retain_restated_address(
                session,
                record=record,
                campaign_id=campaign_id,
                contact_id=contact_id,
            )
            # The row's own evidence is what its batch lineage should point at.
            # Pointing it at the address accepted by an EARLIER batch made this
            # upload look as though it had contributed nothing.
            accepted_record = retained or existing_accepted
            continue
        if is_primary and accepted:
            accepted_record = record

    return accepted_record


# ---------------------------------------------------------------------------
# Reading a completed batch back
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BatchRowView:
    """One committed row as the result screen shows it."""

    row: ImportRow
    validation: ImportRowValidation | None
    imported_email: ImportedContactEmail | None
    contact: Contact | None
    company: Company | None


@dataclass(frozen=True)
class BatchCounts:
    """One import batch's row outcomes as a genuine partition of its rows.

    The durable columns do not partition. ``duplicate_rows`` is the sum of three
    dispositions and ``already_in_campaign_rows`` is one of the three, so a
    screen that renders both side by side under "every raw row has exactly one
    outcome" double-counts and can show a total larger than the file.

    So the split is computed here instead of guessed at in a template, and
    ``unprocessed`` absorbs whatever the recorded outcomes do not account for —
    a batch interrupted between staging and processing has rows in exactly that
    state, and showing them as zero would be the same lie in the other
    direction.
    """

    total: int
    imported: int
    already_in_campaign: int
    matched_or_duplicate: int
    review_required: int
    suppressed: int
    refused: int
    unprocessed: int

    @property
    def accounted(self) -> int:
        return (
            self.imported
            + self.already_in_campaign
            + self.matched_or_duplicate
            + self.review_required
            + self.suppressed
            + self.refused
            + self.unprocessed
        )

    @property
    def partitions(self) -> bool:
        """True when the buckets account for every row exactly once."""

        return self.accounted == self.total


class _HasBatchCounters(Protocol):
    """The counter columns :func:`batch_counts` needs.

    A protocol rather than ``ImportBatch``, so the Admin Workbench's own read
    row can be split by the same function the ORM object is. Two implementations
    of this arithmetic is exactly how the screens came to disagree.
    """

    @property
    def total_rows(self) -> int: ...
    @property
    def accepted_rows(self) -> int: ...
    @property
    def rejected_rows(self) -> int: ...
    @property
    def duplicate_rows(self) -> int: ...
    @property
    def suppressed_rows(self) -> int: ...
    @property
    def ambiguous_rows(self) -> int: ...
    @property
    def already_in_campaign_rows(self) -> int: ...


def batch_counts(batch: _HasBatchCounters) -> BatchCounts:
    """Split one batch's durable counters into non-overlapping buckets."""

    already = max(0, batch.already_in_campaign_rows or 0)
    # ``duplicate_rows`` covers matched-existing, already-in-campaign and
    # repeated rows together. Subtracting the one that is displayed separately
    # is what stops the same row being counted twice on the same screen.
    matched_or_duplicate = max(0, (batch.duplicate_rows or 0) - already)
    recorded = (
        (batch.accepted_rows or 0)
        + already
        + matched_or_duplicate
        + (batch.ambiguous_rows or 0)
        + (batch.suppressed_rows or 0)
        + (batch.rejected_rows or 0)
    )
    return BatchCounts(
        total=batch.total_rows or 0,
        imported=batch.accepted_rows or 0,
        already_in_campaign=already,
        matched_or_duplicate=matched_or_duplicate,
        review_required=batch.ambiguous_rows or 0,
        suppressed=batch.suppressed_rows or 0,
        refused=batch.rejected_rows or 0,
        unprocessed=max(0, (batch.total_rows or 0) - recorded),
    )


def batch_rows(
    session: Session, *, batch_id: uuid.UUID, limit: int = 100, offset: int = 0
) -> tuple[list[BatchRowView], int]:
    """One page of a batch's rows, with what each of them produced."""

    total = (
        session.scalar(
            select(func.count()).select_from(ImportRow).where(ImportRow.batch_id == batch_id)
        )
        or 0
    )
    rows = list(
        session.scalars(
            select(ImportRow)
            .where(ImportRow.batch_id == batch_id)
            .order_by(ImportRow.sheet_index, ImportRow.row_number)
            .limit(limit)
            .offset(offset)
        ).all()
    )
    # Four statements for the whole page rather than four per row. The page size
    # is the operator's choice, so a per-row lookup here would make a large batch
    # arbitrarily expensive on a screen whose only job is to be readable.
    row_ids = [row.id for row in rows]
    if not row_ids:
        return [], int(total)

    validations = {
        record.import_row_id: record
        for record in session.scalars(
            select(ImportRowValidation).where(ImportRowValidation.import_row_id.in_(row_ids))
        ).all()
    }
    emails = {
        record.import_row_id: record
        for record in session.scalars(
            select(ImportedContactEmail).where(
                ImportedContactEmail.import_row_id.in_(row_ids),
                ImportedContactEmail.slot == ImportedEmailSlot.PRIMARY,
            )
        ).all()
    }
    contact_ids = {v.contact_id for v in validations.values() if v.contact_id}
    company_ids = {v.company_id for v in validations.values() if v.company_id}
    contacts = (
        {
            record.id: record
            for record in session.scalars(select(Contact).where(Contact.id.in_(contact_ids))).all()
        }
        if contact_ids
        else {}
    )
    companies = (
        {
            record.id: record
            for record in session.scalars(select(Company).where(Company.id.in_(company_ids))).all()
        }
        if company_ids
        else {}
    )

    views: list[BatchRowView] = []
    for row in rows:
        validation = validations.get(row.id)
        views.append(
            BatchRowView(
                row=row,
                validation=validation,
                imported_email=emails.get(row.id),
                contact=(
                    contacts.get(validation.contact_id)
                    if validation is not None and validation.contact_id
                    else None
                ),
                company=(
                    companies.get(validation.company_id)
                    if validation is not None and validation.company_id
                    else None
                ),
            )
        )
    return views, int(total)


def get_batch(session: Session, batch_id: uuid.UUID) -> ImportBatch | None:
    return session.get(ImportBatch, batch_id)


def campaign_batches(session: Session, campaign_id: uuid.UUID) -> list[ImportBatch]:
    """Every file import into one Campaign, newest first."""

    return list(
        session.scalars(
            select(ImportBatch)
            .where(
                ImportBatch.campaign_id == campaign_id,
                ImportBatch.source_schema.is_not(None),
            )
            .order_by(ImportBatch.created_at.desc())
        ).all()
    )


def accepted_primary_email(
    session: Session, *, campaign_id: uuid.UUID, contact_id: uuid.UUID
) -> ImportedContactEmail | None:
    """The address this Campaign was told to use for this person, if any.

    The Email Agent's single question. Campaign-scoped, so a Contact acquired
    through Sales Navigator in another Campaign is untouched by this path and
    keeps the ordinary discovery behaviour exactly as it was.
    """

    return session.scalars(
        select(ImportedContactEmail)
        .where(
            ImportedContactEmail.campaign_id == campaign_id,
            ImportedContactEmail.contact_id == contact_id,
            ImportedContactEmail.slot == ImportedEmailSlot.PRIMARY,
            ImportedContactEmail.email_stage_outcome
            == ImportedEmailStageOutcome.IMPORTED_EMAIL_ACCEPTED,
        )
        # ``uq_imported_contact_emails_accepted_campaign_contact`` makes this at
        # most one row. The ordering is kept, with ``id`` as the tiebreak, so the
        # answer stays deterministic rather than accidental if that constraint is
        # ever relaxed: ``created_at`` alone is transaction-start time and is
        # identical across everything one batch writes.
        .order_by(ImportedContactEmail.created_at.desc(), ImportedContactEmail.id.desc())
        .limit(1)
    ).first()


def retained_alternates(
    session: Session, *, campaign_id: uuid.UUID, contact_id: uuid.UUID
) -> list[ImportedContactEmail]:
    """The secondary and tertiary addresses kept for this person, unpromoted."""

    return list(
        session.scalars(
            select(ImportedContactEmail)
            .where(
                ImportedContactEmail.campaign_id == campaign_id,
                ImportedContactEmail.contact_id == contact_id,
                ImportedContactEmail.slot != ImportedEmailSlot.PRIMARY,
            )
            .order_by(ImportedContactEmail.slot)
        ).all()
    )


#: Re-exported so callers importing this module alone can name the stage the
#: import path deliberately never reaches.
UNAVAILABLE_STAGE = AgentIdentifier.SENDING


def imported_email_summary(record: ImportedContactEmail) -> dict[str, Any]:
    """A display-safe projection of one imported address and its provider claims.

    Every provider field is named so that reading it aloud makes the ownership
    obvious. There is deliberately no key called ``verified``.
    """

    return {
        "slot": record.slot.value,
        "email": record.normalized_email,
        "raw_email": apollo.neutralize_formula(record.raw_email),
        "provider_source": record.provider_source,
        "provider_claimed_status": record.provider_status_normalized,
        "provider_claimed_status_raw": record.provider_status_raw,
        "provider_claimed_verification_source": record.provider_verification_source,
        "provider_claimed_catch_all": record.provider_catch_all_normalized,
        "provider_claimed_last_verified_at": (
            record.provider_last_verified_at.isoformat()
            if record.provider_last_verified_at
            else None
        ),
        "vmr_email_stage_outcome": (
            record.email_stage_outcome.value if record.email_stage_outcome else None
        ),
        "vmr_verification_stage_outcome": (
            record.verification_stage_outcome.value if record.verification_stage_outcome else None
        ),
        "source_row_number": record.source_row_number,
        "source_file_checksum": record.source_file_checksum,
        "source_schema": record.source_schema,
    }
