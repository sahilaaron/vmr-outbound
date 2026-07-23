"""Read services backing the operator workbench pages.

Every value the workbench shows comes from these queries against the local
development database — the workbench renders no synthetic, simulated, or
placeholder numbers. Business rules stay here (service layer), not in templates
or browser JavaScript (AGENTS.md).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models.campaign import Campaign, CampaignContact
from app.models.contact import Contact
from app.models.enums import (
    ContactWorkflowState,
    ImportBatchStatus,
    ImportRowOutcome,
    SuppressionType,
)
from app.models.import_batch import ImportBatch, ImportRow, ImportRowError, ImportRowValidation
from app.models.provenance import ProvenanceRecord
from app.models.suppression import Suppression

# --- Overview ----------------------------------------------------------------


@dataclass
class OverviewStats:
    """Database-backed numbers for the Overview page."""

    campaign_count: int = 0
    contact_count: int = 0
    import_batch_count: int = 0
    accepted_rows: int = 0
    rejected_rows: int = 0
    duplicate_rows: int = 0
    ambiguous_rows: int = 0
    suppressed_rows: int = 0
    suppression_entries: int = 0
    recent_batches: list[ImportBatch] = field(default_factory=list)
    attention_batches: list[ImportBatch] = field(default_factory=list)
    database_ok: bool = True
    database_detail: str | None = None


def load_overview(session: Session) -> OverviewStats:
    """Aggregate the real counts the Overview page displays."""

    stats = OverviewStats()
    try:
        session.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - exercised via error path
        stats.database_ok = False
        stats.database_detail = type(exc).__name__
        return stats

    stats.campaign_count = session.scalar(select(func.count(Campaign.id))) or 0
    stats.contact_count = session.scalar(select(func.count(Contact.id))) or 0
    stats.import_batch_count = session.scalar(select(func.count(ImportBatch.id))) or 0
    stats.suppression_entries = session.scalar(select(func.count(Suppression.id))) or 0

    sums = session.execute(
        select(
            func.coalesce(func.sum(ImportBatch.accepted_rows), 0),
            func.coalesce(func.sum(ImportBatch.rejected_rows), 0),
            func.coalesce(func.sum(ImportBatch.duplicate_rows), 0),
            func.coalesce(func.sum(ImportBatch.ambiguous_rows), 0),
            func.coalesce(func.sum(ImportBatch.suppressed_rows), 0),
        )
    ).one()
    (
        stats.accepted_rows,
        stats.rejected_rows,
        stats.duplicate_rows,
        stats.ambiguous_rows,
        stats.suppressed_rows,
    ) = (int(v) for v in sums)

    stats.recent_batches = list(
        session.scalars(select(ImportBatch).order_by(ImportBatch.created_at.desc()).limit(5)).all()
    )
    # Imports needing attention: failed outright, or completed with rows a
    # human should look at (rejected or ambiguous).
    stats.attention_batches = list(
        session.scalars(
            select(ImportBatch)
            .where(
                (ImportBatch.status == ImportBatchStatus.FAILED)
                | (ImportBatch.rejected_rows > 0)
                | (ImportBatch.ambiguous_rows > 0)
            )
            .order_by(ImportBatch.created_at.desc())
            .limit(5)
        ).all()
    )
    return stats


# --- Import batches ----------------------------------------------------------


def list_batches(
    session: Session, *, limit: int = 50, offset: int = 0
) -> tuple[list[tuple[ImportBatch, Campaign]], int]:
    """Paginated import batches with their campaigns, newest first."""

    total = session.scalar(select(func.count(ImportBatch.id))) or 0
    rows = session.execute(
        select(ImportBatch, Campaign)
        .join(Campaign, Campaign.id == ImportBatch.campaign_id)
        .order_by(ImportBatch.created_at.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return [(batch, campaign) for batch, campaign in rows], total


def get_batch(session: Session, batch_id: uuid.UUID) -> tuple[ImportBatch, Campaign] | None:
    row = session.execute(
        select(ImportBatch, Campaign)
        .join(Campaign, Campaign.id == ImportBatch.campaign_id)
        .where(ImportBatch.id == batch_id)
    ).first()
    if row is None:
        return None
    return row[0], row[1]


@dataclass(frozen=True)
class BatchRow:
    """One import row joined with its outcome and errors for display."""

    row: ImportRow
    validation: ImportRowValidation | None
    errors: list[ImportRowError]


def list_batch_rows(
    session: Session,
    batch_id: uuid.UUID,
    *,
    outcome: ImportRowOutcome | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[BatchRow], int]:
    """Paginated rows of one batch, optionally filtered by outcome."""

    query = (
        select(ImportRow, ImportRowValidation)
        .outerjoin(ImportRowValidation, ImportRowValidation.import_row_id == ImportRow.id)
        .where(ImportRow.batch_id == batch_id)
    )
    count_query = (
        select(func.count(ImportRow.id))
        .outerjoin(ImportRowValidation, ImportRowValidation.import_row_id == ImportRow.id)
        .where(ImportRow.batch_id == batch_id)
    )
    if outcome is not None:
        query = query.where(ImportRowValidation.outcome == outcome)
        count_query = count_query.where(ImportRowValidation.outcome == outcome)

    total = session.scalar(count_query) or 0
    pairs = session.execute(
        query.order_by(ImportRow.sheet_index, ImportRow.row_number).limit(limit).offset(offset)
    ).all()

    row_ids = [row.id for row, _ in pairs]
    errors_by_row: dict[uuid.UUID, list[ImportRowError]] = {}
    if row_ids:
        for err in session.scalars(
            select(ImportRowError).where(ImportRowError.import_row_id.in_(row_ids))
        ).all():
            errors_by_row.setdefault(err.import_row_id, []).append(err)

    return (
        [
            BatchRow(row=row, validation=validation, errors=errors_by_row.get(row.id, []))
            for row, validation in pairs
        ],
        total,
    )


def get_batch_row(session: Session, batch_id: uuid.UUID, row_id: uuid.UUID) -> BatchRow | None:
    """One import row (scoped to its batch) with outcome and errors."""

    row = session.scalars(
        select(ImportRow).where(ImportRow.id == row_id, ImportRow.batch_id == batch_id)
    ).first()
    if row is None:
        return None
    validation = session.scalars(
        select(ImportRowValidation).where(ImportRowValidation.import_row_id == row.id)
    ).first()
    errors = list(
        session.scalars(select(ImportRowError).where(ImportRowError.import_row_id == row.id)).all()
    )
    return BatchRow(row=row, validation=validation, errors=errors)


# --- Contacts ----------------------------------------------------------------


def list_contacts(
    session: Session,
    *,
    search: str | None = None,
    campaign_id: uuid.UUID | None = None,
    state: ContactWorkflowState | None = None,
    has_email: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Contact], int]:
    """Paginated contact list with search and basic filters."""

    query = select(Contact)
    count_query = select(func.count(func.distinct(Contact.id)))

    if search:
        needle = f"%{search.strip().lower()}%"
        condition = (
            func.lower(Contact.first_name).like(needle)
            | func.lower(Contact.last_name).like(needle)
            | func.lower(Contact.company_name).like(needle)
            | func.lower(Contact.company_domain).like(needle)
            | func.lower(func.coalesce(Contact.email, "")).like(needle)
        )
        query = query.where(condition)
        count_query = count_query.where(condition)

    if campaign_id is not None or state is not None:
        query = query.join(CampaignContact, CampaignContact.contact_id == Contact.id)
        count_query = count_query.join(CampaignContact, CampaignContact.contact_id == Contact.id)
        if campaign_id is not None:
            query = query.where(CampaignContact.campaign_id == campaign_id)
            count_query = count_query.where(CampaignContact.campaign_id == campaign_id)
        if state is not None:
            query = query.where(CampaignContact.state == state)
            count_query = count_query.where(CampaignContact.state == state)

    if has_email is True:
        query = query.where(Contact.email.is_not(None))
        count_query = count_query.where(Contact.email.is_not(None))
    elif has_email is False:
        query = query.where(Contact.email.is_(None))
        count_query = count_query.where(Contact.email.is_(None))

    total = session.scalar(count_query) or 0
    contacts = list(
        session.scalars(
            query.distinct().order_by(Contact.created_at.desc()).limit(limit).offset(offset)
        ).all()
    )
    return contacts, total


@dataclass
class ContactDetail:
    """Everything the contact inspection page shows, from real records."""

    contact: Contact
    memberships: list[tuple[CampaignContact, Campaign]] = field(default_factory=list)
    provenance: list[ProvenanceRecord] = field(default_factory=list)
    observations: list[tuple[ImportRowValidation, ImportRow, ImportBatch]] = field(
        default_factory=list
    )
    active_suppression: Suppression | None = None
    ambiguity_notes: list[str] = field(default_factory=list)


def get_contact_detail(session: Session, contact_id: uuid.UUID) -> ContactDetail | None:
    """Load one contact with provenance, import history, memberships, suppression."""

    contact = session.get(Contact, contact_id)
    if contact is None:
        return None

    detail = ContactDetail(contact=contact)

    detail.memberships = [
        (membership, campaign)
        for membership, campaign in session.execute(
            select(CampaignContact, Campaign)
            .join(Campaign, Campaign.id == CampaignContact.campaign_id)
            .where(CampaignContact.contact_id == contact_id)
            .order_by(CampaignContact.created_at.desc())
        ).all()
    ]
    detail.provenance = list(
        session.scalars(
            select(ProvenanceRecord)
            .where(ProvenanceRecord.contact_id == contact_id)
            .order_by(ProvenanceRecord.observed_at.desc())
        ).all()
    )
    detail.observations = [
        (validation, row, batch)
        for validation, row, batch in session.execute(
            select(ImportRowValidation, ImportRow, ImportBatch)
            .join(ImportRow, ImportRow.id == ImportRowValidation.import_row_id)
            .join(ImportBatch, ImportBatch.id == ImportRow.batch_id)
            .where(ImportRowValidation.contact_id == contact_id)
            .order_by(ImportRow.created_at.desc())
        ).all()
    ]
    detail.ambiguity_notes = [
        validation.note
        for validation, _row, _batch in detail.observations
        if validation.note is not None
    ]

    # Active suppression state from the authoritative ledger.
    conditions = []
    if contact.email:
        conditions.append(
            (Suppression.suppression_type == SuppressionType.EMAIL)
            & (Suppression.value == contact.email.lower())
        )
    if contact.company_domain:
        conditions.append(
            (Suppression.suppression_type == SuppressionType.DOMAIN)
            & (Suppression.value == contact.company_domain.lower())
        )
    if conditions:
        combined = conditions[0]
        for extra in conditions[1:]:
            combined = combined | extra
        detail.active_suppression = session.scalars(select(Suppression).where(combined)).first()

    return detail
