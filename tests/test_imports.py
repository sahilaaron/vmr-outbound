"""Staged CSV import integration tests (DAT-002, DAT-003, DAT-004, DAT-006).

These exercise the full pipeline against a real PostgreSQL instance using the
representative fixture, plus targeted small CSVs for suppression-after-import,
idempotency, and rollback behaviour.
"""

from __future__ import annotations

import pytest
from app.models.campaign import Campaign, CampaignContact
from app.models.contact import Contact
from app.models.enums import (
    ContactWorkflowState,
    ImportBatchStatus,
    ImportRowOutcome,
    SuppressionReason,
    SuppressionType,
)
from app.models.import_batch import ImportBatch, ImportRow, ImportRowError, ImportRowValidation
from app.models.provenance import ProvenanceRecord
from app.services.campaigns import create_campaign
from app.services.imports.importer import (
    BatchProvenance,
    FeatureDisabledError,
    ImportSummary,
    run_import,
)
from app.services.suppressions import add_suppression
from sqlalchemy import func, select
from sqlalchemy.orm import Session

pytestmark = pytest.mark.usefixtures("enable_csv_import")

ONE_CONTACT_CSV = (
    b"first_name,last_name,company_name,company_domain,email\n"
    b"Sam,Smith,Acme Widgets,acme.example,sam@acme.example\n"
)
# Byte-different re-export of the same contact (extra column) so it is processed
# rather than short-circuited by the idempotency check.
ONE_CONTACT_CSV_REEXPORT = (
    b"first_name,last_name,company_name,company_domain,email,note\n"
    b"Sam,Smith,Acme Widgets,acme.example,sam@acme.example,re-export\n"
)


def _seed_suppressions(db: Session) -> None:
    add_suppression(
        db,
        suppression_type=SuppressionType.EMAIL,
        value="optout@donotcontact.example",
        reason=SuppressionReason.OPT_OUT,
        source="fixture",
    )
    add_suppression(
        db,
        suppression_type=SuppressionType.DOMAIN,
        value="rival.example",
        reason=SuppressionReason.COMPETITOR,
        source="fixture",
    )


def _import_fixture(
    db: Session, csv_bytes: bytes, *, name: str = "Pilot 100"
) -> tuple[Campaign, ImportSummary]:
    campaign = create_campaign(db, name=name)
    summary = run_import(
        db,
        campaign_id=campaign.id,
        content=csv_bytes,
        filename="contacts.csv",
        provenance=BatchProvenance(source_name="Batch source", exported_by="operator@vmr.example"),
    )
    return campaign, summary


def test_import_summary_counts(db_session: Session, representative_csv: bytes) -> None:
    _seed_suppressions(db_session)
    _campaign, summary = _import_fixture(db_session, representative_csv)

    assert summary.status is ImportBatchStatus.COMPLETED
    assert summary.total_rows == 11
    assert summary.accepted_rows == 4
    assert summary.rejected_rows == 3
    assert summary.duplicate_rows == 2
    assert summary.suppressed_rows == 2
    assert summary.contacts_created == 4
    # Outcomes are mutually exclusive and cover every row (nothing dropped).
    assert (
        summary.accepted_rows
        + summary.rejected_rows
        + summary.duplicate_rows
        + summary.suppressed_rows
        == summary.total_rows
    )


def test_raw_rows_preserved_verbatim(db_session: Session, representative_csv: bytes) -> None:
    _seed_suppressions(db_session)
    _campaign, summary = _import_fixture(db_session, representative_csv)

    rows = db_session.scalars(select(ImportRow).where(ImportRow.batch_id == summary.batch_id)).all()
    assert len(rows) == 11
    # Every raw row has exactly one validation outcome.
    validation_count = db_session.scalar(
        select(func.count())
        .select_from(ImportRowValidation)
        .join(ImportRow, ImportRowValidation.import_row_id == ImportRow.id)
        .where(ImportRow.batch_id == summary.batch_id)
    )
    assert validation_count == 11

    grace_row = next(r for r in rows if r.row_number == 2)
    # Original whitespace and casing are retained on the immutable raw row.
    assert grace_row.raw_data["First_Name"] == "  grace  "
    assert grace_row.raw_data["Last_Name"] == " HOPPER "


