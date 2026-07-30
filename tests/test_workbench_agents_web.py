"""Workbench pages and operator commands, driven over HTTP.

Two layers, deliberately:

* **Real Phase 2 integration.** Most tests drive the pages against a database
  populated through the Phase 2 services, so what the operator sees is what the
  execution backbone actually holds.
* **A deterministic stub reader.** A handful of render tests swap the read port
  for a fixed view model, proving the templates depend on the port and nothing
  else — and giving a page-render failure that cannot be caused by a query.

The command tests care about one thing above all: that the page reports what
Phase 2 answered. A refused control must reach the operator as a refusal with a
reason, never as a silent no-op and never as a success.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from urllib.parse import unquote_plus

import pytest
from app.api.deps import get_db
from app.core.config import get_settings
from app.main import create_app
from app.models.agent import AgentControl
from app.models.audit_event import AuditEvent
from app.models.enums import (
    AgentControlStatus,
    AgentIdentifier,
    AgentJobStatus,
    CampaignMembershipStatus,
)
from app.services.agents.registry import AGENT_SPECS
from app.services.workbench_agents.views import (
    AgentCardView,
    ControlView,
    QueueCounts,
    WorkbenchOverviewView,
)
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests import workbench_scenario


def _build_client(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, *, enabled: bool
) -> TestClient:
    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    if enabled:
        monkeypatch.setenv("FEATURES__AGENT_WORKBENCH", "true")
    get_settings.cache_clear()
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


@pytest.fixture()
def client(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    app_client = _build_client(db_session, monkeypatch, enabled=True)
    with app_client as c:
        yield c
    get_settings.cache_clear()


@pytest.fixture()
def disabled_client(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    app_client = _build_client(db_session, monkeypatch, enabled=False)
    with app_client as c:
        yield c
    get_settings.cache_clear()


@pytest.fixture()
def scenario(db_session: Session) -> workbench_scenario.Scenario:
    return workbench_scenario.build(db_session)


def _flash(response: object) -> str:
    location = getattr(response, "headers", {}).get("location", "")
    return unquote_plus(location)


# --- Gating ------------------------------------------------------------------


def test_while_the_feature_is_off_the_area_is_one_clean_unavailable_state(
    disabled_client: TestClient,
) -> None:
    response = disabled_client.get("/workbench")
    assert response.status_code == 200
    assert "isn't available yet" in response.text


def test_while_the_feature_is_off_commands_refuse(
    disabled_client: TestClient, scenario: workbench_scenario.Scenario, db_session: Session
) -> None:
    response = disabled_client.post(
        "/workbench/agents/identity/control",
        data={"status": "paused", "expected_version": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "disabled" in _flash(response)
    assert db_session.get(AgentControl, AgentIdentifier.IDENTITY) is None


def test_the_navigation_offers_the_workbench_when_it_is_on(client: TestClient) -> None:
    response = client.get("/admin")
    assert response.status_code == 200
    assert 'href="/workbench"' in response.text


# --- Agent overview ----------------------------------------------------------


def test_the_overview_renders_every_registered_agent(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    response = client.get("/workbench")
    assert response.status_code == 200
    for spec in AGENT_SPECS.values():
        assert spec.display_name in response.text
    assert scenario.campaign.name in response.text


def test_sending_is_shown_as_stopped_by_default(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    """Sending is disabled in the registry and the page must say so unprompted."""

    response = client.get("/workbench")
    assert "Sending is stopped" in response.text


def test_the_overview_distinguishes_terminal_from_retryable_failures(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    response = client.get("/workbench")
    assert "terminal" in response.text
    assert "retryable" in response.text


# --- Campaign and Contact pages ----------------------------------------------


def test_the_campaign_page_shows_the_pipeline_and_the_controls(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    response = client.get(f"/workbench/campaigns/{scenario.campaign.id}")
    assert response.status_code == 200
    assert "Pipeline distribution" in response.text
    assert "Campaign Contacts" in response.text

    # The Agent controls are now a compact strip with the override forms behind a
    # disclosure, rather than a nine-row table always expanded. Both halves must
    # still be present and reachable without JavaScript.
    assert "agent-strip" in response.text
    assert "Change an Agent for this Campaign" in response.text
    assert "/agents/identity/override" in response.text
    # In pipeline order, with the name readable at a glance.
    assert response.text.index("agent-chip") < response.text.index("Change an Agent")


def test_an_unknown_campaign_is_a_clean_not_found(client: TestClient) -> None:
    assert client.get(f"/workbench/campaigns/{uuid.uuid4()}").status_code == 404
    assert client.get("/workbench/campaigns/not-a-uuid").status_code == 404


def test_stage_filtering_narrows_the_campaign_contact_list(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    everything = client.get(f"/workbench/campaigns/{scenario.campaign.id}")
    filtered = client.get(f"/workbench/campaigns/{scenario.campaign.id}?stage=sending")
    assert everything.status_code == filtered.status_code == 200
    assert len(filtered.text) < len(everything.text)


def test_the_contact_page_shows_history_stages_and_the_block(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    membership = scenario.membership("suppressed")
    response = client.get(f"/workbench/campaigns/{scenario.campaign.id}/contacts/{membership.id}")
    assert response.status_code == 200
    assert "Campaign-specific execution only" in response.text
    assert "Pipeline history" in response.text
    assert "Blocked" in response.text
    assert "suppress" in response.text.lower()


def test_a_contact_from_another_campaign_is_a_clean_not_found(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    response = client.get(
        f"/workbench/campaigns/{scenario.campaign.id}/contacts/{scenario.membership('other').id}"
    )
    assert response.status_code == 404


# --- Jobs --------------------------------------------------------------------


def test_the_job_list_filters_and_shows_lease_and_retry_timing(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    assert client.get("/workbench/jobs").status_code == 200
    failed = client.get("/workbench/jobs?status=failed")
    assert failed.status_code == 200
    assert "terminal failure" in failed.text
    leased = client.get("/workbench/jobs?status=leased")
    assert "fixture-worker" in leased.text
    assert client.get("/workbench/jobs?agent=identity&status=queued").status_code == 200
    # A hand-edited, unrecognised filter widens rather than erroring.
    assert client.get("/workbench/jobs?status=nonsense").status_code == 200


def test_the_job_page_shows_the_durable_identity_and_refusal(
    client: TestClient, scenario: workbench_scenario.Scenario, db_session: Session
) -> None:
    job = scenario.job_for(db_session, "terminal")
    assert job is not None
    response = client.get(f"/workbench/jobs/{job.id}")
    assert response.status_code == 200
    assert "Durable identity" in response.text
    assert "not retryable" in response.text


def test_an_unknown_job_is_a_clean_not_found(client: TestClient) -> None:
    assert client.get(f"/workbench/jobs/{uuid.uuid4()}").status_code == 404
    assert client.get("/workbench/jobs/not-a-uuid").status_code == 404


def test_a_page_never_renders_a_provider_credential(
    client: TestClient, scenario: workbench_scenario.Scenario, db_session: Session
) -> None:
    job = scenario.job_for(db_session, "terminal")
    assert job is not None
    job.last_error = "provider call https://api.example.com/v3?api=sk_live_NEVERSHOWME failed"
    job.input_reference = {"api_key": "sk_live_NEVERSHOWME"}
    db_session.flush()
    page = client.get(f"/workbench/jobs/{job.id}")
    listing = client.get("/workbench/jobs?status=failed")
    assert "sk_live_NEVERSHOWME" not in page.text
    assert "sk_live_NEVERSHOWME" not in listing.text
    assert "[redacted]" in page.text


# --- Commands ----------------------------------------------------------------


def test_pausing_an_agent_reports_the_in_flight_consequence(
    client: TestClient, scenario: workbench_scenario.Scenario, db_session: Session
) -> None:
    response = client.post(
        "/workbench/agents/identity/control",
        data={"status": "paused", "expected_version": "", "reason": "policy review"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    flash = _flash(response)
    assert "ok=" in flash
    assert "held at paused" in flash
    control = db_session.get(AgentControl, AgentIdentifier.IDENTITY)
    assert control is not None and control.status is AgentControlStatus.PAUSED


def test_a_stale_control_version_is_refused_on_the_page(
    client: TestClient, scenario: workbench_scenario.Scenario, db_session: Session
) -> None:
    first = client.post(
        "/workbench/agents/identity/control",
        data={"status": "paused", "expected_version": ""},
        follow_redirects=False,
    )
    assert "ok=" in _flash(first)
    stale = client.post(
        "/workbench/agents/identity/control",
        data={"status": "disabled", "expected_version": ""},
        follow_redirects=False,
    )
    assert "err=" in _flash(stale)
    assert "changed while the page was open" in _flash(stale)
    control = db_session.get(AgentControl, AgentIdentifier.IDENTITY)
    assert control is not None and control.status is AgentControlStatus.PAUSED


def test_an_unrecognised_status_changes_nothing(
    client: TestClient, scenario: workbench_scenario.Scenario, db_session: Session
) -> None:
    response = client.post(
        "/workbench/agents/identity/control",
        data={"status": "sideways", "expected_version": ""},
        follow_redirects=False,
    )
    assert "Nothing changed" in _flash(response)
    assert db_session.get(AgentControl, AgentIdentifier.IDENTITY) is None


def test_enabling_an_agent_without_an_adapter_is_refused_on_the_page(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    response = client.post(
        "/workbench/agents/sending/control",
        data={"status": "enabled", "expected_version": ""},
        follow_redirects=False,
    )
    assert "err=" in _flash(response)
    assert "adapter" in _flash(response)


def test_a_campaign_override_applies_and_clears(
    client: TestClient, scenario: workbench_scenario.Scenario, db_session: Session
) -> None:
    applied = client.post(
        f"/workbench/campaigns/{scenario.campaign.id}/agents/identity/override",
        data={"status": "disabled", "expected_version": "", "reason": "pilot only"},
        follow_redirects=False,
    )
    assert "ok=" in _flash(applied)
    assert "No other Campaign changed" in _flash(applied)

    cleared = client.post(
        f"/workbench/campaigns/{scenario.campaign.id}/agents/identity/override/clear",
        data={"expected_version": "1"},
        follow_redirects=False,
    )
    assert "ok=" in _flash(cleared)

    # Clearing again is refused rather than reported as a change.
    again = client.post(
        f"/workbench/campaigns/{scenario.campaign.id}/agents/identity/override/clear",
        data={"expected_version": ""},
        follow_redirects=False,
    )
    assert "err=" in _flash(again)


def test_an_override_for_an_unknown_campaign_is_refused(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    response = client.post(
        f"/workbench/campaigns/{uuid.uuid4()}/agents/identity/override",
        data={"status": "paused", "expected_version": ""},
        follow_redirects=False,
    )
    assert "err=" in _flash(response)


def test_the_sending_stop_requires_the_typed_confirmation(
    client: TestClient, scenario: workbench_scenario.Scenario, db_session: Session
) -> None:
    response = client.post(
        "/workbench/agents/sending/stop",
        data={"confirm": "yes", "expected_version": ""},
        follow_redirects=False,
    )
    assert "Nothing changed" in _flash(response)
    assert db_session.get(AgentControl, AgentIdentifier.SENDING) is None


def test_the_sending_stop_is_recorded_and_immediately_visible(
    client: TestClient, scenario: workbench_scenario.Scenario, db_session: Session
) -> None:
    response = client.post(
        "/workbench/agents/sending/stop",
        data={"confirm": "STOP", "expected_version": "", "reason": "deliverability incident"},
        follow_redirects=False,
    )
    assert "ok=" in _flash(response)
    control = db_session.get(AgentControl, AgentIdentifier.SENDING)
    assert control is not None and control.status is AgentControlStatus.DISABLED
    assert "Sending is stopped" in client.get("/workbench").text


def test_resuming_sending_requires_confirmation_and_still_defers_to_phase_two(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    unconfirmed = client.post(
        "/workbench/agents/sending/resume",
        data={"confirm": "yes", "expected_version": ""},
        follow_redirects=False,
    )
    assert "Nothing changed" in _flash(unconfirmed)

    confirmed = client.post(
        "/workbench/agents/sending/resume",
        data={"confirm": "RESUME SENDING", "expected_version": ""},
        follow_redirects=False,
    )
    assert "err=" in _flash(confirmed)
    assert "adapter" in _flash(confirmed)


def test_retrying_a_terminal_job_is_surfaced_as_a_refusal(
    client: TestClient, scenario: workbench_scenario.Scenario, db_session: Session
) -> None:
    job = scenario.job_for(db_session, "terminal")
    assert job is not None
    response = client.post(f"/workbench/jobs/{job.id}/retry", follow_redirects=False)
    assert "err=" in _flash(response)


def test_retrying_a_retryable_job_is_accepted(
    client: TestClient, scenario: workbench_scenario.Scenario, db_session: Session
) -> None:
    job = scenario.job_for(db_session, "terminal")
    assert job is not None
    workbench_scenario.make_retryable_failure(db_session, job)
    response = client.post(
        f"/workbench/jobs/{job.id}/retry",
        data={"reason": "the provider recovered"},
        follow_redirects=False,
    )
    assert "ok=" in _flash(response)
    db_session.refresh(job)
    assert job.status is AgentJobStatus.PENDING


def test_retrying_an_unknown_job_is_refused(client: TestClient) -> None:
    response = client.post(f"/workbench/jobs/{uuid.uuid4()}/retry", follow_redirects=False)
    assert "err=" in _flash(response)
    assert (
        client.post("/workbench/jobs/not-a-uuid/retry", follow_redirects=False).status_code == 303
    )


def test_pausing_and_resuming_a_campaign_contact_from_the_page(
    client: TestClient, scenario: workbench_scenario.Scenario, db_session: Session
) -> None:
    membership = scenario.membership("healthy")
    base = f"/workbench/campaigns/{scenario.campaign.id}/contacts/{membership.id}"
    paused = client.post(f"{base}/pause", data={"reason": "hold"}, follow_redirects=False)
    assert "ok=" in _flash(paused)
    db_session.refresh(membership)
    assert membership.membership_status is CampaignMembershipStatus.PAUSED

    resumed = client.post(f"{base}/resume", follow_redirects=False)
    assert "ok=" in _flash(resumed)
    db_session.refresh(membership)
    assert membership.membership_status is CampaignMembershipStatus.ACTIVE


def test_a_suppressed_contact_cannot_be_retried_from_the_page(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    membership = scenario.membership("suppressed")
    response = client.post(
        f"/workbench/campaigns/{scenario.campaign.id}/contacts/{membership.id}/retry",
        data={"reason": "please"},
        follow_redirects=False,
    )
    assert "err=" in _flash(response)


def test_skipping_a_stage_without_a_reason_is_refused(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    membership = scenario.membership("healthy")
    response = client.post(
        f"/workbench/campaigns/{scenario.campaign.id}/contacts/{membership.id}/skip-stage",
        data={"reason": "  "},
        follow_redirects=False,
    )
    assert "A reason is required" in _flash(response)


def test_an_unknown_contact_command_is_refused(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    membership = scenario.membership("healthy")
    response = client.post(
        f"/workbench/campaigns/{scenario.campaign.id}/contacts/{membership.id}/detonate",
        follow_redirects=False,
    )
    assert "not available" in _flash(response)


# --- Audit and idempotency ---------------------------------------------------


def test_every_command_is_audited_accepted_or_refused(
    client: TestClient, scenario: workbench_scenario.Scenario, db_session: Session
) -> None:
    client.post(
        "/workbench/agents/identity/control",
        data={"status": "paused", "expected_version": ""},
    )
    client.post(
        "/workbench/agents/sending/control",
        data={"status": "enabled", "expected_version": ""},
    )
    events = list(
        db_session.scalars(
            select(AuditEvent).where(AuditEvent.entity_type == "workbench_command")
        ).all()
    )
    states = {event.new_state for event in events}
    assert "accepted" in states
    assert "refused" in states


def test_an_operator_command_repeated_with_a_current_version_is_idempotent(
    client: TestClient, scenario: workbench_scenario.Scenario, db_session: Session
) -> None:
    client.post(
        "/workbench/agents/identity/control",
        data={"status": "paused", "expected_version": ""},
    )
    control = db_session.get(AgentControl, AgentIdentifier.IDENTITY)
    assert control is not None
    version = control.version
    client.post(
        "/workbench/agents/identity/control",
        data={"status": "paused", "expected_version": str(version)},
    )
    db_session.refresh(control)
    assert control.version == version
    assert control.status is AgentControlStatus.PAUSED


def test_the_api_and_the_page_agree_about_agent_state(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    """One vocabulary: the Phase 2 API and the HTML must not disagree."""

    client.post(
        "/workbench/agents/identity/control",
        data={"status": "paused", "expected_version": ""},
    )
    api = client.get("/api/agents").json()
    identity = next(row for row in api["agents"] if row["agent_id"] == "identity")
    assert identity["configured_status"] == "paused"
    page = client.get("/workbench/agents/identity")
    assert page.status_code == 200
    assert "paused" in page.text


# --- Deterministic stub reader ------------------------------------------------


class _StubReader:
    """A fixed read model, for render tests that must not touch the database."""

    def __init__(self, overview: WorkbenchOverviewView) -> None:
        self._overview = overview

    def overview(self) -> WorkbenchOverviewView:
        return self._overview

    def agent_detail(self, agent_id, *, campaign_id=None):  # type: ignore[no-untyped-def]
        return None

    def campaign_execution(self, campaign_id, *, stage=None, limit=50, offset=0):  # type: ignore[no-untyped-def]
        return None

    def contact_execution(self, campaign_id, campaign_contact_id):  # type: ignore[no-untyped-def]
        return None

    def jobs(self, **_kwargs):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def job(self, job_id):  # type: ignore[no-untyped-def]
        return None


def _stub_overview() -> WorkbenchOverviewView:
    control = ControlView(
        agent_id=AgentIdentifier.SENDING,
        display_name="Sending Agent",
        position=8,
        status=AgentControlStatus.DISABLED,
        source="registry_default",
        reason=None,
        implemented=False,
        global_status=AgentControlStatus.DISABLED,
    )
    card = AgentCardView(
        agent_id=AgentIdentifier.SENDING,
        display_name="Sending Agent",
        position=8,
        control=control,
        queue=QueueCounts(by_status={"queued": 3, "failed": 1}, terminal_failures=1),
    )
    return WorkbenchOverviewView(
        generated_at=datetime(2026, 7, 29, 12, 0, tzinfo=UTC),
        agents=(card,),
        queue=QueueCounts(by_status={"queued": 3, "failed": 1}, terminal_failures=1),
        campaigns=(),
        recent_activity=(),
        sending_control=control,
    )


def test_the_overview_template_renders_from_the_port_alone(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No database, no Phase 2 query — only the view model the port promises."""

    from app.web import routes as web_routes

    monkeypatch.setattr(web_routes, "_reader", lambda _db: _StubReader(_stub_overview()))
    response = client.get("/workbench")
    assert response.status_code == 200
    assert "Sending Agent" in response.text
    assert "Sending is stopped" in response.text
    assert "No Campaigns exist yet" in response.text


# --- auto-update --------------------------------------------------------------


def test_the_monitor_pages_opt_into_auto_update(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    """These are the pages whose whole point is a queue that is moving.

    An operator was reloading by hand to learn what changed. The refresh swaps only
    the main content, so the nav and any flash message stay put and scroll position
    survives.
    """

    for path in (
        "/workbench",
        f"/workbench/campaigns/{scenario.campaign.id}",
        "/workbench/jobs",
    ):
        body = client.get(path).text
        assert 'data-live="5"' in body, f"{path} must opt in"
        assert "live.js" in body, f"{path} must load the refresher"


def test_every_other_page_still_renders_without_any_script(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    """The no-JavaScript convention still holds everywhere it can.

    Auto-update is a considered exception on the monitor pages, not a general
    licence — so a page that did not ask for it must contain no script tag at all.
    """

    for path in ("/campaigns", "/contacts", "/companies", "/knowledge-base"):
        body = client.get(path).text
        assert "<script" not in body.lower(), f"{path} must stay script-free"
        assert "data-live" not in body, f"{path} must not opt in"
