"""CAP-002 durable Capture Agent reporting and lineage contracts."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from app.api.deps import get_db
from app.core.config import get_settings
from app.main import create_app
from app.models.campaign import Campaign, CampaignContact
from app.models.capture_promotion import ContactCapturePromotion
from app.models.collection import Collection, CollectionMembership
from app.models.company import Company
from app.models.contact import Contact
from app.models.contact_capture import ContactCaptureNote
from app.models.contact_field_value import ContactFieldValue
from app.models.enums import (
    AgentIdentifier,
    AgentJobStatus,
    CampaignMembershipStatus,
    CampaignStatus,
    CaptureCampaignFilingStatus,
    CompanyResolutionOutcome,
    ContactPromotionOutcome,
    DedupMatchType,
    ImportBatchStatus,
    ImportRowOutcome,
    ImportSourceFormat,
    LinkedInSnapshotOutcome,
    PipelineStageStatus,
    SuppressionReason,
    SuppressionType,
)
from app.models.import_batch import ImportBatch, ImportRow, ImportRowError, ImportRowValidation
from app.models.linkedin_profile import LinkedInProfileSnapshot
from app.models.pipeline import (
    CampaignContactAgentState,
    CampaignContactSource,
    CaptureCampaignFiling,
    PipelineEvent,
)
from app.models.suppression import Suppression
from app.models.verification_job import AgentJob
from app.services import campaign_contacts
from app.services.agent_studio.capture_report import (
    CaptureReportState,
    CaptureSourceType,
    CaptureValidationOutcome,
    DurableCaptureReportReader,
)
from app.services.agent_studio.extensions import AGENT_STUDIO_MODULES
from app.services.agents.registry import PIPELINE_ORDER
from app.services.captures.execution_lineage import (
    SCHEMA_VERSION,
    record_import_row_execution,
    record_snapshot_execution,
)
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session


@pytest.fixture()
def capture_studio_client(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__AGENT_WORKBENCH", "true")
    get_settings.cache_clear()
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def _identity_job(campaign: Campaign, membership: CampaignContact, contact: Contact) -> AgentJob:
    now = datetime.now(UTC)
    return AgentJob(
        agent_id=AgentIdentifier.IDENTITY,
        idempotency_key=f"pipeline:{membership.id}:identity:v1",
        task_kind="advance_campaign_contact",
        campaign_id=campaign.id,
        campaign_contact_id=membership.id,
        contact_id=contact.id,
        entity_type="campaign_contact",
        entity_id=membership.id,
        status=AgentJobStatus.PENDING,
        attempts=0,
        max_attempts=3,
        input_reference={},
        next_run_at=now,
        created_at=now,
        updated_at=now,
    )


def _extension_subject(
    db: Session,
    *,
    outcome: LinkedInSnapshotOutcome = LinkedInSnapshotOutcome.EXACT_MATCH_UNCHANGED,
    filing_status: CaptureCampaignFilingStatus = CaptureCampaignFilingStatus.APPLIED,
) -> tuple[
    Campaign,
    Contact,
    CampaignContact,
    LinkedInProfileSnapshot,
    AgentJob,
    CollectionMembership,
]:
    domain = f"capture-{uuid.uuid4()}.example"
    campaign = Campaign(
        name=f"Capture report {uuid.uuid4()}",
        status=CampaignStatus.ACTIVE,
        execution_enabled=True,
    )
    company = Company(name="Historical Works", domain=domain)
    db.add_all([campaign, company])
    db.flush()
    contact = Contact(
        first_name="Ada",
        last_name="Lovelace",
        title="Captured Engineer",
        company_name=company.name,
        company_domain=domain,
        company_id=company.id,
        email=f"ada-{uuid.uuid4()}@{domain}",
        linkedin_url="https://www.linkedin.com/in/ada-historical",
        natural_key=f"ada|lovelace|{domain}",
    )
    db.add(contact)
    db.flush()
    captured_at = datetime.now(UTC) - timedelta(hours=2)
    snapshot = LinkedInProfileSnapshot(
        client_capture_id=f"cap002-{uuid.uuid4()}",
        content_hash=uuid.uuid4().hex,
        schema_version="linkedin-contact-capture/2.1.0",
        source="chrome-extension:linkedin-contact-capture",
        source_url="https://www.linkedin.com/in/ada-historical?token=secret#private",
        normalized_profile_url="https://www.linkedin.com/in/ada-historical",
        public_identifier="ada-historical",
        salesnav_member_id="member-123",
        capture_mode="linkedin_profile",
        source_surface="linkedin_profile",
        profile_url_source="observed",
        extraction_status="complete",
        captured_at=captured_at,
        payload={
            "current_employment_hint": {
                "company_name": "Captured Historical Works",
                "company_linkedin_url": (
                    "https://www.linkedin.com/company/historical?tracking=secret"
                ),
                "company_linkedin_id": "company-123",
                "role_location": "London",
                "job_title": "Captured Engineer",
            }
        },
        profile_fields={
            "first_name": "Ada",
            "last_name": "Lovelace",
            "full_name": "Ada Lovelace",
            "displayed_location": "Greater London",
        },
        operator_labels=["Captured label"],
        outcome=outcome,
        matched_contact_id=contact.id,
        reconciled_at=captured_at + timedelta(minutes=1),
        refresh_summary={
            "outcome": outcome.value,
            "matched_contact_id": str(contact.id),
            "suppression_reason": None,
        },
    )
    db.add(snapshot)
    db.flush()
    membership = CampaignContact(
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_capture_id=snapshot.id,
        source_kind="capture",
        next_stage=AgentIdentifier.IDENTITY,
    )
    db.add(membership)
    db.flush()
    state = CampaignContactAgentState(
        campaign_contact_id=membership.id,
        agent_id=AgentIdentifier.CAPTURE,
        status=PipelineStageStatus.COMPLETED,
    )
    source = CampaignContactSource(
        campaign_contact_id=membership.id,
        idempotency_key=f"capture:{snapshot.id}:{campaign.id}",
        source_type="capture",
        source_reference=str(snapshot.id),
        capture_id=snapshot.id,
        recorded_by="operator@example.test",
    )
    filing = CaptureCampaignFiling(
        capture_id=snapshot.id,
        requested_campaign_id=campaign.id,
        campaign_id=campaign.id,
        campaign_contact_id=(
            membership.id if filing_status is CaptureCampaignFilingStatus.APPLIED else None
        ),
        status=filing_status,
        attempts=1,
        applied_at=(
            datetime.now(UTC) if filing_status is CaptureCampaignFilingStatus.APPLIED else None
        ),
        error_code=(
            "campaign_contact_error"
            if filing_status is CaptureCampaignFilingStatus.FAILED
            else None
        ),
    )
    promotion = ContactCapturePromotion(
        capture_id=snapshot.id,
        company_outcome=CompanyResolutionOutcome.EXISTING_COMPANY_RESOLVED,
        contact_outcome=ContactPromotionOutcome.CONTACT_EXACT_MATCH_LINKED,
        resolved_company_id=company.id,
        resolved_domain=domain,
        promoted_contact_id=contact.id,
        promoted_by="operator@example.test",
        promoted_at=datetime.now(UTC),
        detail={"match_kind": "linked_by_url", "natural_key": contact.natural_key},
    )
    label = Collection(name="Captured label", slug=f"captured-{uuid.uuid4()}")
    db.add(label)
    db.flush()
    assignment = CollectionMembership(
        contact_id=contact.id,
        collection_id=label.id,
        source="capture",
        capture_id=snapshot.id,
    )
    note = ContactCaptureNote(
        capture_id=snapshot.id,
        contact_id=contact.id,
        scope="contact",
        note_text="Bounded operator note with https://example.test/?private=1",
        author="operator@example.test",
    )
    observation = ContactFieldValue(
        contact_id=contact.id,
        field_name="title",
        value="Captured Engineer",
        source_name="linkedin-contact-capture",
        source_reference=str(snapshot.id),
        observed_at=captured_at,
        policy_version="contact-field-freshness/1",
        is_current_winner=True,
    )
    identity = _identity_job(campaign, membership, contact)
    db.add_all([state, source, filing, promotion, assignment, note, observation, identity])
    db.flush()
    db.add(
        CampaignContactAgentState(
            campaign_contact_id=membership.id,
            agent_id=AgentIdentifier.IDENTITY,
            status=PipelineStageStatus.WAITING,
            latest_job_id=identity.id,
        )
    )
    db.flush()
    job = record_snapshot_execution(
        db,
        snapshot=snapshot,
        actor="operator@example.test",
    )
    db.flush()
    return campaign, contact, membership, snapshot, job, assignment


def test_complete_extension_report_pins_source_promotion_filing_and_identity(
    db_session: Session,
) -> None:
    campaign, contact, membership, snapshot, job, _ = _extension_subject(db_session)

    report = DurableCaptureReportReader(db_session).read_job(job.id)

    assert report is not None
    assert report.report_state is CaptureReportState.COMPLETE
    assert report.source.source_type is CaptureSourceType.EXTENSION
    assert report.source.snapshot_id == snapshot.id
    assert report.source.source_url == "https://www.linkedin.com/in/ada-historical"
    assert report.captured.person.first_name == "Ada"
    assert report.captured.employer.name == "Captured Historical Works"
    assert report.captured.note.present is True
    assert len(report.captured.note.content or "") <= 500
    assert "private=1" not in (report.captured.note.content or "")
    assert "https://example.test/" in (report.captured.note.content or "")
    assert report.captured.field_provenance[0].field == "title"
    assert report.validation.outcome is CaptureValidationOutcome.ACCEPTED
    assert report.duplicate.applied is True
    assert report.duplicate.selected_contact_id == contact.id
    assert report.promotion.contact_reused is True
    assert report.promotion.contact_id == contact.id
    assert report.filing.status == "applied"
    assert report.filing.membership_created is True
    assert report.filing.campaign_contact_id == membership.id
    assert report.filing.next_stage == "identity"
    assert report.identity_handoff.identity_job_id is not None
    assert report.identity_handoff.status == "queued"
    assert report.campaign_id == campaign.id
    assert job.result is not None and job.result["schema_version"] == SCHEMA_VERSION
    assert "payload" not in job.result


def test_partial_legacy_report_uses_only_immutable_source_evidence(db_session: Session) -> None:
    _, contact, membership, snapshot, _, _ = _extension_subject(db_session)
    legacy = AgentJob(
        agent_id=AgentIdentifier.CAPTURE,
        idempotency_key=f"legacy-capture:{uuid.uuid4()}:v1",
        task_kind="legacy_capture",
        campaign_id=membership.campaign_id,
        campaign_contact_id=membership.id,
        contact_id=contact.id,
        capture_id=snapshot.id,
        entity_type="linkedin_profile_snapshot",
        entity_id=snapshot.id,
        status=AgentJobStatus.SUCCEEDED,
        attempts=1,
        max_attempts=1,
        input_reference={},
        result={"contact_id": str(contact.id)},
    )
    db_session.add(legacy)
    db_session.flush()

    contact.first_name = "Current-only edit"
    db_session.add(
        Contact(
            first_name="Ada",
            last_name="Lovelace",
            company_name=contact.company_name,
            company_domain=contact.company_domain,
            natural_key=f"similar-but-not-lineage|{uuid.uuid4()}",
        )
    )
    db_session.flush()
    report = DurableCaptureReportReader(db_session).read_job(legacy.id)

    assert report is not None
    assert report.report_state is CaptureReportState.PARTIAL
    assert report.source.snapshot_id == snapshot.id
    assert report.captured.person.first_name == "Ada"
    assert report.promotion.contact_at_execution.first_name is None
    assert report.duplicate.candidate_contact_ids is None
    assert any("fuzzy or retrospective" in item for item in report.unavailable)
    assert any("predates" in item for item in report.unavailable)


def test_unavailable_report_for_job_without_execution_or_source(db_session: Session) -> None:
    job = AgentJob(
        agent_id=AgentIdentifier.CAPTURE,
        idempotency_key=f"capture-unavailable:{uuid.uuid4()}:v1",
        task_kind="legacy_capture",
        status=AgentJobStatus.SUCCEEDED,
        attempts=1,
        max_attempts=1,
        input_reference={},
    )
    db_session.add(job)
    db_session.flush()

    report = DurableCaptureReportReader(db_session).read_job(job.id)

    assert report is not None
    assert report.report_state is CaptureReportState.UNAVAILABLE
    assert report.source.source_type is CaptureSourceType.UNKNOWN


def test_missing_snapshot_link_and_source_version_mismatch_are_partial(
    db_session: Session,
) -> None:
    _, _, _, snapshot, complete_job, _ = _extension_subject(db_session)
    assert complete_job.result is not None
    missing = AgentJob(
        agent_id=AgentIdentifier.CAPTURE,
        idempotency_key=f"capture-missing-source:{uuid.uuid4()}:v1",
        task_kind="capture_intake",
        entity_type="linkedin_profile_snapshot",
        entity_id=uuid.uuid4(),
        status=AgentJobStatus.SUCCEEDED,
        attempts=1,
        max_attempts=1,
        input_reference={},
        result=dict(complete_job.result),
    )
    db_session.add(missing)
    db_session.flush()

    missing_report = DurableCaptureReportReader(db_session).read_job(missing.id)
    assert missing_report is not None
    assert missing_report.report_state is CaptureReportState.PARTIAL
    assert any("source record is unavailable" in item for item in missing_report.unavailable)
    assert missing_report.captured.person.first_name == "Ada"

    changed_result = dict(complete_job.result)
    raw_source = changed_result["source"]
    assert isinstance(raw_source, dict)
    changed_source = dict(raw_source)
    changed_source["schema_version"] = "linkedin-contact-capture/99.0.0"
    changed_result["source"] = changed_source
    complete_job.result = changed_result
    db_session.flush()
    mismatch_report = DurableCaptureReportReader(db_session).read_job(complete_job.id)
    assert mismatch_report is not None
    assert mismatch_report.report_state is CaptureReportState.PARTIAL
    assert mismatch_report.source.snapshot_id == snapshot.id
    assert any("source-schema version" in item for item in mismatch_report.unavailable)


def test_unfiled_pending_extension_preserves_source_without_identity_handoff(
    db_session: Session,
) -> None:
    snapshot = LinkedInProfileSnapshot(
        client_capture_id=f"cap002-pending-{uuid.uuid4()}",
        content_hash=uuid.uuid4().hex,
        schema_version="linkedin-contact-capture/2.1.0",
        source="chrome-extension:linkedin-contact-capture",
        source_url="https://www.linkedin.com/in/pending-person?private=true",
        normalized_profile_url="https://www.linkedin.com/in/pending-person",
        public_identifier="pending-person",
        capture_mode="linkedin_profile",
        source_surface="linkedin_profile",
        profile_url_source="observed",
        extraction_status="complete",
        captured_at=datetime.now(UTC),
        payload={
            "current_employment_hint": {
                "company_name": "Unresolved Employer",
                "job_title": "Researcher",
            }
        },
        profile_fields={"first_name": "Pending", "last_name": "Person"},
        outcome=LinkedInSnapshotOutcome.UNMATCHED_STAGED,
        refresh_summary={"outcome": "unmatched_staged"},
    )
    db_session.add(snapshot)
    db_session.flush()
    job = record_snapshot_execution(db_session, snapshot=snapshot, actor="operator")

    report = DurableCaptureReportReader(db_session).read_job(job.id)

    assert report is not None
    assert report.report_state is CaptureReportState.COMPLETE
    assert report.validation.outcome is CaptureValidationOutcome.PENDING
    assert report.filing.requested is False
    assert report.filing.status == "not_requested"
    assert report.identity_handoff.identity_job_id is None
    assert report.identity_handoff.reason == "no_campaign_membership"


@pytest.mark.parametrize(
    ("outcome", "match_type", "expected", "suppression_type"),
    [
        (ImportRowOutcome.ACCEPTED, None, CaptureValidationOutcome.ACCEPTED, None),
        (ImportRowOutcome.REJECTED, None, CaptureValidationOutcome.REJECTED, None),
        (
            ImportRowOutcome.DUPLICATE,
            DedupMatchType.EMAIL,
            CaptureValidationOutcome.DUPLICATE,
            None,
        ),
        (
            ImportRowOutcome.DUPLICATE,
            DedupMatchType.NATURAL_KEY,
            CaptureValidationOutcome.DUPLICATE,
            None,
        ),
        (
            ImportRowOutcome.SUPPRESSED,
            None,
            CaptureValidationOutcome.SUPPRESSED,
            SuppressionType.EMAIL,
        ),
        (
            ImportRowOutcome.SUPPRESSED,
            None,
            CaptureValidationOutcome.SUPPRESSED,
            SuppressionType.DOMAIN,
        ),
        (ImportRowOutcome.AMBIGUOUS, None, CaptureValidationOutcome.AMBIGUOUS, None),
    ],
)
def test_import_source_outcomes_are_typed_and_raw_projection_is_bounded(
    db_session: Session,
    outcome: ImportRowOutcome,
    match_type: DedupMatchType | None,
    expected: CaptureValidationOutcome,
    suppression_type: SuppressionType | None,
) -> None:
    campaign = Campaign(
        name=f"Import capture {uuid.uuid4()}",
        status=CampaignStatus.ACTIVE,
    )
    db_session.add(campaign)
    db_session.flush()
    batch = ImportBatch(
        campaign_id=campaign.id,
        filename="contacts.csv",
        content_hash=uuid.uuid4().hex,
        status=ImportBatchStatus.COMPLETED,
        source_format=ImportSourceFormat.CSV,
        parser_version="csv-1",
        source_name="Authorized export",
    )
    db_session.add(batch)
    db_session.flush()
    row = ImportRow(
        batch_id=batch.id,
        row_number=7,
        raw_data={
            "first_name": "Raw Ada",
            "last_name": "Raw Lovelace",
            "company_name": "Raw Analytical",
            "company_domain": "analytical.example",
            "email": "raw@analytical.example",
            "source_reference": "https://export.example/row?token=private#fragment",
            "exported_by": "Authorization: Bearer secret-token",
            "private_blob": "must never leave the source store",
        },
    )
    db_session.add(row)
    db_session.flush()
    contact = None
    membership = None
    if outcome in {ImportRowOutcome.ACCEPTED, ImportRowOutcome.DUPLICATE}:
        contact = Contact(
            first_name="Normalized Ada",
            last_name="Lovelace",
            company_name="Analytical",
            company_domain="analytical.example",
            email=f"{uuid.uuid4()}@analytical.example",
            natural_key=f"ada|lovelace|{uuid.uuid4()}",
        )
        db_session.add(contact)
        db_session.flush()
        membership = CampaignContact(campaign_id=campaign.id, contact_id=contact.id)
        db_session.add(membership)
        db_session.flush()
        db_session.add(
            CampaignContactSource(
                campaign_contact_id=membership.id,
                idempotency_key=f"import:{row.id}",
                source_type="import",
                source_reference=str(batch.id),
                import_batch_id=batch.id,
            )
        )
    suppression = None
    if suppression_type is not None:
        suppression = Suppression(
            suppression_type=suppression_type,
            value=(
                "raw@analytical.example"
                if suppression_type is SuppressionType.EMAIL
                else "analytical.example"
            ),
            reason=SuppressionReason.LEGAL_COMPLIANCE,
            is_active=True,
        )
        db_session.add(suppression)
        db_session.flush()
    validation = ImportRowValidation(
        import_row_id=row.id,
        outcome=outcome,
        contact_id=contact.id if contact else None,
        match_type=match_type,
        suppression_id=suppression.id if suppression else None,
        normalized_data=(
            {
                "first_name": "Normalized Ada",
                "last_name": "Lovelace",
                "company_name": "Analytical",
                "company_domain": "analytical.example",
                "email": "raw@analytical.example",
            }
            if outcome is not ImportRowOutcome.REJECTED
            else None
        ),
        note="exact deterministic outcome",
    )
    db_session.add(validation)
    if outcome is ImportRowOutcome.REJECTED:
        db_session.add(
            ImportRowError(
                import_row_id=row.id,
                column_name="company_domain",
                code="invalid_domain",
                message="row 7: invalid company domain",
            )
        )
    db_session.flush()
    contacts_before = db_session.scalar(select(func.count()).select_from(Contact))
    job = record_import_row_execution(
        db_session,
        batch=batch,
        row=row,
        validation=validation,
        actor="importer",
        membership_created=(True if membership else None),
    )
    contacts_after = db_session.scalar(select(func.count()).select_from(Contact))

    report = DurableCaptureReportReader(db_session).read_job(job.id)

    assert report is not None
    assert report.report_state is (
        CaptureReportState.PARTIAL
        if outcome is ImportRowOutcome.AMBIGUOUS
        else CaptureReportState.COMPLETE
    )
    assert report.source.source_type is CaptureSourceType.IMPORT
    assert report.source.import_row_id == row.id
    assert report.validation.outcome is expected
    assert report.captured.person.first_name == "Raw Ada"
    normalized_report = dict(report.captured.normalized_fields)
    assert "private_blob" not in normalized_report
    assert report.source.import_source_reference == "https://export.example/row"
    assert "secret-token" not in (report.source.import_exported_by or "")
    assert db_session.get(ImportRow, row.id) is row
    assert contacts_before == contacts_after
    if match_type:
        assert contact is not None
        assert report.duplicate.match_type == match_type.value
        assert report.duplicate.no_new_contact is True
        assert report.duplicate.candidate_contact_ids == (contact.id,)
        assert report.promotion.contact_reused is True
    if outcome is ImportRowOutcome.ACCEPTED:
        assert report.promotion.contact_created is True
    if outcome in {ImportRowOutcome.REJECTED, ImportRowOutcome.AMBIGUOUS}:
        assert report.promotion.contact_id is None
    if outcome is ImportRowOutcome.SUPPRESSED:
        assert suppression_type is not None
        assert report.suppression.dimension == suppression_type.value
        assert report.suppression.reason == "legal_compliance"
        assert report.filing.status == "failed"
        assert report.suppression.blocked_filing is True
    if outcome is ImportRowOutcome.AMBIGUOUS:
        assert report.duplicate.candidate_contact_ids is None
        assert any("ambiguity candidate ledger" in item for item in report.unavailable)


@pytest.mark.parametrize(
    ("source_type", "expected_source"),
    [
        ("manual", CaptureSourceType.MANUAL),
        ("api", CaptureSourceType.API),
    ],
)
def test_manual_and_api_enrollment_record_reuse_and_identity_boundary(
    db_session: Session,
    source_type: str,
    expected_source: CaptureSourceType,
) -> None:
    campaign = Campaign(
        name=f"Manual capture {uuid.uuid4()}",
        status=CampaignStatus.ACTIVE,
        execution_enabled=True,
    )
    contact = Contact(
        first_name="Grace",
        last_name="Hopper",
        company_name="Navy",
        company_domain="navy.example",
        natural_key=f"grace|hopper|{uuid.uuid4()}",
    )
    db_session.add_all([campaign, contact])
    db_session.flush()

    enrolled = campaign_contacts.enrol_contact(
        db_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type=source_type,
        source_reference="contacts-page-selection",
        actor="operator",
        enqueue=True,
        desired_stage=AgentIdentifier.IDENTITY,
    )
    job = db_session.scalars(
        select(AgentJob).where(
            AgentJob.agent_id == AgentIdentifier.CAPTURE,
            AgentJob.campaign_contact_id == enrolled.membership.id,
        )
    ).one()
    report = DurableCaptureReportReader(db_session).read_job(job.id)

    assert report is not None
    assert report.source.source_type is expected_source
    assert report.validation.outcome is CaptureValidationOutcome.ACCEPTED
    assert report.promotion.contact_reused is True
    assert report.filing.membership_created is True
    assert enrolled.queued_job is not None
    assert report.identity_handoff.identity_job_id == enrolled.queued_job.id

    contact.first_name = "Current Grace"
    same_source = campaign_contacts.enrol_contact(
        db_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type=source_type,
        source_reference="contacts-page-selection",
        actor="operator",
        enqueue=True,
        desired_stage=AgentIdentifier.IDENTITY,
    )
    same_source_jobs = db_session.scalars(
        select(AgentJob).where(
            AgentJob.agent_id == AgentIdentifier.CAPTURE,
            AgentJob.campaign_contact_id == enrolled.membership.id,
        )
    ).all()
    assert same_source.source_created is False
    assert len(same_source_jobs) == 1
    unchanged_report = DurableCaptureReportReader(db_session).read_job(job.id)
    assert unchanged_report is not None
    assert unchanged_report.promotion.contact_at_execution.first_name == "Grace"

    replay_key = f"cap002-{source_type}-{uuid.uuid4()}"
    replay = campaign_contacts.enrol_contact(
        db_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type=source_type,
        source_reference="second-explicit-source",
        idempotency_key=replay_key,
        actor="operator",
        enqueue=True,
        desired_stage=AgentIdentifier.IDENTITY,
    )
    replay_source = db_session.scalars(
        select(CampaignContactSource).where(CampaignContactSource.idempotency_key == replay_key)
    ).one()
    replay_job = db_session.scalars(
        select(AgentJob).where(
            AgentJob.agent_id == AgentIdentifier.CAPTURE,
            AgentJob.entity_type == "campaign_contact_source",
            AgentJob.entity_id == replay_source.id,
        )
    ).one()
    assert replay.created is False
    assert replay_job.id != job.id
    replay_report = DurableCaptureReportReader(db_session).read_job(replay_job.id)
    assert replay_report is not None
    assert replay_report.filing.membership_created is False
    assert replay_report.filing.membership_reused is True


def test_suppressed_manual_filing_is_recorded_but_identity_is_not_enqueued(
    db_session: Session,
) -> None:
    campaign = Campaign(
        name=f"Suppressed manual capture {uuid.uuid4()}",
        status=CampaignStatus.ACTIVE,
        execution_enabled=True,
    )
    email = f"suppressed-{uuid.uuid4()}@blocked.example"
    contact = Contact(
        first_name="Suppressed",
        last_name="Person",
        company_name="Blocked",
        company_domain="blocked.example",
        email=email,
        natural_key=f"suppressed|person|{uuid.uuid4()}",
    )
    suppression = Suppression(
        suppression_type=SuppressionType.EMAIL,
        value=email,
        reason=SuppressionReason.OPT_OUT,
        is_active=True,
    )
    db_session.add_all([campaign, contact, suppression])
    db_session.flush()
    enrolled = campaign_contacts.enrol_contact(
        db_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        actor="operator",
        enqueue=True,
    )
    job = db_session.scalars(
        select(AgentJob).where(
            AgentJob.agent_id == AgentIdentifier.CAPTURE,
            AgentJob.campaign_contact_id == enrolled.membership.id,
        )
    ).one()

    report = DurableCaptureReportReader(db_session).read_job(job.id)

    assert report is not None
    assert report.validation.outcome is CaptureValidationOutcome.SUPPRESSED
    assert report.suppression.applied is True
    assert report.suppression.dimension == "email"
    assert report.suppression.reason == "opt_out"
    assert report.suppression.suppression_id == suppression.id
    assert report.suppression.blocked_filing is False
    assert report.filing.status == "applied"
    assert report.identity_handoff.identity_job_id is None
    assert enrolled.queued_job is None


def test_historical_truth_does_not_change_after_edit_merge_labels_or_suppression(
    db_session: Session,
) -> None:
    _, original, _, _, job, old_assignment = _extension_subject(db_session)
    current_company = Company(name="Current Canonical Company", domain="current.example")
    db_session.add(current_company)
    db_session.flush()
    survivor = Contact(
        first_name="Current",
        last_name="Survivor",
        title="Current Title",
        company_name="Current Company",
        company_domain="current.example",
        company_id=current_company.id,
        email=f"survivor-{uuid.uuid4()}@current.example",
        natural_key=f"current|survivor|{uuid.uuid4()}",
    )
    new_label = Collection(name="Current label", slug=f"current-{uuid.uuid4()}")
    db_session.add_all([survivor, new_label])
    db_session.flush()
    original.first_name = "Edited historical row"
    original.title = "Edited current title"
    original.merged_into_id = survivor.id
    db_session.delete(old_assignment)
    db_session.add(
        CollectionMembership(
            contact_id=survivor.id,
            collection_id=new_label.id,
            source="manual",
        )
    )
    db_session.add(
        Suppression(
            suppression_type=SuppressionType.DOMAIN,
            value="current.example",
            reason=SuppressionReason.CUSTOMER,
            is_active=True,
        )
    )
    db_session.flush()

    report = DurableCaptureReportReader(db_session).read_job(job.id)

    assert report is not None
    assert report.promotion.contact_at_execution.first_name == "Ada"
    assert report.promotion.contact_at_execution.title == "Captured Engineer"
    assert report.captured.labels == ("Captured label",)
    assert report.suppression.applied is False
    assert report.current.historical_contact_record.first_name == "Edited historical row"
    assert report.current.current_survivor.contact_id == survivor.id
    assert report.current.current_survivor.first_name == "Current"
    assert report.current.current_company_name == "Current Canonical Company"
    assert report.current.current_labels == ("Current label",)
    assert report.current.suppression_applied is True
    assert report.current.suppression_reason == "customer"


def test_extension_promotion_ambiguity_exposes_only_persisted_candidates(
    db_session: Session,
) -> None:
    _, original, _, snapshot, _, _ = _extension_subject(db_session)
    other = Contact(
        first_name="Ada",
        last_name="Lovelace",
        company_name=original.company_name,
        company_domain=original.company_domain,
        natural_key=f"ambiguous|{uuid.uuid4()}",
    )
    db_session.add(other)
    db_session.flush()
    promotion = db_session.scalars(
        select(ContactCapturePromotion).where(ContactCapturePromotion.capture_id == snapshot.id)
    ).one()
    promotion.contact_outcome = ContactPromotionOutcome.CONTACT_IDENTITY_AMBIGUOUS
    promotion.promoted_contact_id = None
    promotion.blocked_reason = "exact identifiers resolve to different Contacts"
    promotion.detail = {
        "ambiguous_contact_ids": [str(original.id), str(other.id)],
    }
    snapshot.outcome = LinkedInSnapshotOutcome.AMBIGUOUS_REVIEW
    db_session.flush()
    job = record_snapshot_execution(
        db_session,
        snapshot=snapshot,
        actor="operator",
        execution_kind="capture_promotion",
        material_outcome_generation=True,
    )

    report = DurableCaptureReportReader(db_session).read_job(job.id)

    assert report is not None
    assert report.validation.outcome is CaptureValidationOutcome.AMBIGUOUS
    assert report.promotion.promoted is False
    assert report.duplicate.applied is False
    assert report.duplicate.candidate_contact_ids == (original.id, other.id)


def test_related_material_execution_and_retry_are_distinct(db_session: Session) -> None:
    _, _, _, snapshot, intake_job, _ = _extension_subject(db_session)
    same_intake = record_snapshot_execution(db_session, snapshot=snapshot, actor="operator")
    promotion_job = record_snapshot_execution(
        db_session,
        snapshot=snapshot,
        actor="operator",
        execution_kind="capture_promotion",
        material_outcome_generation=True,
    )
    intake_job.attempts = 2
    db_session.flush()

    report = DurableCaptureReportReader(db_session).read_job(promotion_job.id)

    assert report is not None
    assert same_intake.id == intake_job.id
    assert len(report.related_executions) == 2
    assert {item.execution_kind for item in report.related_executions} == {
        "capture_intake",
        "capture_promotion",
    }
    assert (
        next(item for item in report.related_executions if item.job_id == intake_job.id).attempts
        == 2
    )


def test_reader_and_api_reject_wrong_agent_and_cross_owner_context(
    db_session: Session,
    capture_studio_client: TestClient,
) -> None:
    campaign, contact, membership, _, _, _ = _extension_subject(db_session)
    wrong = db_session.scalars(
        select(AgentJob).where(
            AgentJob.agent_id == AgentIdentifier.IDENTITY,
            AgentJob.campaign_contact_id == membership.id,
        )
    ).one()
    other_campaign = Campaign(name=f"Other owner {uuid.uuid4()}", status=CampaignStatus.ACTIVE)
    db_session.add(other_campaign)
    db_session.flush()
    cross = AgentJob(
        agent_id=AgentIdentifier.CAPTURE,
        idempotency_key=f"cross-owner:{uuid.uuid4()}:v1",
        task_kind="capture_enrollment",
        campaign_id=other_campaign.id,
        campaign_contact_id=membership.id,
        contact_id=contact.id,
        status=AgentJobStatus.SUCCEEDED,
        attempts=1,
        max_attempts=1,
        input_reference={},
        result={"schema_version": SCHEMA_VERSION, "lineage_complete": True},
    )
    db_session.add(cross)
    db_session.flush()
    reader = DurableCaptureReportReader(db_session)

    assert reader.read_job(wrong.id) is None
    assert reader.read_job(cross.id) is None
    for job_id in (wrong.id, cross.id):
        response = capture_studio_client.get(
            f"/api/admin/agent-studio/capture/jobs/{job_id}/report"
        )
        assert response.status_code == 404
        assert response.json() == {"detail": "Not found."}


def test_reader_sanitizes_errors_and_performs_no_writes(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, _, _, job, _ = _extension_subject(db_session)
    job.status = AgentJobStatus.FAILED
    job.error_class = "provider_failure"
    job.last_error = (
        "DATABASE_URL=postgresql://user:secret@host/operator "
        "/home/operator/private.py Authorization: Bearer secret-token"
    )
    job.error = {
        "message": job.last_error,
        "retryable": False,
        "detail": dict(job.result or {}),
    }
    job.result = None
    db_session.flush()
    before = {
        model: db_session.scalar(select(func.count()).select_from(model))
        for model in (AgentJob, Contact, CampaignContact, PipelineEvent)
    }
    from app.services.agents import jobs as agent_jobs
    from app.services.captures import campaign_filing, promotion

    monkeypatch.setattr(agent_jobs, "enqueue_job", lambda *args, **kwargs: pytest.fail("enqueue"))
    monkeypatch.setattr(promotion, "promote", lambda *args, **kwargs: pytest.fail("promote"))
    monkeypatch.setattr(
        campaign_filing, "apply_filing", lambda *args, **kwargs: pytest.fail("filing")
    )

    report = DurableCaptureReportReader(db_session).read_job(job.id)
    after = {model: db_session.scalar(select(func.count()).select_from(model)) for model in before}

    assert report is not None
    assert "secret-token" not in (report.error_detail or "")
    assert "postgresql://" not in (report.error_detail or "")
    assert "/home/operator" not in (report.error_detail or "")
    assert before == after
    assert not db_session.new and not db_session.dirty and not db_session.deleted


def test_html_and_api_share_report_and_safe_404s(
    capture_studio_client: TestClient, db_session: Session
) -> None:
    _, _, _, snapshot, job, _ = _extension_subject(db_session)
    before = db_session.scalar(select(func.count()).select_from(AgentJob))

    api = capture_studio_client.get(f"/api/admin/agent-studio/capture/jobs/{job.id}/report")
    html = capture_studio_client.get(f"/admin/agents/studio/capture?job={job.id}")
    after = db_session.scalar(select(func.count()).select_from(AgentJob))

    assert api.status_code == 200
    assert api.json()["job_id"] == str(job.id)
    assert api.json()["source"]["snapshot_id"] == str(snapshot.id)
    assert api.json()["report_state"] == "complete"
    assert html.status_code == 200
    assert str(job.id) in html.text
    assert "Captured Historical Works" in html.text
    assert "Historical execution truth versus current truth" in html.text
    assert before == after
    for value in ("not-a-uuid", str(uuid.uuid4())):
        response = capture_studio_client.get(f"/api/admin/agent-studio/capture/jobs/{value}/report")
        assert response.status_code == 404
        assert response.json() == {"detail": "Not found."}


def test_capture_studio_preserves_pipeline_and_authority_boundaries(
    capture_studio_client: TestClient,
) -> None:
    assert tuple(PIPELINE_ORDER) == (
        AgentIdentifier.CAPTURE,
        AgentIdentifier.IDENTITY,
        AgentIdentifier.COMPANY,
        AgentIdentifier.RESEARCH,
        AgentIdentifier.EMAIL,
        AgentIdentifier.VERIFICATION,
        AgentIdentifier.INSIGHTS,
        AgentIdentifier.PERSONALIZATION,
        AgentIdentifier.SENDING,
    )
    assert len(AgentIdentifier) == 9
    assert AGENT_STUDIO_MODULES[AgentIdentifier.CAPTURE].capabilities.reporting is True
    assert capture_studio_client.get("/app/agents/studio/capture").status_code == 404
    assert capture_studio_client.get("/app/capture/studio").status_code == 404
    page = capture_studio_client.get("/admin/agents/studio/capture")
    assert page.status_code == 200
    assert "Identity owns person-resolution decisions" in page.text
    assert "Company owns Company linking and canonical domains" in page.text
    assert "no edit, replay, retry, filing, promotion or enqueue action" in page.text


def test_filing_states_and_missing_historical_membership_remain_explicit(
    db_session: Session,
) -> None:
    _, _, _, _, applied_job, _ = _extension_subject(
        db_session, filing_status=CaptureCampaignFilingStatus.APPLIED
    )
    _, _, _, _, failed_job, _ = _extension_subject(
        db_session, filing_status=CaptureCampaignFilingStatus.FAILED
    )
    _, _, _, snapshot, pending_job, _ = _extension_subject(
        db_session, filing_status=CaptureCampaignFilingStatus.PENDING
    )
    report_applied = DurableCaptureReportReader(db_session).read_job(applied_job.id)
    report_failed = DurableCaptureReportReader(db_session).read_job(failed_job.id)
    report_pending = DurableCaptureReportReader(db_session).read_job(pending_job.id)
    assert report_applied is not None and report_applied.filing.status == "applied"
    assert report_failed is not None and report_failed.filing.status == "failed"
    assert report_pending is not None and report_pending.filing.status == "pending"
    assert report_failed.campaign_contact_id is None
    assert report_failed.filing.campaign_contact_id is None
    assert report_failed.current.current_campaign_contact_ids
    assert report_pending.campaign_contact_id is None
    assert report_pending.identity_handoff.identity_job_id is None

    legacy = AgentJob(
        agent_id=AgentIdentifier.CAPTURE,
        idempotency_key=f"legacy-filing:{uuid.uuid4()}:v1",
        task_kind="legacy_capture",
        capture_id=snapshot.id,
        entity_type="linkedin_profile_snapshot",
        entity_id=snapshot.id,
        status=AgentJobStatus.SUCCEEDED,
        attempts=1,
        max_attempts=1,
        input_reference={},
        result={"filing": {"requested": True, "status": "applied"}},
    )
    db_session.add(legacy)
    db_session.flush()
    legacy_report = DurableCaptureReportReader(db_session).read_job(legacy.id)
    assert legacy_report is not None
    assert legacy_report.filing.campaign_contact_id is None
    assert legacy_report.report_state is CaptureReportState.PARTIAL
    assert any("Campaign Contact lineage" in item for item in legacy_report.unavailable)


def test_current_membership_archive_does_not_rewrite_historical_filing(
    db_session: Session,
) -> None:
    _, _, membership, _, job, _ = _extension_subject(db_session)
    membership.membership_status = CampaignMembershipStatus.ARCHIVED
    db_session.flush()

    report = DurableCaptureReportReader(db_session).read_job(job.id)

    assert report is not None
    assert report.filing.membership_status == "active"
    assert report.current.exact_campaign_membership_status == "archived"
    assert report.current.current_campaign_memberships[0].membership_status == "archived"