def test_normalization_applied_to_contacts(db_session: Session, representative_csv: bytes) -> None:
    _seed_suppressions(db_session)
    _import_fixture(db_session, representative_csv)

    grace = db_session.scalars(
        select(Contact).where(Contact.email == "grace@compilercorp.example")
    ).one()
    assert grace.first_name == "grace"  # trimmed, case preserved
    assert grace.last_name == "HOPPER"
    assert grace.company_domain == "compilercorp.example"  # host normalized


def test_rejected_rows_keep_actionable_errors(
    db_session: Session, representative_csv: bytes
) -> None:
    _seed_suppressions(db_session)
    _campaign, summary = _import_fixture(db_session, representative_csv)

    errors = db_session.scalars(
        select(ImportRowError)
        .join(ImportRow, ImportRowError.import_row_id == ImportRow.id)
        .where(ImportRow.batch_id == summary.batch_id)
    ).all()
    codes = {e.code for e in errors}
    assert {"missing_required", "invalid_domain", "invalid_email"} <= codes
    assert all(e.message.startswith("row ") for e in errors)


def test_conservative_dedup_email_and_natural_key(
    db_session: Session, representative_csv: bytes
) -> None:
    _seed_suppressions(db_session)
    _import_fixture(db_session, representative_csv)

    # Ada appears twice by identical email -> exactly one contact.
    ada_count = db_session.scalar(
        select(func.count())
        .select_from(Contact)
        .where(Contact.email == "ada@analyticalengines.example")
    )
    assert ada_count == 1

    # Margaret Hamilton appears twice with no email, same natural key -> one contact.
    hamilton_count = db_session.scalar(
        select(func.count())
        .select_from(Contact)
        .where(Contact.natural_key == "margaret|hamilton|apollosw.example")
    )
    assert hamilton_count == 1

    duplicate_outcomes = db_session.scalar(
        select(func.count())
        .select_from(ImportRowValidation)
        .where(ImportRowValidation.outcome == ImportRowOutcome.DUPLICATE)
    )
    assert duplicate_outcomes == 2

    total_contacts = db_session.scalar(select(func.count()).select_from(Contact))
    assert total_contacts == 4


def test_distinct_email_never_merges_same_name(db_session: Session) -> None:
    # Two people, same name and company, different emails: must stay separate.
    csv_bytes = (
        b"first_name,last_name,company_name,company_domain,email\n"
        b"Chris,Lee,Acme,acme.example,chris.lee@acme.example\n"
        b"Chris,Lee,Acme,acme.example,c.lee@acme.example\n"
    )
    _campaign, summary = _import_fixture(db_session, csv_bytes)
    assert summary.contacts_created == 2
    assert summary.duplicate_rows == 0


def test_suppressed_identity_creates_no_contact_and_survives_reimport(
    db_session: Session, representative_csv: bytes
) -> None:
    _seed_suppressions(db_session)
    campaign, first = _import_fixture(db_session, representative_csv)

    # The suppressed email never becomes a contact.
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(Contact)
            .where(Contact.email == "optout@donotcontact.example")
        )
        == 0
    )

    # Re-importing a byte-different export of the same batch keeps it suppressed.
    reexport = representative_csv + b"\n"  # trailing newline -> different bytes
    second = run_import(
        db_session,
        campaign_id=campaign.id,
        content=reexport,
        filename="contacts-2.csv",
    )
    assert second.suppressed_rows == 2
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(Contact)
            .where(Contact.email == "optout@donotcontact.example")
        )
        == 0
    )


def test_suppression_after_import_blocks_reeligibility(db_session: Session) -> None:
    campaign, first = _import_fixture(db_session, ONE_CONTACT_CSV, name="Reeligibility")
    assert first.contacts_created == 1

    membership = db_session.scalars(select(CampaignContact)).one()
    assert membership.state is ContactWorkflowState.IMPORTED

    # The contact is later suppressed (e.g. a hard bounce), then re-appears.
    add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value="sam@acme.example",
        reason=SuppressionReason.HARD_BOUNCE,
    )
    second = run_import(
        db_session,
        campaign_id=campaign.id,
        content=ONE_CONTACT_CSV_REEXPORT,
        filename="reexport.csv",
    )
    assert second.suppressed_rows == 1

    db_session.refresh(membership)
    assert membership.state is ContactWorkflowState.SUPPRESSED


