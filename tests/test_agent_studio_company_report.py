"""CMP-003 durable Company Agent report and lineage contracts."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.models.campaign import Campaign, CampaignContact
from app.models.company import Company
from app.models.company_domain_resolution import CompanyDomainResolution
from app.models.contact import Contact
from app.models.enums import (
    AgentIdentifier,
    AgentJobStatus,
    CampaignStatus,
    DomainResolutionKind,
    DomainResolutionState,
    EnrichmentConfirmationStatus,
    EnrichmentLookupStatus,
)
from app.models.linkedin_profile import LinkedInProfileSnapshot
from app.models.salesnav_enrichment import SalesNavCompanyEnrichment
from app.models.verification_job import AgentJob
from app.services.agent_studio.company_report import (
    CompanyDomainOutcome,
    CompanyReportState,
    DurableCompanyReportReader,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session


def _job(
    *,
    agent: AgentIdentifier,
    campaign: Campaign,
    membership: CampaignContact,
    contact: Contact,
    capture: LinkedInProfileSnapshot,
    parent: AgentJob | None = None,
    status: AgentJobStatus = AgentJobStatus.SUCCEEDED,
    generation: int = 1,
) -> AgentJob:
    now = datetime.now(UTC)
    return AgentJob(
        agent_id=agent,
        idempotency_key=f"pipeline:{membership.id}:{agent.value}:v{generation}",
        task_kind="advance_campaign_contact",
        campaign_id=campaign.id,
        campaign_contact_id=membership.id,
        contact_id=contact.id,
        company_id=contact.company_id,
        capture_id=capture.id,
        parent_job_id=parent.id if parent else None,
        status=status,
        attempts=2,
        max_attempts=3,
        input_reference={},
        created_at=now - timedelta(minutes=1),
        updated_at=now,
        next_run_at=now,
        started_at=now - timedelta(seconds=30),
        finished_at=now if status in {AgentJobStatus.SUCCEEDED, AgentJobStatus.FAILED} else None,
    )


def _subject(
    db: Session,
    *,
    state: DomainResolutionState = DomainResolutionState.CONFIRMED,
    allow_provisional: bool = False,
) -> tuple[
    Campaign,
    Company,
    Contact,
    CampaignContact,
    LinkedInProfileSnapshot,
    CompanyDomainResolution,
    AgentJob,
]:
    domain = f"kiln-{uuid.uuid4()}.example"
    campaign = Campaign(
        name=f"Company report {uuid.uuid4()}",
        status=CampaignStatus.ACTIVE,
        execution_enabled=True,
        allow_provisional_domains=allow_provisional,
        settings_version=3,
    )
    company = Company(name="Kiln Systems", domain=domain)
    db.add_all([campaign, company])
    db.flush()
    contact = Contact(
        first_name="Ada",
        last_name="Lovelace",
        company_name=company.name,
        company_domain=domain,
        company_id=company.id,
        natural_key=f"ada|lovelace|{uuid.uuid4()}",
    )
    db.add(contact)
    db.flush()
    membership = CampaignContact(campaign_id=campaign.id, contact_id=contact.id)
    capture = LinkedInProfileSnapshot(
        client_capture_id=f"cmp003-{uuid.uuid4()}",
        content_hash=uuid.uuid4().hex,
        schema_version="test/1",
        source="test",
        source_url="https://www.linkedin.com/in/ada?token=secret",
        normalized_profile_url="https://www.linkedin.com/in/ada",
        extraction_status="complete",
        payload={
            "current_employment_hint": {
                "company_name": company.name,
                "company_linkedin_url": ("https://www.linkedin.com/company/kiln?tracking=secret"),
                "company_linkedin_id": "12345",
                "role_location": "London",
            }
        },
        profile_fields={"first_name": "Ada", "last_name": "Lovelace"},
        matched_contact_id=contact.id,
        captured_at=datetime.now(UTC) - timedelta(days=1),
    )
    db.add_all([membership, capture])
    db.flush()
    decision = CompanyDomainResolution(
        capture_id=capture.id,
        resolved_company_id=company.id,
        decision_number=1,
        is_current=True,
        state=state,
        decision_kind=DomainResolutionKind.AUTOMATIC,
        policy_version="company-domain-resolution/practical-v1",
        company_name_original=company.name,
        company_name_normalized="kiln systems",
        candidates=[
            {
                "domain": domain,
                "name": company.name,
                "rank": 1,
                "eligible": state is not DomainResolutionState.UNRESOLVED,
                "aligned": True,
                "alignment": "provider_name",
                "rejection_reason": None,
            },
            {
                "domain": domain,
                "name": "Duplicate normalized candidate",
                "rank": 2,
                "eligible": False,
                "aligned": False,
                "alignment": None,
                "rejection_reason": "duplicate_or_conflicting_candidate",
            },
        ],
        selected_domain=None if state is DomainResolutionState.UNRESOLVED else domain,
        selected_candidate=(
            None
            if state is DomainResolutionState.UNRESOLVED
            else {"domain": domain, "name": company.name, "rank": 1}
        ),
        provider="logo.dev",
        provider_rank=1 if state is not DomainResolutionState.UNRESOLVED else None,
        reasons=[
            "no_candidate_aligned_with_company_name"
            if state is DomainResolutionState.UNRESOLVED
            else "single_aligned_provider_candidate"
        ],
        warnings=(
            ["provisional_domain_authorizes_research_only"]
            if state is DomainResolutionState.PROVISIONAL
            else None
        ),
        provider_call_made=True,
        decided_by="test",
        decided_at=datetime.now(UTC) - timedelta(hours=1),
    )
    db.add(decision)
    db.flush()
    identity = _job(
        agent=AgentIdentifier.IDENTITY,
        campaign=campaign,
        membership=membership,
        contact=contact,
        capture=capture,
    )
    db.add(identity)
    db.flush()
    company_job = _job(
        agent=AgentIdentifier.COMPANY,
        campaign=campaign,
        membership=membership,
        contact=contact,
        capture=capture,
        parent=identity,
        status=(
            AgentJobStatus.PAUSED
            if state is DomainResolutionState.UNRESOLVED
            else AgentJobStatus.SUCCEEDED
        ),
    )
    historical = {
        "schema_version": "company-agent-report/1",
        "identity": {
            "match_key": "contact.company_id",
            "match_value": str(company.id),
            "candidate_company_ids": [str(company.id)],
            "selected_company_id": str(company.id),
            "company_action": "reused",
            "contact_link_action": "already_linked",
            "reason": "Reused the Contact's existing permanent Company association.",
            "evidence_references": [f"capture:{capture.id}"],
        },
        "historical_company": {
            "company_id": str(company.id),
            "name": company.name,
            "company_record_domain": domain,
            "canonical_domain": (None if state is DomainResolutionState.UNRESOLVED else domain),
            "domain_resolution_state": state.value,
        },
        "capture_domain_resolution_id": str(decision.id),
        "company_aggregate_domain_resolution_id": str(decision.id),
        "domain_resolution_source": "company_aggregate_decision",
        "conflict_kinds": [],
        "campaign_policy": {
            "allow_provisional_domains": allow_provisional,
            "campaign_settings_version": 3,
            "source": "execution_snapshot",
        },
        "continuation": {
            "action": "block"
            if state is DomainResolutionState.UNRESOLVED
            else "review_required"
            if state is DomainResolutionState.PROVISIONAL and not allow_provisional
            else "continue",
            "research_allowed": state is not DomainResolutionState.UNRESOLVED,
            "research_reason": f"Historical {state.value} Research decision.",
            "later_stages_allowed": (
                state is DomainResolutionState.CONFIRMED
                or (state is DomainResolutionState.PROVISIONAL and allow_provisional)
            ),
            "later_stages_reason": f"Historical {state.value} downstream decision.",
        },
        "company_id": str(company.id),
        "domain": domain,
        "domain_resolution_state": state.value,
    }
    if state is DomainResolutionState.UNRESOLVED:
        company_job.error_class = "company_domain_unresolved"
        company_job.last_error = "The company domain is unresolved."
        company_job.error = {
            "class": "company_domain_unresolved",
            "message": "The company domain is unresolved.",
            "retryable": True,
            "detail": historical,
        }
    else:
        company_job.result = historical
    db.add(company_job)
    db.flush()
    if state is not DomainResolutionState.UNRESOLVED:
        research = _job(
            agent=AgentIdentifier.RESEARCH,
            campaign=campaign,
            membership=membership,
            contact=contact,
            capture=capture,
            parent=company_job,
        )
        db.add(research)
    db.flush()
    return campaign, company, contact, membership, capture, decision, company_job


@pytest.mark.parametrize(
    ("state", "allow", "action"),
    [
        (DomainResolutionState.CONFIRMED, False, "continue"),
        (DomainResolutionState.PROVISIONAL, True, "continue"),
        (DomainResolutionState.PROVISIONAL, False, "review_required"),
        (DomainResolutionState.UNRESOLVED, False, "block"),
    ],
)
def test_exact_outcomes_and_campaign_policy_are_not_upgraded(
    db_session: Session,
    state: DomainResolutionState,
    allow: bool,
    action: str,
) -> None:
    campaign, _, _, _, _, _, job = _subject(db_session, state=state, allow_provisional=allow)
    campaign.allow_provisional_domains = not allow
    campaign.settings_version = 4
    db_session.flush()
    report = DurableCompanyReportReader(db_session).read_job(job.id)
    assert report is not None
    assert report.report_state is CompanyReportState.COMPLETE
    assert report.historical.domain_outcome is CompanyDomainOutcome(state.value)
    assert report.campaign_policy.historical_allow_provisional is allow
    assert report.campaign_policy.current_allow_provisional is not allow
    assert report.campaign_policy.action == action
    assert report.historical_domain_decision is not None
    assert report.historical_capture_decision is not None
    assert report.historical_company_aggregate_decision is not None
    assert report.historical_domain_decision.outcome is CompanyDomainOutcome(state.value)
    assert len(report.historical_domain_decision.candidates) == 2
    assert report.historical_domain_decision.candidates[0].normalized_domain == (
        report.historical_domain_decision.candidates[1].normalized_domain
    )
    assert report.identity is not None
    assert report.identity.company_action == "reused"
    assert any("creation provenance" in item for item in report.unavailable)


def test_historical_company_domain_and_decision_survive_current_corrections(
    db_session: Session,
) -> None:
    _, original, contact, _, capture, first, job = _subject(db_session)
    replacement = Company(name="Kiln Corrected", domain=f"corrected-{uuid.uuid4()}.example")
    db_session.add(replacement)
    db_session.flush()
    first.is_current = False
    first.superseded_at = datetime.now(UTC)
    db_session.flush()
    later = CompanyDomainResolution(
        capture_id=capture.id,
        resolved_company_id=replacement.id,
        decision_number=2,
        is_current=True,
        state=DomainResolutionState.CONFIRMED,
        decision_kind=DomainResolutionKind.OPERATOR_CORRECTION,
        policy_version="company-domain-resolution/practical-v1",
        candidates=None,
        selected_domain=replacement.domain,
        reasons=["operator_correction"],
        provider_call_made=False,
        decided_by="operator",
        decided_at=datetime.now(UTC),
    )
    contact.company_id = replacement.id
    db_session.add(later)
    db_session.flush()

    report = DurableCompanyReportReader(db_session).read_job(job.id)
    assert report is not None
    assert report.historical.company_id == original.id
    assert report.historical.canonical_domain == original.domain
    assert report.historical_domain_decision is not None
    assert report.historical_domain_decision.decision_id == first.id
    assert report.historical_domain_decision.is_current is False
    assert report.current_contact_company.company_id == replacement.id
    assert report.current_capture_decision is not None
    assert report.current_capture_decision.decision_id == later.id
    assert len(report.decision_history) == 2


def test_provider_only_is_current_evidence_and_never_confirmation(db_session: Session) -> None:
    _, _, _, _, capture, decision, job = _subject(db_session)
    db_session.delete(decision)
    enrichment = SalesNavCompanyEnrichment(
        capture_id=capture.id,
        company_key="kiln systems",
        company_name="Kiln Systems",
        lookup_status=EnrichmentLookupStatus.OK,
        candidates=[{"domain": "provider.example", "name": "Kiln", "rank": 1, "confidence": None}],
        provider="logo.dev",
        looked_up_at=datetime.now(UTC),
        confirmation_status=EnrichmentConfirmationStatus.UNCONFIRMED,
        model_lookup_status=EnrichmentLookupStatus.OK,
        model_domain="model.example",
        model_source_url="https://model.example/about?token=secret#fragment",
        model_note="found on the public company page at /root/private/file",
    )
    db_session.add(enrichment)
    db_session.flush()
    report = DurableCompanyReportReader(db_session).read_job(job.id)
    assert report is not None
    assert report.historical.domain_outcome is CompanyDomainOutcome.CONFIRMED
    assert report.historical_domain_decision is None
    assert report.current_capture_decision is None
    assert report.current_provider_result is not None
    assert report.current_provider_result.outcome is CompanyDomainOutcome.PROVIDER_ONLY
    assert report.current_provider_result.candidates[0].status == "provider_only"
    assert report.current_provider_result.candidates[1].source_reference == (
        "https://model.example/about"
    )
    assert "/root/" not in (report.current_provider_result.candidates[1].evidence or "")


def test_explicit_no_automatic_decision_is_complete_without_fabricating_a_ledger(
    db_session: Session,
) -> None:
    _, company, _, _, _, decision, job = _subject(db_session)
    db_session.delete(decision)
    assert job.result is not None
    result = dict(job.result)
    historical_company = dict(result["historical_company"])
    historical_company["domain_resolution_state"] = None
    historical_company["canonical_domain"] = company.domain
    result["historical_company"] = historical_company
    result["capture_domain_resolution_id"] = None
    result["company_aggregate_domain_resolution_id"] = None
    result["domain_resolution_source"] = "no_automatic_decision"
    job.result = result
    db_session.flush()
    report = DurableCompanyReportReader(db_session).read_job(job.id)
    assert report is not None
    assert report.report_state is CompanyReportState.COMPLETE
    assert report.historical.domain_outcome is None
    assert report.historical.domain_source == "no_automatic_decision"
    assert report.historical_domain_decision is None
    assert not any("exact domain-decision" in item for item in report.unavailable)


def test_conflicting_candidates_and_superseded_decisions_remain_explicit(
    db_session: Session,
) -> None:
    _, _, _, _, _, decision, job = _subject(db_session, state=DomainResolutionState.UNRESOLVED)
    decision.candidates = [
        {
            "domain": "first.example",
            "name": "Kiln Systems",
            "rank": 1,
            "eligible": True,
            "aligned": True,
            "alignment": "provider_name",
            "rejection_reason": None,
        },
        {
            "domain": "second.example",
            "name": "Kiln Systems",
            "rank": 2,
            "eligible": True,
            "aligned": True,
            "alignment": "provider_name",
            "rejection_reason": None,
        },
    ]
    db_session.flush()
    report = DurableCompanyReportReader(db_session).read_job(job.id)
    assert report is not None and report.historical_domain_decision is not None
    assert {item.status for item in report.historical_domain_decision.candidates} == {"conflicting"}


def test_partial_unavailable_wrong_agent_cross_owner_and_read_only(db_session: Session) -> None:
    _, _, _, membership, _, _, job = _subject(db_session)
    reader = DurableCompanyReportReader(db_session)
    job.result = {"company_id": str(job.company_id), "domain": "legacy.example"}
    db_session.flush()
    partial = reader.read_job(job.id)
    assert partial is not None and partial.report_state is CompanyReportState.PARTIAL

    job.result = None
    db_session.flush()
    unavailable = reader.read_job(job.id)
    assert unavailable is not None
    assert unavailable.report_state is CompanyReportState.UNAVAILABLE
    assert reader.read_job(uuid.uuid4()) is None

    before = {
        model: db_session.scalar(select(func.count()).select_from(model))
        for model in (AgentJob, Company, CompanyDomainResolution)
    }
    assert reader.read_job(job.id) is not None
    after = {model: db_session.scalar(select(func.count()).select_from(model)) for model in before}
    assert before == after
    assert not db_session.new and not db_session.dirty

    job.agent_id = AgentIdentifier.EMAIL
    db_session.flush()
    assert reader.read_job(job.id) is None
    job.agent_id = AgentIdentifier.COMPANY
    _, _, _, other_membership, _, _, _ = _subject(db_session)
    job.campaign_contact_id = other_membership.id
    db_session.flush()
    assert reader.read_job(job.id) is None
    assert membership.id != job.campaign_contact_id


def test_related_generations_retries_safe_error_and_research_handoff(db_session: Session) -> None:
    campaign, _, contact, membership, capture, _, job = _subject(db_session)
    rerun = _job(
        agent=AgentIdentifier.COMPANY,
        campaign=campaign,
        membership=membership,
        contact=contact,
        capture=capture,
        generation=2,
    )
    rerun.result = dict(job.result or {})
    job.error_class = "RuntimeError"
    job.last_error = "TOKEN=secret at /root/private/key.txt"
    db_session.add(rerun)
    db_session.flush()
    report = DurableCompanyReportReader(db_session).read_job(job.id)
    assert report is not None
    assert report.attempts == 2
    assert len(report.related_generations) == 2
    assert {item.generation for item in report.related_generations} == {1, 2}
    assert report.downstream_research_job_id is not None
    assert "secret" not in (report.error_detail or "")
    assert "/root/" not in (report.error_detail or "")
