"""Server-rendered Admin Agent Studio and Research report boundaries."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from app.api.deps import get_db
from app.core.config import get_settings
from app.main import create_app
from app.models.draft import DraftApproval, DraftVersion
from app.models.enums import AgentIdentifier
from app.models.verification_job import AgentJob
from app.services.agent_studio.extensions import AGENT_STUDIO_MODULES
from app.services.agent_studio.research_report import (
    ResearchFactView,
    ResearchReport,
    ResearchReportState,
    ResearchRetryView,
    ResearchSourceRead,
)
from app.services.agents.registry import PIPELINE_ORDER
from app.services.personalization import policy
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tests.test_agent_studio_policy import ScriptedThinker, _subject


@pytest.fixture()
def studio_client(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
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


def test_global_studio_lists_the_authoritative_registry_and_capabilities(
    studio_client: TestClient,
) -> None:
    response = studio_client.get("/admin/agents/studio")
    assert response.status_code == 200
    assert response.text.count("Position ") == len(PIPELINE_ORDER)
    for agent_id in PIPELINE_ORDER:
        assert AGENT_STUDIO_MODULES[agent_id].dedicated_path in response.text
    assert "configure" in response.text
    assert "preview" in response.text
    assert "No persisted run" in response.text
    assert "Open execution Workbench" in response.text


def test_personalization_has_a_dedicated_policy_page_and_other_agents_are_explicit(
    studio_client: TestClient, db_session: Session
) -> None:
    active = policy.ensure_initial_policy(db_session, actor="test:web")
    response = studio_client.get("/admin/agents/studio/personalization")
    assert response.status_code == 200
    assert f"Active: v{active.version_number}" in response.text
    assert "Side-effect-free" in response.text
    assert "Earnest offering-led introduction" in response.text
    assert "Saving creates a draft version; it does not activate it." in response.text

    email = studio_client.get("/admin/agents/studio/email")
    assert email.status_code == 200
    assert "Email Agent Studio" in email.text
    assert "Employee size does not select or sequence patterns" in email.text
    assert "Read-only execution inspection" in email.text

    verification = studio_client.get("/admin/agents/studio/verification")
    assert verification.status_code == 200
    assert "Verification Agent Studio" in verification.text
    assert "Provider Test Console can be billable" in verification.text
    assert "Customer Operation" in verification.text
    assert "Agent Studio" in verification.text


def test_wording_revision_is_versioned_and_preserves_every_other_policy_field(
    studio_client: TestClient, db_session: Session
) -> None:
    active = policy.ensure_initial_policy(db_session, actor="test:web")
    original = policy.PolicyConfig.from_dict(dict(active.configuration))
    data = {
        "edit_mode": "wording",
        "based_on_version_id": str(active.id),
        "name": "Clearer operator wording",
        "change_note": "Make the standards easier to inspect without retuning policy.",
    }
    for standard in original.standards:
        data[f"standard_{standard.identifier}_description"] = standard.description
        data[f"standard_{standard.identifier}_wording"] = standard.wording
    first = original.standards[0]
    data[f"standard_{first.identifier}_description"] = "A clearer operator description."
    data[f"standard_{first.identifier}_wording"] = "A clearer deterministic instruction."

    response = studio_client.post("/admin/agents/studio/personalization/policies", data=data)

    assert response.status_code == 200
    revised_version = policy.list_policy_versions(db_session)[0]
    revised = policy.PolicyConfig.from_dict(dict(revised_version.configuration))
    assert revised_version.id != active.id
    assert revised_version.based_on_version_id == active.id
    assert policy.active_policy(db_session).id == active.id
    assert revised.standards[0].description == "A clearer operator description."
    assert revised.standards[0].wording == "A clearer deterministic instruction."
    assert revised.standards[0].strength == first.strength
    assert revised.standards[0].state == first.state
    assert revised.standards[1:] == original.standards[1:]
    assert revised.temperament == original.temperament
    assert revised.strategies == original.strategies
    assert revised.fallback_ladder == original.fallback_ladder
    assert revised.examples == original.examples
    assert revised.evidence == original.evidence


def test_preview_route_renders_decision_and_creates_no_pipeline_side_effect(
    studio_client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, _, membership = _subject(db_session)
    active = policy.ensure_initial_policy(db_session, actor="test:web")
    from app.web import routes

    thinker = ScriptedThinker(
        {
            "subject": "A direct introduction",
            "body": "We help teams simplify workflow. Is that relevant to you?",
            "evidence_insight_ids": [],
            "rationale": "Offering-led fallback selected.",
        }
    )
    monkeypatch.setattr(routes, "_personalization_thinker", lambda: thinker)
    before = {
        model: db_session.scalar(select(func.count()).select_from(model))
        for model in (DraftVersion, DraftApproval, AgentJob)
    }
    response = studio_client.post(
        "/admin/agents/studio/personalization/preview",
        data={
            "campaign_contact_id": str(membership.id),
            "policy_version_id": str(active.id),
        },
    )
    after = {model: db_session.scalar(select(func.count()).select_from(model)) for model in before}
    assert response.status_code == 200
    assert "A direct introduction" in response.text
    assert "Level 5" in response.text
    assert "not saved" in response.text
    assert before == after
    assert thinker.requests[0].purpose == "email_personalization_preview"


def test_studio_exists_only_under_admin_and_is_absent_when_admin_is_not_mounted(
    studio_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert studio_client.get("/app/agents/studio").status_code == 404
    assert studio_client.get("/app/agents/studio/insights").status_code == 404

    monkeypatch.setenv("FEATURES__WORKBENCH", "false")
    monkeypatch.setenv("FEATURES__AGENT_WORKBENCH", "true")
    get_settings.cache_clear()
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as client:
        assert client.get("/admin/agents/studio").status_code == 404
        assert (
            client.get(f"/api/admin/agent-studio/research/jobs/{uuid.uuid4()}/report").status_code
            == 404
        )
        assert (
            client.get(f"/api/admin/agent-studio/email/jobs/{uuid.uuid4()}/report").status_code
            == 404
        )
        assert (
            client.get(f"/api/admin/agent-studio/insights/jobs/{uuid.uuid4()}/report").status_code
            == 404
        )
        assert (
            client.get(
                f"/api/admin/agent-studio/verification/jobs/{uuid.uuid4()}/report"
            ).status_code
            == 404
        )


class _Reader:
    def __init__(self, report: ResearchReport | None) -> None:
        self.report = report

    def read(self, _campaign_contact_id: uuid.UUID) -> ResearchReport | None:
        return self.report

    def read_job(self, agent_job_id: uuid.UUID) -> ResearchReport | None:
        if self.report is None or self.report.job_id != agent_job_id:
            return None
        return self.report


def _report(*, complete: bool) -> ResearchReport:
    now = datetime.now(UTC)
    membership_id = uuid.uuid4()
    job_id = uuid.uuid4()
    return ResearchReport(
        campaign_contact_id=membership_id,
        campaign_id=uuid.uuid4(),
        campaign_name="Research Pilot",
        contact_id=uuid.uuid4(),
        contact_label="Ada Lovelace",
        company_id=uuid.uuid4() if complete else None,
        company_name="Kiln Systems" if complete else None,
        domain="kiln.example" if complete else None,
        domain_state="confirmed" if complete else None,
        job_id=job_id if complete else None,
        job_status="succeeded" if complete else None,
        worker_identity=("homepage/v2",) if complete else (),
        attempts=1 if complete else None,
        max_attempts=3 if complete else None,
        started_at=now if complete else None,
        finished_at=now if complete else None,
        duration_seconds=1.2 if complete else None,
        urls_attempted=("https://kiln.example",) if complete else None,
        successful_reads=(
            ResearchSourceRead(
                url="https://kiln.example",
                title="Kiln Systems",
                retrieved_at=now.isoformat(),
                retrieval_method="homepage",
            ),
        )
        if complete
        else None,
        collection_failures=() if complete else None,
        submission_id=uuid.uuid4() if complete else None,
        dossier_version=2 if complete else None,
        sourced_facts=(
            ResearchFactView(
                claim="Offers plant workflow software.",
                source_urls=("https://kiln.example",),
                confidence=0.92,
            ),
        )
        if complete
        else None,
        rejected_evidence=("Stale careers page was rejected.",) if complete else None,
        retry_history=(
            ResearchRetryView(
                job_id=job_id,
                public_status="succeeded",
                attempts=1,
                max_attempts=3,
                error_type=None,
                error_detail=None,
                created_at=now,
            ),
        )
        if complete
        else (),
        final_outcome="research_stored" if complete else None,
        error_type=None,
        error_detail=None,
        unavailable=()
        if complete
        else (
            "No persisted Research Agent Job exists for this Campaign Contact.",
            "Worker-level collection detail was not persisted for this run.",
        ),
        report_state=(
            ResearchReportState.COMPLETE if complete else ResearchReportState.UNAVAILABLE
        ),
        report_reason=(
            "The selected job and its committed Research artifacts are durably available."
            if complete
            else "No Research execution has been durably recorded."
        ),
    )


@pytest.mark.parametrize("complete", [True, False])
def test_research_report_renders_complete_and_unavailable_stable_read_models(
    studio_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    complete: bool,
) -> None:
    report = _report(complete=complete)
    from app.web import routes

    monkeypatch.setattr(routes, "_research_report_reader", lambda _db: _Reader(report))
    response = studio_client.get(
        f"/admin/agents/studio/research?campaign_contact={report.campaign_contact_id}"
    )
    assert response.status_code == 200
    assert "Read-only" in response.text
    assert "Research prompts, workers, code and collection rules cannot be edited" in response.text
    if complete:
        assert "homepage/v2" in response.text
        assert "Offers plant workflow software." in response.text
        assert "research_stored" in response.text
    else:
        assert "Unavailable in current persistence" in response.text
        assert "Worker-level collection detail was not persisted" in response.text
        assert "console logs" in response.text


def test_research_report_renders_an_explicit_partial_state(
    studio_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = replace(
        _report(complete=True),
        report_state=ResearchReportState.PARTIAL,
        report_reason="The job exists, but its exact dossier is unavailable.",
        dossier_version=None,
        unavailable=("No exact dossier version is linked to this job.",),
    )
    from app.web import routes

    monkeypatch.setattr(routes, "_research_report_reader", lambda _db: _Reader(report))
    response = studio_client.get(
        f"/admin/agents/studio/research?campaign_contact={report.campaign_contact_id}"
    )
    assert response.status_code == 200
    assert "Report partial" in response.text
    assert "exact dossier is unavailable" in response.text


def test_research_api_and_html_share_the_typed_reader(
    studio_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _report(complete=True)
    assert report.job_id is not None
    reader = _Reader(report)
    from app.web import routes

    monkeypatch.setattr(routes, "_research_report_reader", lambda _db: reader)
    html = studio_client.get(
        f"/admin/agents/studio/research?campaign_contact={report.campaign_contact_id}"
    )
    api = studio_client.get(f"/api/admin/agent-studio/research/jobs/{report.job_id}/report")

    assert html.status_code == 200
    assert str(report.job_id) in html.text
    assert api.status_code == 200
    assert api.json()["job_id"] == str(report.job_id)
    assert api.json()["report_state"] == "complete"
    assert (
        studio_client.get(f"/app/agents/studio/research/jobs/{report.job_id}/report").status_code
        == 404
    )


def test_research_api_hides_unknown_and_non_research_jobs(
    studio_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.web import routes

    monkeypatch.setattr(routes, "_research_report_reader", lambda _db: _Reader(None))
    missing = studio_client.get(f"/api/admin/agent-studio/research/jobs/{uuid.uuid4()}/report")
    malformed = studio_client.get("/api/admin/agent-studio/research/jobs/not-a-uuid/report")
    assert missing.status_code == 404
    assert malformed.status_code == 404
    assert missing.json() == malformed.json() == {"detail": "Not found."}


def test_insights_html_and_api_share_the_exact_job_report(
    studio_client: TestClient, db_session: Session
) -> None:
    from tests.test_agent_studio_insights_report import _complete

    job, dossier_id, claim_id = _complete(db_session)
    html = studio_client.get(f"/admin/agents/studio/insights?job={job.id}")
    api = studio_client.get(f"/api/admin/agent-studio/insights/jobs/{job.id}/report")

    assert html.status_code == 200
    assert "Insights Agent Studio" in html.text
    assert str(job.id) in html.text
    assert str(dossier_id) in html.text
    assert str(claim_id) in html.text
    assert "Customer/account" in html.text
    assert "Employee Size" in html.text
    assert api.status_code == 200
    assert api.json()["job_id"] == str(job.id)
    assert api.json()["research_dossier_id"] == str(dossier_id)
    assert api.json()["claims"][0]["insight_id"] == str(claim_id)
    assert studio_client.get(f"/app/agents/studio/insights?job={job.id}").status_code == 404


def test_insights_api_uses_one_generic_safe_not_found(studio_client: TestClient) -> None:
    missing = studio_client.get(f"/api/admin/agent-studio/insights/jobs/{uuid.uuid4()}/report")
    malformed = studio_client.get("/api/admin/agent-studio/insights/jobs/not-a-uuid/report")
    assert missing.status_code == malformed.status_code == 404
    assert missing.json() == malformed.json() == {"detail": "Not found."}


def test_every_registered_agent_has_exactly_one_studio_module() -> None:
    assert tuple(AGENT_STUDIO_MODULES) == PIPELINE_ORDER
    assert AGENT_STUDIO_MODULES[AgentIdentifier.PERSONALIZATION].capabilities.configuration
    assert AGENT_STUDIO_MODULES[AgentIdentifier.RESEARCH].capabilities.reporting
    assert not AGENT_STUDIO_MODULES[AgentIdentifier.RESEARCH].capabilities.configuration
    assert not AGENT_STUDIO_MODULES[AgentIdentifier.SENDING].capabilities.live_execution