def test_idempotent_retry_of_identical_file(db_session: Session, representative_csv: bytes) -> None:
    _seed_suppressions(db_session)
    campaign, first = _import_fixture(db_session, representative_csv)
    contacts_after_first = db_session.scalar(select(func.count()).select_from(Contact))

    second = run_import(
        db_session,
        campaign_id=campaign.id,
        content=representative_csv,
        filename="contacts.csv",
    )
    assert second.reused_existing_batch is True
    assert second.batch_id == first.batch_id
    contacts_after_second = db_session.scalar(select(func.count()).select_from(Contact))
    assert contacts_after_second == contacts_after_first

    completed_batches = db_session.scalar(
        select(func.count())
        .select_from(ImportBatch)
        .where(ImportBatch.status == ImportBatchStatus.COMPLETED)
    )
    assert completed_batches == 1


def test_overlapping_reimport_dedups_without_new_contacts(db_session: Session) -> None:
    campaign, first = _import_fixture(db_session, ONE_CONTACT_CSV, name="Overlap")
    assert first.contacts_created == 1
    second = run_import(
        db_session,
        campaign_id=campaign.id,
        content=ONE_CONTACT_CSV_REEXPORT,
        filename="reexport.csv",
    )
    assert second.contacts_created == 0
    assert second.duplicate_rows == 1
    assert db_session.scalar(select(func.count()).select_from(Contact)) == 1


def test_rollback_on_failure_preserves_raw_rows_and_marks_failed(
    db_session: Session, representative_csv: bytes
) -> None:
    _seed_suppressions(db_session)
    campaign = create_campaign(db_session, name="Failing import")

    def _boom() -> None:
        raise RuntimeError("simulated processing failure")

    with pytest.raises(RuntimeError, match="simulated processing failure"):
        run_import(
            db_session,
            campaign_id=campaign.id,
            content=representative_csv,
            filename="contacts.csv",
            _fault=_boom,
        )

    batch = db_session.scalars(
        select(ImportBatch).where(ImportBatch.campaign_id == campaign.id)
    ).one()
    assert batch.status is ImportBatchStatus.FAILED
    assert batch.error_detail is not None

    # Raw capture (stage 1) survived; processing artefacts (stage 2) rolled back.
    raw_rows = db_session.scalar(
        select(func.count()).select_from(ImportRow).where(ImportRow.batch_id == batch.id)
    )
    assert raw_rows == 11
    assert db_session.scalar(select(func.count()).select_from(Contact)) == 0
    assert db_session.scalar(select(func.count()).select_from(ImportRowValidation)) == 0
    assert db_session.scalar(select(func.count()).select_from(CampaignContact)) == 0


def test_provenance_recorded_for_accepted_contacts(
    db_session: Session, representative_csv: bytes
) -> None:
    _seed_suppressions(db_session)
    _import_fixture(db_session, representative_csv)

    ada = db_session.scalars(
        select(Contact).where(Contact.email == "ada@analyticalengines.example")
    ).one()
    provenance = db_session.scalars(
        select(ProvenanceRecord).where(ProvenanceRecord.contact_id == ada.id)
    ).all()
    assert len(provenance) >= 1
    record = provenance[0]
    assert record.observed_at is not None
    # Row-level provenance column overrides the batch default.
    assert record.source_name == "Sales Navigator export - 2026-07"
    assert record.exported_by == "operator@vmr.example"


def test_membership_created_and_not_duplicated(db_session: Session) -> None:
    campaign, _first = _import_fixture(db_session, ONE_CONTACT_CSV, name="Membership")
    run_import(
        db_session,
        campaign_id=campaign.id,
        content=ONE_CONTACT_CSV_REEXPORT,
        filename="reexport.csv",
    )
    memberships = db_session.scalars(
        select(CampaignContact).where(CampaignContact.campaign_id == campaign.id)
    ).all()
    assert len(memberships) == 1
    assert memberships[0].state is ContactWorkflowState.IMPORTED


