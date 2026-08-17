"""The customer-facing product at ``/app``, driven over HTTP.

The shell is four destinations — Today · Campaigns · People · Library — with a
role-gated Admin entry, and the Campaign is a workspace with Overview / People /
Setup / Activity tabs. Three things these tests protect, in order:

1. **The admin Workbench is still there and still works** at ``/admin``.
2. **Nothing technical leaks into the customer product** — no Agent tiles, no
   queue vocabulary, no future-feature stubs, and no retired destination that
   still renders as one.
3. **Real state reaches the page.** Fixtures build state through the services
   (``tests.workbench_scenario``) and assertions look for what was committed.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator

import pytest
from app.api.deps import get_db
from app.core.config import get_settings
from app.main import create_app
from app.models.campaign import Campaign, CampaignContact
from app.models.draft import DraftVersion
from app.models.enums import (
    AgentControlStatus,
    AgentIdentifier,
    CampaignStatus,
    SellerOfferingType,
)
from app.services import campaigns as campaign_service
from app.services.agents import controls as agent_controls
from app.services.agents import readiness as agent_readiness
from app.services.agents.registry import AGENT_SPECS, PIPELINE_ORDER
from app.services.personalization import cadence as cadence_service
from app.services.seller import campaign_offerings as seller_campaign_offerings
from app.services.seller import profile as seller_profile
from app.services.seller import records as seller_records
from app.web.v2 import shell
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tests import workbench_scenario


def _build_client(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    *,
    agent_workbench: bool = True,
    knowledge_base: bool = True,
) -> TestClient:
    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    if agent_workbench:
        monkeypatch.setenv("FEATURES__AGENT_WORKBENCH", "true")
    if knowledge_base:
        monkeypatch.setenv("FEATURES__SELLER_KNOWLEDGE_BASE", "true")
    get_settings.cache_clear()
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


@pytest.fixture()
def client(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    with _build_client(db_session, monkeypatch) as app_client:
        yield app_client


@pytest.fixture()
def bare_client(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Every optional switch off — the pages must still render honestly."""

    with _build_client(
        db_session, monkeypatch, agent_workbench=False, knowledge_base=False
    ) as app_client:
        yield app_client


@pytest.fixture()
def scenario(db_session: Session) -> workbench_scenario.Scenario:
    built = workbench_scenario.build(db_session)
    db_session.commit()
    return built


def _make_draft(
    session: Session,
    scenario: workbench_scenario.Scenario,
    key: str = "healthy",
    *,
    version: int = 1,
    subject: str = "Your Q3 batch-release target",
) -> DraftVersion:
    """A finished legacy single draft, as the Personalization Agent leaves one."""

    membership = scenario.membership(key)
    draft = DraftVersion(
        contact_id=membership.contact_id,
        campaign_id=membership.campaign_id,
        version_number=version,
        subject=subject,
        body=(
            "Alice — your published quality roadmap names batch-release review first.\n\n"
            "We keep a benchmark across 34 EU manufacturers on that metric.\n\n"
            "Worth twenty minutes?"
        ),
        rationale="Opened on the roadmap page because it is the only recent sourced fact.",
        created_by="personalization-agent",
    )
    session.add(draft)
    session.commit()
    return draft


def _campaign_url(scenario: workbench_scenario.Scenario) -> str:
    return f"/app/campaigns/{scenario.campaign.id}"


def _person_url(scenario: workbench_scenario.Scenario, key: str = "healthy") -> str:
    return f"/app/people/{scenario.contacts[key].id}?campaign={scenario.campaign.id}"


def _customer_body(html: str) -> str:
    """The page below the header, so nav labels do not satisfy content assertions."""

    return html.split("<main", 1)[1]


# ---------------------------------------------------------------------------
# The default experience, and the admin panel that keeps working
# ---------------------------------------------------------------------------


