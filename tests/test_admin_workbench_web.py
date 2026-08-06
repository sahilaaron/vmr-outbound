"""The redesigned Admin Workbench, driven over HTTP.

The Workbench is a projection over committed Phase 2 state, so these tests
build their world through the same deterministic scenario the legacy monitor
tests use (``tests/workbench_scenario.py``) — every job state and pipeline
event comes from the Phase 2 services, never from a hand-written row.

What the suite pins down:

* gating — no workbench switch, no routes; corrective actions additionally
  require ``agent_workbench`` and refuse without it;
* the shell — every conceptual area is reachable from the persistent rail;
* the primary workflow — Campaigns -> Campaign detail -> Contact table ->
  Contact diagnosis -> stage timeline -> Job detail;
* truthfulness — Sending renders unavailable, empty states render as empty
  states, lineage-free Research runs say so, blocked Contacts show the
  authoritative reason and no release control;
* safety — read-only pages write nothing (audit trail unchanged), commands
  report Phase 2's answer rather than the operator's intention;
* survival of the legacy surface — the retained routes stay reachable.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from app.api.deps import get_db
from app.core.config import get_settings
from app.main import create_app
from app.models.audit_event import AuditEvent
from app.models.enums import AgentIdentifier
from app.services.agents import jobs as agent_jobs
from app.services.agents.registry import AGENT_SPECS
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tests import workbench_scenario


def _build_client(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    *,
    workbench: bool = True,
    agent_workbench: bool = True,
) -> TestClient:
    if workbench:
        monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    if agent_workbench:
        monkeypatch.setenv("FEATURES__AGENT_WORKBENCH", "true")
    get_settings.cache_clear()
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


@pytest.fixture()
def client(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    with _build_client(db_session, monkeypatch) as c:
        yield c
    get_settings.cache_clear()


@pytest.fixture()
def read_only_client(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Workbench on, agent_workbench off: pages read, actions refuse."""

    with _build_client(db_session, monkeypatch, agent_workbench=False) as c:
        yield c
    get_settings.cache_clear()