def test_suppression_propagates_across_all_campaigns(db_session: Session) -> None:
    # The same contact belongs to two campaigns, both eligible.
    camp_a, _first = _import_fixture(db_session, ONE_CONTACT_CSV, name="Camp A")
    camp_b = create_campaign(db_session, name="Camp B")
    run_import(
        db_session,
        campaign_id=camp_b.id,
        content=ONE_CONTACT_CSV,
        filename="camp-b.csv",
    )
    memberships = db_session.scalars(select(CampaignContact)).all()
    assert len(memberships) == 2
    assert all(m.state is ContactWorkflowState.IMPORTED for m in memberships)

    # Suppress the identity, then re-observe it through campaign A only.
    add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value="sam@acme.example",
        reason=SuppressionReason.HARD_BOUNCE,
    )
    result = run_import(
        db_session,
        campaign_id=camp_a.id,
        content=ONE_CONTACT_CSV_REEXPORT,
        filename="camp-a-reexport.csv",
    )
    assert result.suppressed_rows == 1

    # BOTH memberships are suppressed, not only the campaign that was re-imported.
    for membership in memberships:
        db_session.refresh(membership)
    assert {m.campaign_id for m in memberships} == {camp_a.id, camp_b.id}
    assert all(m.state is ContactWorkflowState.SUPPRESSED for m in memberships)


def test_headerless_csv_is_rejected(db_session: Session) -> None:
    # No header line: the first data line is misread as a (pseudo) header.
    csv_bytes = (
        b"Ada,Lovelace,Analytical Engines,analyticalengines.example,ada@analyticalengines.example\n"
        b"Grace,Hopper,Compiler Corp,compilercorp.example,grace@compilercorp.example\n"
    )
    campaign = create_campaign(db_session, name="Headerless")
    summary = run_import(
        db_session, campaign_id=campaign.id, content=csv_bytes, filename="headerless.csv"
    )
    assert summary.status is ImportBatchStatus.FAILED
    assert summary.error_detail is not None
    assert summary.contacts_created == 0
    assert db_session.scalar(select(func.count()).select_from(Contact)) == 0
    # Meaningful raw evidence is still preserved (the misread data row).
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(ImportRow)
            .where(ImportRow.batch_id == summary.batch_id)
        )
        == 1
    )


def test_missing_required_header_is_rejected(db_session: Session) -> None:
    csv_bytes = (
        b"first_name,last_name,company_name,email\n"  # company_domain absent
        b"Ada,Lovelace,Analytical Engines,ada@analyticalengines.example\n"
    )
    campaign = create_campaign(db_session, name="Missing header")
    summary = run_import(
        db_session, campaign_id=campaign.id, content=csv_bytes, filename="missing-col.csv"
    )
    assert summary.status is ImportBatchStatus.FAILED
    assert summary.error_detail is not None
    assert "company_domain" in summary.error_detail
    assert summary.contacts_created == 0
    assert db_session.scalar(select(func.count()).select_from(Contact)) == 0


def test_header_only_csv_is_rejected(db_session: Session) -> None:
    csv_bytes = b"first_name,last_name,company_name,company_domain,email\n"  # header, no data
    campaign = create_campaign(db_session, name="Header only")
    summary = run_import(
        db_session, campaign_id=campaign.id, content=csv_bytes, filename="header-only.csv"
    )
    assert summary.status is ImportBatchStatus.FAILED
    assert summary.error_detail is not None
    assert summary.total_rows == 0
    assert summary.contacts_created == 0
    assert db_session.scalar(select(func.count()).select_from(Contact)) == 0


def test_import_requires_feature_flag(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings

    # Disable the flag despite the module-level usefixtures enabler.
    monkeypatch.delenv("FEATURES__CSV_IMPORT", raising=False)
    get_settings.cache_clear()

    campaign = create_campaign(db_session, name="No flag")
    with pytest.raises(FeatureDisabledError):
        run_import(
            db_session,
            campaign_id=campaign.id,
            content=ONE_CONTACT_CSV,
            filename="contacts.csv",
        )
    get_settings.cache_clear()