def test_root_lands_on_the_customer_interface(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/app"


def test_root_followed_through_renders_today(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Campaigns in motion" in response.text or "No Campaigns yet" in response.text


def test_admin_workbench_kept_its_overview_at_its_own_address(client: TestClient) -> None:
    response = client.get("/admin")
    assert response.status_code == 200
    assert "admin.css" in response.text
    assert "v2.css" not in response.text
    assert "Admin Workbench" in response.text
    legacy = client.get("/admin/legacy/overview")
    assert legacy.status_code == 200
    assert "app.css" in legacy.text


def test_admin_pages_are_untouched_by_the_new_interface(client: TestClient) -> None:
    for path in ("/imports", "/review", "/workbench"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "app.css" in response.text, path
        assert "v2.css" not in response.text, path


def test_admin_rail_offers_the_way_back_to_the_customer_interface(client: TestClient) -> None:
    assert 'href="/app"' in client.get("/admin").text


def test_customer_pages_never_load_the_admin_stylesheet(client: TestClient) -> None:
    for path in ("/app", "/app/campaigns", "/app/people", "/app/companies", "/app/library"):
        body = client.get(path).text
        assert "v2.css" in body, path
        assert "app.css" not in body, path


def test_the_whole_ui_stays_behind_the_workbench_switch(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FEATURES__WORKBENCH", raising=False)
    get_settings.cache_clear()
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as disabled:
        assert disabled.get("/app").status_code == 404
        assert disabled.get("/admin").status_code == 404
        assert disabled.get("/", follow_redirects=False).status_code == 404


# ---------------------------------------------------------------------------
# Every screen renders
# ---------------------------------------------------------------------------


V2_PAGES = (
    "/app",
    "/app/campaigns",
    "/app/campaigns/new",
    "/app/add-people",
    "/app/people",
    "/app/companies",
    "/app/library",
    "/app/library/company",
    "/app/library/offerings",
    "/app/library/proof-points",
    "/app/library/restricted-claims",
    "/app/library/personas",
    "/app/account/connections",
    "/app/admin",
    "/app/admin/agents",
    "/app/admin/suppressions",
)


@pytest.mark.parametrize("path", V2_PAGES)
def test_every_page_renders_on_an_empty_database(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200, path


@pytest.mark.parametrize("path", V2_PAGES)
def test_every_page_renders_with_real_pipeline_state(
    client: TestClient, scenario: workbench_scenario.Scenario, path: str
) -> None:
    response = client.get(path)
    assert response.status_code == 200, path


@pytest.mark.parametrize("path", V2_PAGES)
def test_every_page_renders_with_every_optional_switch_off(
    bare_client: TestClient, path: str
) -> None:
    """A switched-off feature must produce an honest page, not a 500 and not a 404."""

    response = bare_client.get(path)
    assert response.status_code == 200, path


def test_every_campaign_tab_and_the_person_page_render(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    base = _campaign_url(scenario)
    for path in (base, f"{base}/people", f"{base}/setup", f"{base}/activity", f"{base}/add-people"):
        assert client.get(path).status_code == 200, path
    contact_id = scenario.contacts["healthy"].id
    assert client.get(f"/app/people/{contact_id}").status_code == 200
    assert client.get(_person_url(scenario)).status_code == 200


def test_unknown_ids_render_the_not_found_page_not_a_crash(client: TestClient) -> None:
    for path in (
        "/app/campaigns/not-a-uuid",
        f"/app/campaigns/{uuid.uuid4()}",
        f"/app/campaigns/{uuid.uuid4()}/people",
        f"/app/campaigns/{uuid.uuid4()}/setup",
        f"/app/campaigns/{uuid.uuid4()}/activity",
        "/app/people/not-a-uuid",
        f"/app/people/{uuid.uuid4()}",
        f"/app/companies/{uuid.uuid4()}",
    ):
        response = client.get(path)
        assert response.status_code == 404, path
        assert "Not found" in response.text


# ---------------------------------------------------------------------------
# The shell
# ---------------------------------------------------------------------------


def test_the_customer_navigation_is_exactly_four_destinations(client: TestClient) -> None:
    assert [item.key for item in shell.primary_nav()] == ["today", "campaigns", "people", "library"]
    header = client.get("/app").text.split("<main", 1)[0]
    for label, href in (
        ("Today", "/app"),
        ("Campaigns", "/app/campaigns"),
        ("People", "/app/people"),
        ("Library", "/app/library"),
    ):
        assert f'href="{href}"' in header, label
        assert f">{label}<" in header, label
    for retired in ("Emails", "Review", "Contacts", "Knowledge Base", "Capture"):
        assert f">{retired}<" not in header, retired
    assert "Add people" in header
    # Authentication is not enforced in this client, so every request is an
    # administrator's and the Admin entry renders.
    assert 'href="/app/admin"' in header
    assert "v2-nav-badge" not in header


def test_the_account_menu_offers_connections_not_machinery(client: TestClient) -> None:
    header = client.get("/app").text.split("<main", 1)[0]
    assert 'href="/app/account/connections"' in header
    for retired in ("Agent settings", "Operator Workbench", "Sending accounts", "Suppression list"):
        assert retired not in header, retired


def test_no_customer_template_uses_an_inline_style_attribute(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    """The design system lives in one stylesheet, not scattered across markup.

    The one exception is the outcome bar, whose segment widths are data.
    """

    for path in (*V2_PAGES, _campaign_url(scenario), f"{_campaign_url(scenario)}/setup"):
        body = client.get(path).text
        for match in re.findall(r' style="([^"]*)"', body):
            assert match.startswith("width:"), (path, match)


# ---------------------------------------------------------------------------
# Campaigns list and creation
# ---------------------------------------------------------------------------


def test_the_campaign_list_shows_the_three_outcomes_and_a_create_action(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    body = client.get("/app/campaigns").text
    for column in ("State", "People", "Processing", "Ready for Sending", "Could not prepare"):
        assert column in body, column
    assert "New Campaign" in body
    assert scenario.campaign.name in body
    assert f'href="/app/campaigns/{scenario.campaign.id}"' in body
    assert ">Active<" in body


def test_an_empty_database_shows_the_campaign_empty_state(client: TestClient) -> None:
    body = client.get("/app/campaigns").text
    assert "No Campaigns yet" in body
    assert "Create your first Campaign" in body


def test_creating_a_campaign_starts_it_and_opens_its_overview(
    client: TestClient, db_session: Session
) -> None:
    response = client.post(
        "/app/campaigns/new",
        data={"name": "Pune manufacturing leaders", "description": "Q3 pilot"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    campaign = db_session.scalars(
        select(Campaign).where(Campaign.name == "Pune manufacturing leaders")
    ).one()
    assert response.headers["location"].startswith(f"/app/campaigns/{campaign.id}")
    assert campaign.status is CampaignStatus.ACTIVE
    assert campaign.execution_enabled is True

    body = client.get(f"/app/campaigns/{campaign.id}").text
    assert "Pune manufacturing leaders" in body
    assert "Nobody has been added yet" in body


def test_creating_a_campaign_attaches_the_chosen_offering(
    client: TestClient, db_session: Session
) -> None:
    offering = seller_records.create_offering(
        db_session,
        name="Medical-device QA benchmarking dataset",
        offering_type=SellerOfferingType.RESEARCH_REPORT,
        created_by="test",
    )
    db_session.commit()
    client.post(
        "/app/campaigns/new",
        data={"name": "QA leaders", "offering_id": str(offering.id)},
        follow_redirects=False,
    )
    campaign = db_session.scalars(select(Campaign).where(Campaign.name == "QA leaders")).one()
    linked = seller_campaign_offerings.offerings_for_campaign(db_session, campaign.id)
    assert [item.id for item in linked] == [offering.id]
    assert (
        "Medical-device QA benchmarking dataset" in client.get(f"/app/campaigns/{campaign.id}").text
    )


def test_a_duplicate_campaign_name_is_refused_with_a_flash(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    response = client.post(
        "/app/campaigns/new", data={"name": scenario.campaign.name}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/app/campaigns/new?err=")
    assert "already exists" in client.get(response.headers["location"]).text


# ---------------------------------------------------------------------------
# Campaign Overview
# ---------------------------------------------------------------------------


def test_the_overview_reports_outcomes_ready_people_setup_and_activity(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    body = _customer_body(client.get(_campaign_url(scenario)).text)
    for heading in ("Where people stand", "Ready for Sending", "Setup", "Recent activity"):
        assert heading in body, heading
    assert "v2-outcome-bar" in body
    for legend in ("Processing", "Ready for Sending", "Could not prepare"):
        assert legend in body
    assert "Nobody is ready yet" in body
    # Overview / People / Setup / Activity tabs, and the persistent header counts.
    for tab in ("people", "setup", "activity"):
        assert f'href="{_campaign_url(scenario)}/{tab}"' in body, tab
    assert "people</a>" in body


def test_the_overview_carries_no_agent_or_queue_vocabulary(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    body = _customer_body(client.get(_campaign_url(scenario)).text)
    for word in ("Agent", "v2-pipe-stage", "job", "queue", "retry", "lease", "settings version"):
        assert word not in body, word


def test_a_paused_campaign_offers_resume_on_the_overview(
    client: TestClient, db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    campaign_service.apply_campaign_execution(
        db_session, scenario.campaign.id, enabled=False, actor="operator", reason="test"
    )
    body = client.get(_campaign_url(scenario)).text
    assert ">Paused<" in body
    assert "Paused. Resume the Campaign" in body
    assert "Resume Campaign" in body


def test_a_paused_campaign_still_says_paused_when_an_agent_is_also_off(
    client: TestClient, db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    """Two causes are live at once, and the customer's own is the one reported.

    A Campaign can be paused by its customer *and* held by an administrator
    setting simultaneously. Only one sentence is shown, so which one wins is a
    product decision rather than an implementation accident, and it belongs in a
    test that drives the real page.

    It is the pause. That is the state the customer put the Campaign into, it is
    the state they can leave, and answering "an administrator is holding this"
    would be a false account of their own action with no control attached to it.
    """

    campaign = db_session.get(Campaign, scenario.campaign.id)
    assert campaign is not None
    # Opt the Campaign in, so execution readiness is consulted at all, and hold
    # it: Research is disabled by registry default, which is exactly the state a
    # fresh deployment is in.
    campaign.cadence_config = cadence_service.with_campaign_opt_in(campaign, enabled=True)
    db_session.flush()
    agent_controls.set_global_control(
        db_session,
        agent_id=AgentIdentifier.RESEARCH,
        status=AgentControlStatus.DISABLED,
        reason="test setup",
    )
    campaign_service.apply_campaign_execution(
        db_session, campaign.id, enabled=False, actor="operator", reason="test"
    )
    db_session.flush()

    # The hold is real: this is not a test that passes because nothing is held.
    readiness = agent_readiness.execution_readiness(db_session, campaign=campaign)
    assert not readiness.runnable

    body = client.get(_campaign_url(scenario)).text
    assert "Paused. Resume the Campaign" in body
    assert "Resume Campaign" in body
    assert "Preparation is being held by an administrator setting." not in body
    # And still no Agent vocabulary in the customer's product.
    assert "Research Agent" not in _customer_body(body)


def test_an_active_campaign_shows_no_start_or_resume_prompt(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    body = client.get(_campaign_url(scenario)).text
    assert "Resume Campaign" not in body
    assert "Start Campaign" not in body


def test_could_not_prepare_reasons_are_plain_language(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    body = client.get(_campaign_url(scenario)).text
    assert "Could not prepare" in body
    assert "On the suppression list — never contacted" in body
    assert "View affected people" in body


# ---------------------------------------------------------------------------
# Campaign People
# ---------------------------------------------------------------------------


def test_campaign_people_lists_everyone_with_an_outcome_and_a_detail(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    body = client.get(f"{_campaign_url(scenario)}/people").text
    for label in ("All", "Processing", "Ready for Sending", "Could not prepare"):
        assert f"outcome={label.lower().replace(' ', '_')}" in body or label in body
    assert "Nakamura" in body
    assert "Pinto" in body
    assert "On the suppression list — never contacted" in body
    # The other Campaign's member never leaks in.
    assert "Marsh" not in body


def test_campaign_people_filters_by_outcome(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    stopped = client.get(f"{_campaign_url(scenario)}/people?outcome=could_not_prepare").text
    assert "Pinto" in stopped
    assert "Nakamura" not in stopped
    ready = client.get(f"{_campaign_url(scenario)}/people?outcome=ready_for_sending").text
    assert "Nobody here" in ready


def test_campaign_people_search_narrows_by_name_company_or_email(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    body = client.get(f"{_campaign_url(scenario)}/people?q=Northwind").text
    assert "Nakamura" in body
    assert "Pinto" not in body
    none = client.get(f"{_campaign_url(scenario)}/people?q=zzz-nobody").text
    assert "Nobody matches" in none


# ---------------------------------------------------------------------------
# Campaign Setup and lifecycle
# ---------------------------------------------------------------------------


def test_setup_renders_every_campaign_owned_decision(
    client: TestClient, db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    seller_records.create_offering(db_session, name="Benchmark dataset", created_by="test")
    db_session.commit()
    body = client.get(f"{_campaign_url(scenario)}/setup").text
    for name in ("name", "description", "offering_id", "primary_cta", "messaging_direction"):
        assert f'name="{name}"' in body, name
    for heading in ("General", "Offering and direction", "Preparation", "Access", "Lifecycle"):
        assert heading in body, heading
    assert "Archive Campaign" in body
    assert "Ready to prepare people" in body


def test_saving_setup_updates_name_note_direction_and_the_website_policy(
    client: TestClient, db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    offering = seller_records.create_offering(
        db_session, name="Benchmark dataset", created_by="test"
    )
    db_session.commit()
    response = client.post(
        f"{_campaign_url(scenario)}/setup",
        data={
            "name": "Pilot 100 — renamed",
            "description": "A new note",
            "messaging_direction": "Lead with the benchmark.",
            "primary_cta": "a 20-minute call",
            "offering_id": str(offering.id),
            "allow_provisional_domains": "on",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "ok=" in response.headers["location"]
    db_session.expire_all()
    campaign = db_session.get(Campaign, scenario.campaign.id)
    assert campaign.name == "Pilot 100 — renamed"
    assert campaign.description == "A new note"
    assert campaign.messaging_direction == "Lead with the benchmark."
    assert campaign.primary_cta == "a 20-minute call"
    assert campaign.allow_provisional_domains is True
    linked = seller_campaign_offerings.offerings_for_campaign(db_session, campaign.id)
    assert [item.id for item in linked] == [offering.id]

    # Saving without the checkbox turns the policy off; without an offering, detaches it.
    client.post(
        f"{_campaign_url(scenario)}/setup",
        data={"name": "Pilot 100 — renamed", "offering_id": ""},
        follow_redirects=False,
    )
    db_session.expire_all()
    campaign = db_session.get(Campaign, scenario.campaign.id)
    assert campaign.allow_provisional_domains is False
    assert seller_campaign_offerings.offerings_for_campaign(db_session, campaign.id) == []


def test_saving_setup_with_a_missing_name_is_rejected(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    response = client.post(f"{_campaign_url(scenario)}/setup", data={"description": "x"})
    assert response.status_code == 422


def test_pause_and_resume_use_the_execution_switch(
    client: TestClient, db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    lifecycle = f"{_campaign_url(scenario)}/lifecycle"
    response = client.post(lifecycle, data={"action": "pause"}, follow_redirects=False)
    assert response.status_code == 303
    db_session.expire_all()
    assert db_session.get(Campaign, scenario.campaign.id).execution_enabled is False
    assert ">Paused<" in client.get(_campaign_url(scenario)).text

    client.post(lifecycle, data={"action": "resume"}, follow_redirects=False)
    db_session.expire_all()
    assert db_session.get(Campaign, scenario.campaign.id).execution_enabled is True
    assert ">Active<" in client.get(_campaign_url(scenario)).text


def test_start_turns_a_draft_campaign_active(
    client: TestClient, db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    draft = scenario.other_campaign
    assert draft.status is CampaignStatus.DRAFT
    body = client.get(f"/app/campaigns/{draft.id}").text
    assert ">Draft<" in body
    assert "Start Campaign" in body
    client.post(f"/app/campaigns/{draft.id}/lifecycle", data={"action": "start"})
    db_session.expire_all()
    started = db_session.get(Campaign, draft.id)
    assert started.status is CampaignStatus.ACTIVE
    assert started.execution_enabled is True


def test_an_unknown_lifecycle_action_is_refused(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    response = client.post(
        f"{_campaign_url(scenario)}/lifecycle", data={"action": "delete"}, follow_redirects=False
    )
    assert "err=" in response.headers["location"]


def test_archiving_turns_execution_off_and_keeps_every_record(
    client: TestClient, db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    campaign_id = scenario.campaign.id
    memberships_before = db_session.scalar(
        select(func.count(CampaignContact.id)).where(CampaignContact.campaign_id == campaign_id)
    )
    assert memberships_before

    # A GET cannot archive anything.
    assert client.get(f"/app/campaigns/{campaign_id}/lifecycle").status_code == 405

    response = client.post(
        f"/app/campaigns/{campaign_id}/lifecycle",
        data={"action": "archive"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/app/campaigns?ok=")

    db_session.expire_all()
    campaign = db_session.get(Campaign, campaign_id)
    assert campaign is not None
    assert campaign.status is CampaignStatus.ARCHIVED
    assert campaign.execution_enabled is False
    assert (
        db_session.scalar(
            select(func.count(CampaignContact.id)).where(CampaignContact.campaign_id == campaign_id)
        )
        == memberships_before
    )

    detail = client.get(f"/app/campaigns/{campaign_id}").text
    assert ">Archived<" in detail
    # The Campaign header offers no Add people once archived.
    assert f'href="/app/campaigns/{campaign_id}/add-people"' not in _customer_body(detail)
    assert "Archive Campaign" not in client.get(f"/app/campaigns/{campaign_id}/setup").text


def test_archiving_a_missing_campaign_redirects_with_an_error(client: TestClient) -> None:
    response = client.post(
        f"/app/campaigns/{uuid.uuid4()}/lifecycle",
        data={"action": "archive"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "err=" in response.headers["location"]


def test_the_research_switch_toggles_the_live_opt_in(
    client: TestClient, db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    campaign = scenario.campaign
    assert (
        agent_controls.campaign_live_opt_in(
            db_session, campaign=campaign, agent_id=AgentIdentifier.RESEARCH
        )
        is False
    )
    response = client.post(
        f"{_campaign_url(scenario)}/setup/research", data={"allowed": "1"}, follow_redirects=False
    )
    assert "ok=" in response.headers["location"]
    db_session.expire_all()
    assert (
        agent_controls.campaign_live_opt_in(
            db_session,
            campaign=db_session.get(Campaign, campaign.id),
            agent_id=AgentIdentifier.RESEARCH,
        )
        is True
    )
    assert "Allowed" in client.get(_campaign_url(scenario)).text

    client.post(f"{_campaign_url(scenario)}/setup/research", data={"allowed": "0"})
    db_session.expire_all()
    assert (
        agent_controls.campaign_live_opt_in(
            db_session,
            campaign=db_session.get(Campaign, campaign.id),
            agent_id=AgentIdentifier.RESEARCH,
        )
        is False
    )


def test_the_legacy_edit_url_lands_on_setup(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    response = client.get(f"{_campaign_url(scenario)}/edit", follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"] == f"{_campaign_url(scenario)}/setup"


# ---------------------------------------------------------------------------
# Campaign Activity
# ---------------------------------------------------------------------------


def test_activity_lists_lifecycle_changes_and_people_added(
    client: TestClient, db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    client.post(f"{_campaign_url(scenario)}/lifecycle", data={"action": "pause"})
    body = _customer_body(client.get(f"{_campaign_url(scenario)}/activity").text)
    assert "Preparation paused" in body
    assert "Preparation started" in body
    assert "people added" in body
    for word in ("job", "lease", "retry", "worker"):
        assert word not in body.lower().replace("workbench", ""), word


# ---------------------------------------------------------------------------
# Add people
# ---------------------------------------------------------------------------


def test_add_people_without_a_campaign_asks_which_one(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    body = client.get("/app/add-people").text
    assert "Which Campaign?" in body
    assert scenario.campaign.name in body
    assert 'name="campaign"' in body


def test_add_people_without_any_campaign_points_at_creation(client: TestClient) -> None:
    body = client.get("/app/add-people").text
    assert "Create a Campaign first" in body
    assert 'href="/app/campaigns/new"' in body


def test_add_people_for_a_campaign_offers_the_three_sources(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    for path in (
        f"{_campaign_url(scenario)}/add-people",
        f"/app/add-people?campaign={scenario.campaign.id}",
    ):
        body = client.get(path).text
        assert f"Add people to {scenario.campaign.name}" in body, path
        for source in ("Chrome extension", "Google Sheets", "Import a file"):
            assert source in body, (path, source)
        # File import is a real link when the switch is on, an honest note when off.
        assert f'href="{_campaign_url(scenario)}/imports"' in body or "switched off" in body, path


# ---------------------------------------------------------------------------
# Legacy destinations resolve into the new ones
# ---------------------------------------------------------------------------


def test_legacy_urls_redirect_into_the_four_destinations(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    contact_id = scenario.contacts["healthy"].id
    campaign_id = scenario.campaign.id
    expectations = {
        "/app/review": "/app/campaigns",
        f"/app/review?campaign={campaign_id}": f"/app/campaigns/{campaign_id}#ready",
        "/app/contacts": "/app/people",
        "/app/contacts?q=Nakamura": "/app/people?q=Nakamura",
        f"/app/contacts/{contact_id}?campaign={campaign_id}": (
            f"/app/people/{contact_id}?campaign={campaign_id}"
        ),
        "/app/knowledge": "/app/library",
        "/app/knowledge/offerings": "/app/library/offerings",
        "/app/capture": "/app/add-people",
        "/app/sending": "/app",
        "/app/replies": "/app",
        "/app/sequences": "/app",
        "/app/analytics": "/app",
        "/app/agents": "/app/admin/agents",
        "/app/agents?agent=email": "/app/admin/agents?agent=email",
        "/app/suppressions": "/app/admin/suppressions",
    }
    for path, target in expectations.items():
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 308, path
        assert response.headers["location"] == target, path


def test_a_legacy_review_link_to_a_foreign_campaign_falls_back_to_the_list(
    client: TestClient,
) -> None:
    response = client.get(f"/app/review?campaign={uuid.uuid4()}", follow_redirects=False)
    assert response.headers["location"] == "/app/campaigns"


# ---------------------------------------------------------------------------
# People and Companies
# ---------------------------------------------------------------------------


def test_people_lists_captured_people_with_the_local_switch(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    body = client.get("/app/people").text
    assert "Nakamura" in body
    assert "Brandt" in body
    assert 'href="/app/companies"' in body
    assert "Add people" in body
    assert '<h1 class="v2-h1">People</h1>' in body


def test_people_filters_and_search_are_honoured(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    found = client.get("/app/people?q=Nakamura").text
    assert "Nakamura" in found
    assert "Brandt" not in found


def test_a_person_in_no_campaign_says_so(
    client: TestClient, db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    from app.models.contact import Contact

    orphan = Contact(
        first_name="Unenrolled",
        last_name="Person",
        company_name="Nowhere",
        natural_key="unenrolled|person|nowhere",
    )
    db_session.add(orphan)
    db_session.commit()
    body = client.get(f"/app/people/{orphan.id}").text
    assert "Not in a Campaign" in body


def test_companies_render_with_the_local_switch(
    client: TestClient, db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    body = client.get("/app/companies").text
    assert 'href="/app/people"' in body
    assert "no address can be built" in body or "No website" in body


def test_a_company_with_no_dossier_says_nothing_was_researched(
    client: TestClient, db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    from app.models.company import Company

    company = Company(name="Vantage Holdings", domain="vantage.example.com")
    db_session.add(company)
    db_session.commit()
    body = client.get(f"/app/companies/{company.id}").text
    assert "Nothing researched yet" in body


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------


def test_the_library_shows_entered_records(client: TestClient, db_session: Session) -> None:
    seller_profile.save_profile(
        db_session,
        name="Verified Market Research",
        short_description="Market research in niche and emerging markets.",
        industries_served=["Medical devices"],
        updated_by="test",
    )
    seller_records.create_offering(
        db_session,
        name="Medical-device QA benchmarking dataset",
        offering_type=SellerOfferingType.RESEARCH_REPORT,
        short_description="Benchmarks across 34 EU manufacturers.",
        created_by="test",
    )
    db_session.commit()

    overview = client.get("/app/library").text
    assert "Verified Market Research" in overview
    assert '<h1 class="v2-h1">Library</h1>' in overview
    offerings = client.get("/app/library/offerings").text
    assert "Medical-device QA benchmarking dataset" in offerings


def test_the_library_says_when_the_feature_is_off(bare_client: TestClient) -> None:
    body = bare_client.get("/app/library").text
    assert "FEATURES__SELLER_KNOWLEDGE_BASE" in body
    assert "not empty" in body


def test_an_unknown_library_section_falls_back_to_the_overview(client: TestClient) -> None:
    response = client.get("/app/library/nonsense")
    assert response.status_code == 200
    assert "What is entered, and what is missing" in response.text


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------


def test_connections_says_gmail_is_unavailable_when_the_feature_is_off(
    client: TestClient,
) -> None:
    body = client.get("/app/account/connections").text
    assert "Gmail" in body
    assert "not available in this environment" in body


# ---------------------------------------------------------------------------
# Admin inside the product
# ---------------------------------------------------------------------------


def test_the_admin_landing_lists_the_machinery(client: TestClient) -> None:
    body = client.get("/app/admin").text
    for item in (
        "Capabilities",
        "Users &amp; Access",
        "Data tools",
        "Diagnostics",
        "Operator Workbench",
    ):
        assert item in body, item
    for href in (
        "/app/admin/agents",
        "/app/admin/users",
        "/app/admin/data-tools",
        "/app/admin/diagnostics",
        "/admin",
    ):
        assert f'href="{href}"' in body, href


def test_agent_settings_shows_registry_facts_as_facts_not_settings(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    body = client.get("/app/admin/agents?agent=email").text
    assert "Registry facts" in body
    assert "Not settings" in body
    assert str(AGENT_SPECS[AgentIdentifier.EMAIL].max_attempts) in body


def test_agent_settings_refuses_to_offer_a_switch_for_an_unimplemented_agent(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    body = client.get("/app/admin/agents?agent=sending").text
    assert "no adapter registered" in body
    assert "cannot be enabled at all" in body


def _form_version(client: TestClient, agent: str) -> str:
    """The control version the page rendered, read back out of the form."""

    page = client.get(f"/app/admin/agents?agent={agent}").text
    match = re.search(r'name="expected_version" value="([^"]*)"', page)
    assert match is not None, "the control form must carry the version the page saw"
    return match.group(1)


def test_changing_an_agent_control_carries_the_expected_version(
    client: TestClient, db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    from app.models.agent import AgentControl

    response = client.post(
        "/app/admin/agents/research/control",
        data={
            "status": "enabled",
            "expected_version": _form_version(client, "research"),
            "reason": "turning research on",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    control = db_session.scalars(
        select(AgentControl).where(AgentControl.agent_id == AgentIdentifier.RESEARCH)
    ).one()
    assert control.status.value == "enabled"


def test_a_stale_control_version_is_refused_and_the_reason_is_shown(
    client: TestClient, db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    stale = _form_version(client, "research")
    client.post(
        "/app/admin/agents/research/control",
        data={"status": "paused", "expected_version": stale},
    )
    response = client.post(
        "/app/admin/agents/research/control",
        data={"status": "enabled", "expected_version": stale},
        follow_redirects=False,
    )
    assert "err=" in response.headers["location"]

    from app.models.agent import AgentControl

    control = db_session.scalars(
        select(AgentControl).where(AgentControl.agent_id == AgentIdentifier.RESEARCH)
    ).one()
    assert control.status.value == "paused", "the stale write must not have landed"


def test_enabling_an_unimplemented_agent_is_refused(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    response = client.post(
        "/app/admin/agents/sending/control",
        data={"status": "enabled", "expected_version": "0"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "adapter" in response.text.lower()


def test_agent_settings_says_when_the_monitor_is_off(bare_client: TestClient) -> None:
    body = bare_client.get("/app/admin/agents").text
    assert "FEATURES__AGENT_WORKBENCH" in body
    assert "Nothing is running unseen" in body


def test_campaign_diagnostics_show_all_nine_agents_and_the_stopped_people(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    base = f"/app/admin/campaigns/{scenario.campaign.id}/diagnostics"
    body = client.get(base).text
    for agent_id in PIPELINE_ORDER:
        assert AGENT_SPECS[agent_id].display_name in body
    assert scenario.campaign.name in body

    focused = client.get(f"{base}?stage=research").text
    assert "stopped people" in focused
    assert "Live work" in focused
    assert f"/app/admin/campaigns/{scenario.campaign.id}/agents/research/live" in focused


def test_the_suppression_list_is_read_only_and_says_why(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    body = client.get("/app/admin/suppressions").text
    assert "gerald.pinto@ashcroft.example.com" in body
    assert "Read-only here on purpose" in body
    assert "<form" not in body.split("<main", 1)[1]