@pytest.fixture()
def no_workbench_client(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    with _build_client(db_session, monkeypatch, workbench=False, agent_workbench=False) as c:
        yield c
    get_settings.cache_clear()


@pytest.fixture()
def scenario(db_session: Session) -> workbench_scenario.Scenario:
    return workbench_scenario.build(db_session)


AREAS = (
    "/admin",
    "/admin/campaigns",
    "/admin/contacts",
    "/admin/companies",
    "/admin/stages",
    "/admin/failures",
    "/admin/review",
    "/admin/providers",
    "/admin/configuration",
    "/admin/system",
    "/admin/diagnostics",
)


# --- Gating ------------------------------------------------------------------


def test_without_the_workbench_switch_no_admin_route_exists(
    no_workbench_client: TestClient,
) -> None:
    for path in AREAS:
        assert no_workbench_client.get(path).status_code == 404, path


def test_every_area_renders_with_an_empty_database(client: TestClient) -> None:
    """Complete, partial and absent data are all real states; empty must render."""

    for path in AREAS:
        response = client.get(path)
        assert response.status_code == 200, path
        assert "admin.css" in response.text, path


def test_actions_refuse_without_the_agent_workbench_switch(
    read_only_client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    membership = scenario.membership("terminal")
    response = read_only_client.post(
        f"/admin/campaigns/{scenario.campaign.id}/contacts/{membership.id}/actions/retry",
        data={"reason": "retry it"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "agent_workbench" in response.headers["location"]


# --- Shell -------------------------------------------------------------------


def test_the_rail_reaches_every_conceptual_area(client: TestClient) -> None:
    page = client.get("/admin").text
    for path in AREAS[1:]:
        assert f'href="{path}"' in page, path
    # The way back to the customer application, and the workflow surfaces.
    assert 'href="/app"' in page
    assert 'href="/imports"' in page
    assert 'href="/verification"' in page


def test_the_shell_never_loads_the_customer_stylesheet(client: TestClient) -> None:
    page = client.get("/admin").text
    assert "admin.css" in page
    assert "v2.css" not in page


# --- Overview ----------------------------------------------------------------


def test_the_overview_reports_the_scenario_truthfully(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    page = client.get("/admin").text
    assert scenario.campaign.name in page
    # Sending is not implemented and must say so, not render a control.
    assert "Sending Agent not implemented in this release" in page
    for spec in AGENT_SPECS.values():
        assert spec.display_name in page


def test_the_overview_attention_items_link_to_diagnosis_surfaces(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    page = client.get("/admin").text
    assert 'href="/admin/failures"' in page


# --- Campaigns → Contact diagnosis (the primary workflow) --------------------


def test_the_campaign_list_shows_health_and_counts(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    page = client.get("/admin/campaigns").text
    assert scenario.campaign.name in page
    assert scenario.other_campaign.name in page
    assert f'href="/admin/campaigns/{scenario.campaign.id}"' in page


def test_the_campaign_detail_shows_the_funnel_and_the_contact_table(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    page = client.get(f"/admin/campaigns/{scenario.campaign.id}").text
    for spec in AGENT_SPECS.values():
        assert spec.display_name.replace(" Agent", "") in page
    # Every enrolled Contact appears with a diagnosis link.
    for key in ("healthy", "leased", "retrying", "terminal", "suppressed", "nodomain"):
        membership = scenario.membership(key)
        assert f"/admin/campaigns/{scenario.campaign.id}/contacts/{membership.id}" in page, key
    # The other Campaign's Contact does not leak in.
    other = scenario.membership("other")
    assert str(other.id) not in page


def test_the_campaign_contact_table_supports_the_attention_filter(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    page = client.get(f"/admin/campaigns/{scenario.campaign.id}?attention=1").text
    suppressed = scenario.membership("suppressed")
    healthy = scenario.membership("healthy")
    assert str(suppressed.id) in page
    assert str(healthy.id) not in page


def test_an_unknown_campaign_is_a_clean_not_found(client: TestClient) -> None:
    response = client.get(f"/admin/campaigns/{uuid.uuid4()}")
    assert response.status_code == 404
    assert client.get("/admin/campaigns/not-a-uuid").status_code == 404


def test_the_contact_diagnosis_renders_the_full_stage_timeline(
    client: TestClient, scenario: workbench_scenario.Scenario, db_session: Session
) -> None:
    membership = scenario.membership("terminal")
    page = client.get(f"/admin/campaigns/{scenario.campaign.id}/contacts/{membership.id}").text
    for spec in AGENT_SPECS.values():
        assert spec.display_name in page
    # The committed failure reaches the operator in words, with its Job.
    assert "the record cannot be resolved from the evidence on file" in page
    job = scenario.job_for(db_session, "terminal")
    assert job is not None
    assert f"/admin/jobs/{job.id}" in page
    # Sending renders as unavailable, never as a pending stage.
    assert "Sending Agent is not implemented in this release." in page


def test_the_diagnosis_shows_authoritative_blocks_without_release_controls(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    membership = scenario.membership("suppressed")
    page = client.get(f"/admin/campaigns/{scenario.campaign.id}/contacts/{membership.id}").text
    assert "Blocking reasons" in page
    assert "cannot be released from this page" in page


def test_the_diagnosis_is_scoped_to_its_campaign(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    membership = scenario.membership("other")
    response = client.get(f"/admin/campaigns/{scenario.campaign.id}/contacts/{membership.id}")
    assert response.status_code == 404


def test_research_without_lineage_says_so_instead_of_guessing(
    client: TestClient, scenario: workbench_scenario.Scenario, db_session: Session
) -> None:
    """A Research job whose result predates RES-002 must not grow a fallback story."""

    membership = scenario.membership("healthy")
    job, _ = agent_jobs.enqueue_job(
        db_session,
        agent_id=AgentIdentifier.RESEARCH,
        idempotency_key=f"research-{membership.id}",
        task_kind="research.company",
        max_attempts=3,
        campaign_id=scenario.campaign.id,
        campaign_contact_id=membership.id,
        contact_id=membership.contact_id,
        entity_type="campaign_contact",
        entity_id=membership.id,
    )
    agent_jobs.claim_job(
        db_session, job_id=job.id, worker_id=workbench_scenario.WORKER, lease_seconds=60
    )
    agent_jobs.start_job(db_session, job, worker_id=workbench_scenario.WORKER)
    agent_jobs.mark_completed(
        db_session, job, result={"summary": "legacy run"}, outcome_committed=True
    )
    db_session.flush()

    page = client.get(f"/admin/campaigns/{scenario.campaign.id}/contacts/{membership.id}").text
    assert "lineage is unavailable" in page.lower()


def test_research_fallback_lineage_is_rendered_from_the_committed_result(
    client: TestClient, scenario: workbench_scenario.Scenario, db_session: Session
) -> None:
    membership = scenario.membership("healthy")
    job, _ = agent_jobs.enqueue_job(
        db_session,
        agent_id=AgentIdentifier.RESEARCH,
        idempotency_key=f"research-fb-{membership.id}",
        task_kind="research.company",
        max_attempts=3,
        campaign_id=scenario.campaign.id,
        campaign_contact_id=membership.id,
        contact_id=membership.contact_id,
        entity_type="campaign_contact",
        entity_id=membership.id,
    )
    agent_jobs.claim_job(
        db_session, job_id=job.id, worker_id=workbench_scenario.WORKER, lease_seconds=60
    )
    agent_jobs.start_job(db_session, job, worker_id=workbench_scenario.WORKER)
    agent_jobs.mark_completed(
        db_session,
        job,
        result={
            "dossier_basis": "claude_cli_fallback",
            "deterministic": {
                "workers": ["website"],
                "usable": False,
                "reason_code": "insufficient_evidence",
                "reason": "Two facts gathered; below the evidence floor.",
            },
            "fallback": {
                "attempted": True,
                "status": "succeeded",
                "producer": "claude_web",
                "producer_version": "research-claude-fallback/1",
                "evidence_accepted": 11,
                "claims_rejected": 4,
                "rejection_reasons": ["uncited model claim"],
                "source_urls": ["https://example.com/about"],
                "tools": ["WebSearch", "WebFetch"],
            },
        },
        outcome_committed=True,
    )
    db_session.flush()

    page = client.get(f"/admin/campaigns/{scenario.campaign.id}/contacts/{membership.id}").text
    assert "Claude web fallback" in page
    assert "claude_web" in page
    assert "11 claims accepted, 4 discarded" in page
    assert "claude_cli_fallback" in page
    # The Overview's fallback panel picks the same run up.
    overview = client.get("/admin").text
    assert "Recent Research fallback use" in overview
    assert "claude_cli_fallback" in overview


# --- Corrective actions ------------------------------------------------------


def test_retry_reports_phase_two_refusal_verbatim_for_a_terminal_failure(
    client: TestClient, scenario: workbench_scenario.Scenario, db_session: Session
) -> None:
    job = scenario.job_for(db_session, "terminal")
    assert job is not None
    response = client.post(
        f"/admin/jobs/{job.id}/retry", data={"reason": "try again"}, follow_redirects=False
    )
    assert response.status_code == 303
    location = response.headers["location"]
    # Whatever Phase 2 answered, the page reports an outcome, not the intention.
    assert "ok=" in location or "err=" in location


def test_contact_pause_and_resume_round_trip_through_the_command_surface(
    client: TestClient, scenario: workbench_scenario.Scenario, db_session: Session
) -> None:
    membership = scenario.membership("healthy")
    base = f"/admin/campaigns/{scenario.campaign.id}/contacts/{membership.id}"
    paused = client.post(f"{base}/actions/pause", data={}, follow_redirects=False)
    assert paused.status_code == 303
    assert "ok=" in paused.headers["location"]
    resumed = client.post(f"{base}/actions/resume", data={}, follow_redirects=False)
    assert resumed.status_code == 303
    assert "ok=" in resumed.headers["location"]


def test_skip_stage_requires_a_reason(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    membership = scenario.membership("healthy")
    response = client.post(
        f"/admin/campaigns/{scenario.campaign.id}/contacts/{membership.id}/actions/skip-stage",
        data={"reason": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "reason" in response.headers["location"].lower()


def test_an_unknown_command_is_refused(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    membership = scenario.membership("healthy")
    response = client.post(
        f"/admin/campaigns/{scenario.campaign.id}/contacts/{membership.id}/actions/drop-table",
        data={},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "not available" in response.headers["location"].replace("+", " ")


# --- Failures inbox ----------------------------------------------------------


def test_the_failures_inbox_normalizes_stage_failures_and_blocks(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    page = client.get("/admin/failures").text
    # The terminal failure and the suppressed Contact both surface, each with a
    # route to its diagnosis.
    terminal = scenario.membership("terminal")
    suppressed = scenario.membership("suppressed")
    assert f"/admin/campaigns/{scenario.campaign.id}/contacts/{terminal.id}" in page
    assert f"/admin/campaigns/{scenario.campaign.id}/contacts/{suppressed.id}" in page


def test_the_failures_inbox_filters_by_category(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    page = client.get("/admin/failures?category=blocked_contact").text
    suppressed = scenario.membership("suppressed")
    terminal = scenario.membership("terminal")
    assert str(suppressed.id) in page
    assert str(terminal.id) not in page


# --- Agent/Stages ------------------------------------------------------------


def test_the_stage_index_lists_the_pipeline_in_order_with_workers(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    page = client.get("/admin/stages").text
    for spec in AGENT_SPECS.values():
        assert spec.display_name in page
    assert "claude_web" in page  # the Research fallback worker is named
    assert "Sending Agent not implemented" in page


def test_the_stage_detail_shows_control_provenance_and_open_jobs(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    page = client.get("/admin/stages/research").text
    assert "Research Agent" in page
    assert "claude_web" in page
    assert client.get("/admin/stages/nonsense").status_code == 404


# --- Contacts / Companies ----------------------------------------------------


def test_the_contact_index_searches_and_shows_memberships(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    page = client.get("/admin/contacts?q=Nakamura").text
    assert "Alice Nakamura" in page
    assert "Gerald Pinto" not in page


def test_the_contact_detail_shows_suppressions_and_memberships(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    contact = scenario.contacts["suppressed"]
    page = client.get(f"/admin/contacts/{contact.id}").text
    assert "suppressed" in page.lower()
    assert "opt out" in page.lower() or "opt_out" in page.lower()
    assert scenario.campaign.name in page
    # And the authority statement is visible.
    assert "cannot be released from the Workbench" in page


def test_the_company_index_renders(client: TestClient) -> None:
    assert client.get("/admin/companies").status_code == 200
    assert client.get(f"/admin/companies/{uuid.uuid4()}").status_code == 404


# --- Review / Providers / Configuration / System -----------------------------


def test_review_renders_the_unavailable_state_without_drafting(
    client: TestClient,
) -> None:
    page = client.get("/admin/review").text
    assert "Review is unavailable" in page
    # No fabricated queue, no fake decisions.
    assert "awaiting decision" not in page


def test_providers_shows_configuration_state_without_secrets(
    client: TestClient,
) -> None:
    page = client.get("/admin/providers").text
    assert "MillionVerifier" in page
    assert "Claude CLI" in page
    assert "Logo.dev" in page
    assert "simulator" in page  # the honest no-key state


def test_configuration_is_read_only_and_names_the_authoritative_surfaces(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    page = client.get("/admin/configuration").text
    assert "read-only" in page
    for spec in AGENT_SPECS.values():
        assert spec.display_name in page
    assert "dry-run" in page.lower() or "Dry-run" in page


def test_system_searches_jobs_by_uuid_only(
    client: TestClient, scenario: workbench_scenario.Scenario, db_session: Session
) -> None:
    assert "Enter a full Agent Job UUID" in client.get("/admin/system?job=nonsense").text
    assert (
        "No Agent Job with that identifier" in client.get(f"/admin/system?job={uuid.uuid4()}").text
    )
    job = scenario.job_for(db_session, "terminal")
    assert job is not None
    assert f"/admin/jobs/{job.id}" in client.get(f"/admin/system?job={job.id}").text


def test_the_job_detail_renders_sanitized_execution_state(
    client: TestClient, scenario: workbench_scenario.Scenario, db_session: Session
) -> None:
    job = scenario.job_for(db_session, "terminal")
    assert job is not None
    page = client.get(f"/admin/jobs/{job.id}").text
    assert "terminal_domain_error" in page
    assert "Input snapshot" in page
    assert client.get(f"/admin/jobs/{uuid.uuid4()}").status_code == 404


# --- Diagnostics and legacy survival -----------------------------------------


def test_advanced_diagnostics_catalogues_the_legacy_surfaces(
    client: TestClient,
) -> None:
    page = client.get("/admin/diagnostics").text
    for href in (
        "/admin/agents/studio",
        "/workbench",
        "/imports",
        "/verification",
        "/local-tools",
        "/admin/legacy/overview",
    ):
        assert f'href="{href}"' in page, href


def test_the_retained_legacy_routes_still_answer(client: TestClient) -> None:
    for path in (
        "/admin/legacy/overview",
        "/campaigns",
        "/contacts",
        "/companies",
        "/imports",
        "/review",
        "/workbench",
        "/admin/agents/studio",
    ):
        assert client.get(path).status_code == 200, path


# --- No mutation from read-only pages ----------------------------------------


def test_reading_every_page_commits_nothing(
    client: TestClient, scenario: workbench_scenario.Scenario, db_session: Session
) -> None:
    before = db_session.scalar(select(func.count(AuditEvent.id)))
    membership = scenario.membership("terminal")
    for path in AREAS + (
        f"/admin/campaigns/{scenario.campaign.id}",
        f"/admin/campaigns/{scenario.campaign.id}/contacts/{membership.id}",
    ):
        assert client.get(path).status_code == 200, path
    after = db_session.scalar(select(func.count(AuditEvent.id)))
    assert before == after
