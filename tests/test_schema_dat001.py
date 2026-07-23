"""Schema tests for the DAT-001 core-schema completion.

Representation-only: these assert relationships, uniqueness, duplicate protection,
draft/approval linkage, the separation of the three email-evidence categories,
and CSV/XLSX import-batch metadata. No later-phase behaviour is exercised.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from app.db.base import Base
from app.models.campaign import Campaign
from app.models.company import Company
from app.models.contact import Contact
from app.models.draft import DraftApproval, DraftVersion
from app.models.email_evidence import (
    DomainPatternObservation,
    ExactEmailVerification,
    MailDomainObservation,
)
from app.models.enums import (
    ApprovalStatus,
    EmailVerificationResult,
    ImportBatchStatus,
    ImportSourceFormat,
    InsightSubject,
    ScoreType,
)
from app.models.external_event import ExternalEvent
from app.models.import_batch import ImportBatch, ImportRow
from app.models.insight import Insight, InsightEvidence
from app.models.score import Score, ScoreComponent, ScoreEvidence
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


def _contact(db: Session, *, email: str = "person@acme.example") -> Contact:
    contact = Contact(
        first_name="Ada",
        last_name="Lovelace",
        company_name="Acme",
        company_domain="acme.example",
        email=email,
        natural_key="ada|lovelace|acme.example",
    )
    db.add(contact)
    db.flush()
    return contact


def _campaign(db: Session, name: str = "Schema Test") -> Campaign:
    campaign = Campaign(name=name)
    db.add(campaign)
    db.flush()
    return campaign


# --- New tables are registered on the metadata ------------------------------


def test_all_dat001_tables_registered() -> None:
    expected = {
        "companies",
        "exact_email_verifications",
        "domain_pattern_observations",
        "mail_domain_observations",
        "insights",
        "insight_evidence",
        "scores",
        "score_components",
        "score_evidence",
        "draft_versions",
        "draft_approvals",
        "external_events",
    }
    assert expected <= set(Base.metadata.tables)


# --- Relationships ----------------------------------------------------------


def test_score_relationships_persist(db_session: Session) -> None:
    contact = _contact(db_session)
    campaign = _campaign(db_session)
    insight = Insight(subject=InsightSubject.CONTACT, contact_id=contact.id, claim="uses widgets")
    db_session.add(insight)
    db_session.flush()

    score = Score(
        contact_id=contact.id,
        campaign_id=campaign.id,
        score_type=ScoreType.INITIAL_FIT,
        rule_version="v1",
        total=85,
        reason="strong fit",
        calculated_at=datetime.now(UTC),
    )
    db_session.add(score)
    db_session.flush()
    db_session.add(ScoreComponent(score_id=score.id, name="company_fit", value=25, weight=25))
    db_session.add(ScoreEvidence(score_id=score.id, insight_id=insight.id))
    db_session.flush()

    assert db_session.get(Score, score.id) is not None
    components = db_session.scalars(
        select(ScoreComponent).where(ScoreComponent.score_id == score.id)
    ).all()
    assert len(components) == 1


def test_score_requires_existing_contact(db_session: Session) -> None:
    import uuid

    score = Score(
        contact_id=uuid.uuid4(),  # nonexistent
        score_type=ScoreType.INITIAL_FIT,
        rule_version="v1",
        total=10,
        calculated_at=datetime.now(UTC),
    )
    db_session.add(score)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_insight_evidence_linkage(db_session: Session) -> None:
    contact = _contact(db_session)
    insight = Insight(
        subject=InsightSubject.CONTACT,
        contact_id=contact.id,
        claim="expanding into EU",
        source_url="https://example.com/news",
        retrieved_at=datetime.now(UTC),
        confidence=0.8,
        freshness_at=datetime.now(UTC),
    )
    db_session.add(insight)
    db_session.flush()
    db_session.add(
        InsightEvidence(insight_id=insight.id, source_url="https://example.com/news", excerpt="…")
    )
    db_session.flush()
    refs = db_session.scalars(
        select(InsightEvidence).where(InsightEvidence.insight_id == insight.id)
    ).all()
    assert len(refs) == 1


# --- Uniqueness constraints -------------------------------------------------


def test_company_domain_unique_when_present(db_session: Session) -> None:
    db_session.add(Company(name="Acme One", domain="acme.example"))
    db_session.flush()
    db_session.add(Company(name="Acme Two", domain="acme.example"))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_multiple_companies_may_have_no_domain(db_session: Session) -> None:
    db_session.add(Company(name="No Domain A"))
    db_session.add(Company(name="No Domain B"))
    db_session.flush()  # partial unique index: NULL domains do not collide
    assert db_session.scalar(select(func.count()).select_from(Company)) == 2


def test_score_component_name_unique_per_score(db_session: Session) -> None:
    contact = _contact(db_session)
    score = Score(
        contact_id=contact.id,
        score_type=ScoreType.INITIAL_FIT,
        rule_version="v1",
        total=50,
        calculated_at=datetime.now(UTC),
    )
    db_session.add(score)
    db_session.flush()
    db_session.add(ScoreComponent(score_id=score.id, name="timing", value=10))
    db_session.flush()
    db_session.add(ScoreComponent(score_id=score.id, name="timing", value=15))
    with pytest.raises(IntegrityError):
        db_session.flush()


# --- Duplicate external-event rejection -------------------------------------


def test_duplicate_external_event_rejected(db_session: Session) -> None:
    db_session.add(
        ExternalEvent(
            provider="saleshandy",
            external_event_id="evt_123",
            event_type="email.delivered",
            received_at=datetime.now(UTC),
            payload={"ok": True},
        )
    )
    db_session.flush()
    db_session.add(
        ExternalEvent(
            provider="saleshandy",
            external_event_id="evt_123",  # same provider + id
            event_type="email.delivered",
            received_at=datetime.now(UTC),
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_same_event_id_across_providers_allowed(db_session: Session) -> None:
    db_session.add(
        ExternalEvent(
            provider="saleshandy",
            external_event_id="shared",
            event_type="x",
            received_at=datetime.now(UTC),
        )
    )
    db_session.add(
        ExternalEvent(
            provider="millionverifier",
            external_event_id="shared",
            event_type="x",
            received_at=datetime.now(UTC),
        )
    )
    db_session.flush()
    assert db_session.scalar(select(func.count()).select_from(ExternalEvent)) == 2


# --- Draft-version and approval linkage -------------------------------------


def test_draft_version_and_approval_linkage(db_session: Session) -> None:
    contact = _contact(db_session)
    campaign = _campaign(db_session)
    v1 = DraftVersion(
        contact_id=contact.id,
        campaign_id=campaign.id,
        version_number=1,
        subject="Hello",
        body="Body v1",
    )
    db_session.add(v1)
    db_session.flush()
    approval = DraftApproval(
        draft_version_id=v1.id,
        approved_by="operator@vmr.example",
        approved_at=datetime.now(UTC),
        status=ApprovalStatus.APPROVED,
    )
    db_session.add(approval)
    db_session.flush()

    fetched = db_session.get(DraftApproval, approval.id)
    assert fetched is not None
    assert fetched.draft_version_id == v1.id
    assert fetched.status is ApprovalStatus.APPROVED


def test_draft_version_requires_a_campaign(db_session: Session) -> None:
    contact = _contact(db_session)
    # campaign_id is required: a draft cannot be persisted without a campaign.
    draft = DraftVersion(contact_id=contact.id, version_number=1, subject="s", body="b")
    db_session.add(draft)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_draft_version_number_unique_per_contact_campaign(db_session: Session) -> None:
    contact = _contact(db_session)
    campaign = _campaign(db_session)
    db_session.add(
        DraftVersion(
            contact_id=contact.id,
            campaign_id=campaign.id,
            version_number=1,
            subject="a",
            body="b",
        )
    )
    db_session.flush()
    db_session.add(
        DraftVersion(
            contact_id=contact.id,
            campaign_id=campaign.id,
            version_number=1,  # duplicate version
            subject="c",
            body="d",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_one_approval_per_draft_version(db_session: Session) -> None:
    contact = _contact(db_session)
    campaign = _campaign(db_session)
    v1 = DraftVersion(
        contact_id=contact.id,
        campaign_id=campaign.id,
        version_number=1,
        subject="s",
        body="b",
    )
    db_session.add(v1)
    db_session.flush()
    db_session.add(
        DraftApproval(draft_version_id=v1.id, approved_by="op", approved_at=datetime.now(UTC))
    )
    db_session.flush()
    db_session.add(
        DraftApproval(draft_version_id=v1.id, approved_by="op2", approved_at=datetime.now(UTC))
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


# --- The three email-evidence categories are structurally distinct ----------


def test_three_email_evidence_categories_are_independent(db_session: Session) -> None:
    db_session.add(
        ExactEmailVerification(
            email="ada@acme.example",
            result=EmailVerificationResult.CATCH_ALL,
            provider="millionverifier",
            policy_version="safe-v1",
            checked_at=datetime.now(UTC),
        )
    )
    db_session.add(
        DomainPatternObservation(
            domain="acme.example",
            pattern="{first}.{last}",
            confidence=0.7,
            sample_email="grace.hopper@acme.example",
        )
    )
    db_session.add(
        MailDomainObservation(domain="acme.example", is_catch_all=True, mx_provider="google")
    )
    db_session.flush()

    assert db_session.scalar(select(func.count()).select_from(ExactEmailVerification)) == 1
    assert db_session.scalar(select(func.count()).select_from(DomainPatternObservation)) == 1
    assert db_session.scalar(select(func.count()).select_from(MailDomainObservation)) == 1
    # Catch-all evidence is represented as its own uncertainty, not a valid mailbox.
    ev = db_session.scalars(select(ExactEmailVerification)).one()
    assert ev.result is EmailVerificationResult.CATCH_ALL


def test_exact_verification_persists_policy_version(db_session: Session) -> None:
    db_session.add(
        ExactEmailVerification(
            email="grace@acme.example",
            result=EmailVerificationResult.VALID,
            provider="millionverifier",
            policy_version="safe-v2",
            checked_at=datetime.now(UTC),
        )
    )
    db_session.flush()
    ev = db_session.scalars(
        select(ExactEmailVerification).where(ExactEmailVerification.email == "grace@acme.example")
    ).one()
    assert ev.policy_version == "safe-v2"


def test_exact_verification_requires_policy_version(db_session: Session) -> None:
    # policy_version is required; omitting it is a not-null violation.
    db_session.add(
        ExactEmailVerification(
            email="omit@acme.example",
            result=EmailVerificationResult.UNKNOWN,
            provider="millionverifier",
            checked_at=datetime.now(UTC),
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


# --- Import batch/file metadata supports CSV and XLSX -----------------------


def test_import_batch_defaults_to_csv_format(db_session: Session) -> None:
    campaign = _campaign(db_session)
    batch = ImportBatch(
        campaign_id=campaign.id, content_hash="abc", status=ImportBatchStatus.COMPLETED
    )
    db_session.add(batch)
    db_session.flush()
    db_session.refresh(batch)
    assert batch.source_format is ImportSourceFormat.CSV  # server/default keeps CSV imports working


def test_import_batch_supports_xlsx_metadata_and_sheets(db_session: Session) -> None:
    campaign = _campaign(db_session)
    batch = ImportBatch(
        campaign_id=campaign.id,
        content_hash="xlsxhash",
        status=ImportBatchStatus.PENDING,
        source_format=ImportSourceFormat.XLSX,
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        parser_version="openpyxl-1",
        mapper_version="map-v1",
        filename="contacts.xlsx",
    )
    db_session.add(batch)
    db_session.flush()

    # The same row number on two different sheets is allowed (distinct sheets).
    db_session.add(
        ImportRow(batch_id=batch.id, row_number=1, sheet_index=0, sheet_name="Sheet1", raw_data={})
    )
    db_session.add(
        ImportRow(batch_id=batch.id, row_number=1, sheet_index=1, sheet_name="Sheet2", raw_data={})
    )
    db_session.flush()
    assert (
        db_session.scalar(
            select(func.count()).select_from(ImportRow).where(ImportRow.batch_id == batch.id)
        )
        == 2
    )


def test_duplicate_row_within_same_sheet_rejected(db_session: Session) -> None:
    campaign = _campaign(db_session)
    batch = ImportBatch(
        campaign_id=campaign.id, content_hash="h2", status=ImportBatchStatus.PENDING
    )
    db_session.add(batch)
    db_session.flush()
    db_session.add(ImportRow(batch_id=batch.id, row_number=1, sheet_index=0, raw_data={}))
    db_session.flush()
    db_session.add(ImportRow(batch_id=batch.id, row_number=1, sheet_index=0, raw_data={}))
    with pytest.raises(IntegrityError):
        db_session.flush()
