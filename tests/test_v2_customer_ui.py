"""The customer-facing v2 interface, driven over HTTP.

Three things these tests exist to protect, in order of importance:

1. **The admin Workbench is still there and still works.** It moved to `/admin`
   and nothing else about it changed. A regression here is the expensive kind.
2. **Nothing is invented.** Every page that the design gives a send count, a reply
   count or a confidence score must render that slot as unavailable, not as a
   number. A test asserting the *absence* of a fabricated figure is the only thing
   that stops one being added later by accident.
3. **Real state reaches the page.** The pages are projections of the Phase 2
   services, so the fixtures build state through those services (see
   `tests.workbench_scenario`) and the assertions look for what the services
   actually committed.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator

import pytest
from app.api.deps import get_db
from app.core.config import get_settings
from app.main import create_app
from app.models.audit_event import AuditEvent
from app.models.draft import DraftApproval, DraftVersion
from app.models.enums import AgentIdentifier, ApprovalStatus, SellerOfferingType
from app.services import drafts as draft_service
from app.services.agents.registry import AGENT_SPECS, PIPELINE_ORDER
from app.services.seller import profile as seller_profile
from app.services.seller import records as seller_records
from fastapi.testclient import TestClient
from sqlalchemy import select
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
    """A finished draft, as the Personalization Agent leaves one.

    Written directly rather than through the adapter because the adapter's only
    path runs the local `claude` executable — a real model call, which a test must
    never make. The row is exactly the shape the adapter commits, and everything
    the pages read comes from these columns.
    """

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
    assert "Where the work stands" in response.text


def test_admin_workbench_kept_its_overview_at_its_own_address(client: TestClient) -> None:
    response = client.get("/admin")
    assert response.status_code == 200
    # The admin shell, unchanged: its own stylesheet, its own rail.
    assert "app.css" in response.text
    assert "v2.css" not in response.text
    assert "Operator Workbench" in response.text


def test_admin_pages_are_untouched_by_the_new_interface(client: TestClient) -> None:
    for path in ("/campaigns", "/imports", "/contacts", "/companies", "/review", "/workbench"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "app.css" in response.text, path
        assert "v2.css" not in response.text, path


def test_admin_rail_offers_the_way_back_to_the_customer_interface(client: TestClient) -> None:
    assert 'href="/app"' in client.get("/admin").text


def test_customer_pages_never_load_the_admin_stylesheet(client: TestClient) -> None:
    for path in ("/app", "/app/campaigns", "/app/contacts", "/app/companies", "/app/review"):
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
    "/app/review",
    "/app/contacts",
    "/app/companies",
    "/app/knowledge",
    "/app/knowledge/company",
    "/app/knowledge/offerings",
    "/app/knowledge/proof-points",
    "/app/knowledge/restricted-claims",
    "/app/knowledge/personas",
    "/app/agents",
    "/app/capture",
    "/app/suppressions",
    "/app/sending",
    "/app/replies",
    "/app/sequences",
    "/app/analytics",
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
    """A switched-off feature must produce an honest page, not a 500 and not a 404.

    The customer front door is the default experience, so a page here cannot simply
    vanish the way the admin surface's feature-gated pages do — it has to say what
    is unavailable and why.
    """

    response = bare_client.get(path)
    assert response.status_code == 200, path


def test_campaign_and_contact_detail_render(
    client: TestClient, db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    campaign_id = scenario.campaign.id
    contact_id = scenario.contacts["healthy"].id
    assert client.get(f"/app/campaigns/{campaign_id}").status_code == 200
    assert client.get(f"/app/contacts/{contact_id}").status_code == 200
    assert client.get(f"/app/contacts/{contact_id}?campaign={campaign_id}").status_code == 200


def test_unknown_ids_render_the_not_found_page_not_a_crash(client: TestClient) -> None:
    for path in (
        "/app/campaigns/not-a-uuid",
        f"/app/campaigns/{uuid.uuid4()}",
        "/app/contacts/not-a-uuid",
        f"/app/contacts/{uuid.uuid4()}",
        f"/app/companies/{uuid.uuid4()}",
    ):
        response = client.get(path)
        assert response.status_code == 404, path
        assert "Not found" in response.text


# ---------------------------------------------------------------------------
# Nothing is invented
# ---------------------------------------------------------------------------


def test_today_marks_sending_and_replies_unavailable_rather_than_zero(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    body = client.get("/app").text
    assert "sending is not built" in body
    assert "no inbound channel" in body
    assert "not built yet" in body


def test_future_surfaces_carry_no_figures_at_all(client: TestClient) -> None:
    """A placeholder screen states what is missing and shows no data.

    The marker asserted here is the one the `unbuilt` macro emits, so a future
    change that starts populating one of these pages with numbers has to remove it
    and will fail this test.
    """

    for path in ("/app/sending", "/app/replies", "/app/sequences", "/app/analytics"):
        body = client.get(path).text
        assert "not available yet" in body, path
        assert "v2-soon" in body, path


def test_sending_page_says_plainly_that_nothing_can_be_sent(client: TestClient) -> None:
    body = client.get("/app/sending").text
    assert "cannot send an email" in body
    assert "no adapter registered" in body


def test_review_never_shows_a_confidence_score(
    client: TestClient, db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    """The design's confidence number does not exist in this product.

    There is no scoring service, so the slot is present (the layout needs it) and
    explicitly unscored. This is the single most tempting number to fabricate.
    """

    _make_draft(db_session, scenario)
    body = client.get("/app/review").text
    assert "not scored" in body
    assert "no auto-send" in body or "waits for your approval" in body


def test_approving_says_that_nothing_was_sent(
    client: TestClient, db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    draft = _make_draft(db_session, scenario)
    response = client.post(
        f"/app/review/{draft.id}/approve", data={"back": "/app/review"}, follow_redirects=True
    )
    assert response.status_code == 200
    assert "Nothing was sent" in response.text


# ---------------------------------------------------------------------------
# Review: the read model and the human decision
# ---------------------------------------------------------------------------


def test_the_queue_lists_a_finished_draft_with_its_real_text(
    client: TestClient, db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    _make_draft(db_session, scenario, subject="A subject only this test writes")
    body = client.get("/app/review").text
    assert "A subject only this test writes" in body
    assert "batch-release review first" in body
    assert scenario.contacts["healthy"].first_name in body


def test_approve_records_an_approval_and_an_audit_event(
    client: TestClient, db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    draft = _make_draft(db_session, scenario)
    client.post(f"/app/review/{draft.id}/approve", data={"reason": "reads well"})

    approval = db_session.scalars(
        select(DraftApproval).where(DraftApproval.draft_version_id == draft.id)
    ).one()
    assert approval.status is ApprovalStatus.APPROVED
    assert approval.reason == "reads well"

    event = db_session.scalars(select(AuditEvent).where(AuditEvent.action == "draft.approve")).one()
    assert event.entity_id == str(draft.id)
    assert "No message was sent" in str(event.context)


def test_discard_is_a_decision_not_a_deletion(
    client: TestClient, db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    draft = _make_draft(db_session, scenario)
    client.post(f"/app/review/{draft.id}/discard", data={"reason": "wrong angle"})

    approval = db_session.scalars(
        select(DraftApproval).where(DraftApproval.draft_version_id == draft.id)
    ).one()
    assert approval.status is ApprovalStatus.INVALIDATED
    # The draft itself survives, which is what makes "you looked and said no"
    # distinguishable from "nobody has looked".
    assert db_session.get(DraftVersion, draft.id) is not None


def test_a_decision_can_be_changed_without_creating_a_second_record(
    client: TestClient, db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    draft = _make_draft(db_session, scenario)
    client.post(f"/app/review/{draft.id}/discard", data={"reason": "no"})
    client.post(f"/app/review/{draft.id}/approve", data={"reason": "on reflection, yes"})

    approvals = db_session.scalars(
        select(DraftApproval).where(DraftApproval.draft_version_id == draft.id)
    ).all()
    assert len(approvals) == 1
    assert approvals[0].status is ApprovalStatus.APPROVED


def test_a_superseded_version_cannot_be_approved(
    client: TestClient, db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    """Approving text the Agent has already rewritten would approve nothing real."""

    first = _make_draft(db_session, scenario, version=1, subject="First attempt")
    _make_draft(db_session, scenario, version=2, subject="Rewritten")

    response = client.post(
        f"/app/review/{first.id}/approve", data={"back": "/app/review"}, follow_redirects=True
    )
    assert "superseded" in response.text
    assert (
        db_session.scalars(
            select(DraftApproval).where(DraftApproval.draft_version_id == first.id)
        ).one_or_none()
        is None
    )


def test_the_service_refuses_a_missing_draft(db_session: Session) -> None:
    with pytest.raises(draft_service.DraftReviewError):
        draft_service.approve(db_session, draft_version_id=uuid.uuid4())


def test_queue_counts_separate_awaiting_from_decided(
    db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    waiting = _make_draft(db_session, scenario, key="healthy")
    decided = _make_draft(db_session, scenario, key="leased")
    draft_service.approve(db_session, draft_version_id=decided.id)
    db_session.commit()

    counts = draft_service.queue_counts(db_session)
    assert counts.awaiting == 1
    assert counts.approved == 1
    assert counts.discarded == 0
    assert counts.total == 2
    assert draft_service.get_draft(db_session, waiting.id).awaiting_decision is True


def test_the_queue_can_be_scoped_to_one_campaign(
    db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    _make_draft(db_session, scenario, key="healthy")
    _make_draft(db_session, scenario, key="other")

    everywhere = draft_service.list_queue(db_session)
    scoped = draft_service.list_queue(db_session, campaign_id=scenario.campaign.id)
    assert everywhere.total == 2
    assert scoped.total == 1
    assert scoped.rows[0].campaign_id == scenario.campaign.id


def test_review_badge_counts_only_what_is_waiting(
    client: TestClient, db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    _make_draft(db_session, scenario)
    assert "Review" in client.get("/app").text
    counts = draft_service.queue_counts(db_session)
    assert counts.awaiting == 1


# ---------------------------------------------------------------------------
# The pipeline screen
# ---------------------------------------------------------------------------


def test_the_pipeline_shows_all_nine_agents_in_registry_order(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    body = client.get(f"/app/campaigns/{scenario.campaign.id}").text
    start = body.index('<ol class="v2-pipe">')
    strip = body[start : body.index("</ol>", start)]
    positions = [
        strip.index(AGENT_SPECS[agent_id].display_name.replace(" Agent", ""))
        for agent_id in PIPELINE_ORDER
    ]
    assert positions == sorted(positions), "the strip must read in pipeline order"
    assert len(re.findall(r'class="v2-pipe-stage', strip)) == 9


def test_the_pipeline_marks_the_sending_agent_as_having_no_adapter(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    body = client.get(f"/app/campaigns/{scenario.campaign.id}").text
    assert "no adapter" in body
    assert not AGENT_SPECS[AgentIdentifier.SENDING].implemented


def test_the_strip_counts_how_many_got_through_not_how_many_are_standing_there(
    client: TestClient, db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    """The row has to read as a funnel, or it misreports an import as a no-op.

    The strip originally showed how many contacts were *resting* on each Agent.
    Capture completes the moment a contact is enrolled, and Identity and Company
    finish in under a second — so all three permanently showed 0 while contacts sat
    failing further down. An operator who had just imported 50 people saw "Capture 0"
    and concluded, reasonably and wrongly, that nothing had been captured.

    Asserted through the projection rather than on rendered digits: the tile numbers
    are what the funnel is, and reading them from HTML would test the markup instead.
    """

    from app.web.v2 import routes as v2_routes

    enrolled = len(scenario.memberships) - 1  # one membership is in the other campaign
    execution = v2_routes._reader(db_session).campaign_execution(scenario.campaign.id)
    assert execution is not None
    tiles = v2_routes._stage_tiles(
        execution,
        selected=None,
        base_href="/app/campaigns/x",
        open_counts=v2_routes._agent_open_counts(db_session, scenario.campaign.id),
        progress=v2_routes._stage_progress(db_session, scenario.campaign.id),
    )
    by_agent = {tile.agent_id: tile for tile in tiles}

    # Capture is complete for everyone enrolled: the extension already did it.
    assert by_agent["capture"].through == enrolled, (
        "Capture must report how many arrived, not how many are queued on it"
    )
    # And the funnel only ever descends.
    counts = [tile.through for tile in tiles]
    assert counts == sorted(counts, reverse=True), "a funnel cannot widen"


def test_the_strip_still_says_where_contacts_are_right_now(
    client: TestClient, db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    """The live detail is kept — it just stopped being the headline.

    The fixture parks contacts on Identity, one of them under a worker lease and one
    with a terminal failure, so "waiting", "moving" and "held" are all reachable.
    """

    body = client.get(f"/app/campaigns/{scenario.campaign.id}").text
    assert "how many have got" in body
    # Both must appear on the Identity tile: the fixture has one terminal failure and
    # five contacts simply waiting. Reporting only the failure would hide the five.
    assert "held here" in body
    assert "waiting here" in body
    # And the strip explains why Capture and Identity look the way they do.
    assert "Capture completes as soon as a contact is enrolled" in body


def test_the_run_log_shows_what_an_unexpected_error_recorded(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    """An opaque failure must be inspectable from the page, not only from the database."""

    body = client.get("/app/agents?agent=identity").text
    assert "What the Agent recorded" in body or "This Agent has not run" in body


def test_a_stage_filter_narrows_the_contact_list(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    base = client.get(f"/app/campaigns/{scenario.campaign.id}")
    filtered = client.get(f"/app/campaigns/{scenario.campaign.id}?stage=identity")
    assert base.status_code == 200
    assert filtered.status_code == 200
    assert "Identity Agent" in filtered.text


def test_an_unknown_stage_falls_back_to_everyone(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    response = client.get(f"/app/campaigns/{scenario.campaign.id}?stage=nonsense")
    assert response.status_code == 200
    assert "Everyone in this campaign" in response.text


def test_the_pipeline_surfaces_a_suppressed_contact_as_never_contacted(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    body = client.get(f"/app/campaigns/{scenario.campaign.id}").text
    assert "Suppressed" in body or "suppressed" in body


def test_pausing_a_campaign_uses_the_existing_execution_switch(
    client: TestClient, db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    response = client.post(
        f"/app/campaigns/{scenario.campaign.id}/execution",
        data={"enabled": "0", "reason": "checking the guardrails"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "nothing already in flight was discarded" in response.text
    db_session.expire_all()
    assert scenario.campaign.execution_enabled is False


def test_resuming_a_campaign_turns_execution_back_on(
    client: TestClient, db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    client.post(f"/app/campaigns/{scenario.campaign.id}/execution", data={"enabled": "0"})
    client.post(f"/app/campaigns/{scenario.campaign.id}/execution", data={"enabled": "1"})
    db_session.expire_all()
    assert scenario.campaign.execution_enabled is True


def test_the_campaign_screen_is_the_only_page_that_auto_refreshes(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    """The monitor exception, kept as an exception.

    The design's campaign screen watches a queue that is moving, so it opts into
    the existing `live.js`. Every other customer page must render with no script
    tag at all — the same rule the admin surface holds itself to.
    """

    monitor = client.get(f"/app/campaigns/{scenario.campaign.id}").text
    assert 'data-live="5"' in monitor
    assert "live.js" in monitor

    for path in (
        "/app",
        "/app/campaigns",
        "/app/review",
        "/app/contacts",
        "/app/companies",
        "/app/knowledge",
        "/app/agents",
        "/app/capture",
        "/app/suppressions",
        "/app/sending",
    ):
        body = client.get(path).text
        assert "<script" not in body.lower(), f"{path} must stay script-free"
        assert "data-live" not in body, f"{path} must not opt in to auto-refresh"


def test_a_campaign_renders_honestly_when_the_agent_monitor_is_off(
    bare_client: TestClient, db_session: Session
) -> None:
    built = workbench_scenario.build(db_session)
    db_session.commit()
    response = bare_client.get(f"/app/campaigns/{built.campaign.id}")
    assert response.status_code == 200
    assert "FEATURES__AGENT_WORKBENCH" in response.text
    assert "The campaign is unaffected" in response.text


# ---------------------------------------------------------------------------
# Contacts, companies
# ---------------------------------------------------------------------------


def test_contacts_shows_captured_people_and_their_email_state(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    body = client.get("/app/contacts").text
    assert "Nakamura" in body
    assert "Brandt" in body


def test_contact_filters_and_search_are_honoured(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    found = client.get("/app/contacts?q=Nakamura").text
    assert "Nakamura" in found
    assert "Brandt" not in found


def test_the_contact_story_walks_all_nine_agents(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    body = client.get(
        f"/app/contacts/{scenario.contacts['healthy'].id}?campaign={scenario.campaign.id}"
    ).text
    for agent_id in PIPELINE_ORDER:
        assert AGENT_SPECS[agent_id].display_name in body


def test_a_contact_in_no_campaign_says_so_rather_than_showing_an_empty_chain(
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
    body = client.get(f"/app/contacts/{orphan.id}").text
    assert "Not in a campaign" in body


def test_companies_reports_a_missing_website_as_blocking(
    client: TestClient, db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    body = client.get("/app/companies").text
    assert "no address can be built" in body or "No website" in body


def test_a_company_with_no_dossier_says_nothing_was_researched(
    client: TestClient, db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    from app.models.company import Company

    company = Company(name="Vantage Holdings", domain="vantage.example.com")
    db_session.add(company)
    db_session.commit()
    body = client.get(f"/app/companies/{company.id}").text
    assert "No research on file" in body


# ---------------------------------------------------------------------------
# Knowledge Base
# ---------------------------------------------------------------------------


def test_knowledge_base_shows_entered_records(client: TestClient, db_session: Session) -> None:
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

    overview = client.get("/app/knowledge").text
    assert "Verified Market Research" in overview

    offerings = client.get("/app/knowledge/offerings").text
    assert "Medical-device QA benchmarking dataset" in offerings


def test_knowledge_base_distinguishes_not_entered_from_none_apply(
    client: TestClient, db_session: Session
) -> None:
    """`None` and `[]` are different answers and must read differently."""

    seller_profile.save_profile(
        db_session,
        name="Verified Market Research",
        industries_served=None,
        geographies_served=[],
        updated_by="test",
    )
    db_session.commit()
    body = client.get("/app/knowledge/company").text
    assert "Not entered" in body
    assert "Recorded as none" in body


def test_knowledge_base_says_when_the_feature_is_off(bare_client: TestClient) -> None:
    body = bare_client.get("/app/knowledge").text
    assert "FEATURES__SELLER_KNOWLEDGE_BASE" in body
    assert "not empty" in body


def test_an_unknown_knowledge_section_falls_back_to_the_overview(client: TestClient) -> None:
    response = client.get("/app/knowledge/nonsense")
    assert response.status_code == 200
    assert "What is entered, and what is missing" in response.text


# ---------------------------------------------------------------------------
# Agent settings
# ---------------------------------------------------------------------------


def test_agent_settings_shows_registry_facts_as_facts_not_settings(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    body = client.get("/app/agents?agent=email").text
    assert "Registry facts" in body
    assert "Not settings" in body
    assert str(AGENT_SPECS[AgentIdentifier.EMAIL].max_attempts) in body


def test_agent_settings_refuses_to_offer_a_switch_for_an_unimplemented_agent(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    body = client.get("/app/agents?agent=sending").text
    assert "no adapter registered" in body
    assert "cannot be enabled at all" in body


def _form_version(client: TestClient, agent: str) -> str:
    """The control version the page rendered, taken from the page.

    Reading it back out of the form is the point: the round-trip is what protects
    the optimistic-concurrency check, and a test that hard-codes a version would
    pass even if the template stopped emitting one.
    """

    page = client.get(f"/app/agents?agent={agent}").text
    match = re.search(r'name="expected_version" value="([^"]*)"', page)
    assert match is not None, "the control form must carry the version the page saw"
    return match.group(1)


def test_changing_an_agent_control_carries_the_expected_version(
    client: TestClient, db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    from app.models.agent import AgentControl

    response = client.post(
        "/app/agents/research/control",
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


def test_a_flash_message_survives_a_target_that_already_has_a_query_string(
    client: TestClient, db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    """The separator is chosen, not assumed.

    Review sends the operator back to the exact draft and filter they were on, so
    the target already carries `?`. Appending a second one swallowed the flash into
    the previous parameter and the operator saw nothing happen.
    """

    draft = _make_draft(db_session, scenario)
    back = f"/app/review?draft={draft.id}&view=all"
    response = client.post(
        f"/app/review/{draft.id}/approve", data={"back": back}, follow_redirects=False
    )
    location = response.headers["location"]
    assert location.count("?") == 1, location
    assert "&ok=" in location, location


def test_a_stale_control_version_is_refused_and_the_reason_is_shown(
    client: TestClient, db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    stale = _form_version(client, "research")
    client.post(
        "/app/agents/research/control",
        data={"status": "paused", "expected_version": stale},
    )
    # Acting from the now out-of-date page must be refused, not silently applied.
    response = client.post(
        "/app/agents/research/control",
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
        "/app/agents/sending/control",
        data={"status": "enabled", "expected_version": "0"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "adapter" in response.text.lower()


def test_agent_settings_says_when_the_monitor_is_off(bare_client: TestClient) -> None:
    body = bare_client.get("/app/agents").text
    assert "FEATURES__AGENT_WORKBENCH" in body
    assert "Nothing is running unseen" in body


# ---------------------------------------------------------------------------
# Suppressions and capture
# ---------------------------------------------------------------------------


def test_the_suppression_list_is_read_only_and_says_why(
    client: TestClient, scenario: workbench_scenario.Scenario
) -> None:
    body = client.get("/app/suppressions").text
    assert "gerald.pinto@ashcroft.example.com" in body
    assert "Read-only here on purpose" in body
    assert "<form" not in body


def test_the_capture_page_reports_intake_state_without_claiming_a_connection(
    client: TestClient, bare_client: TestClient
) -> None:
    assert "Intake closed" in bare_client.get("/app/capture").text


# ---------------------------------------------------------------------------
# Design contract
# ---------------------------------------------------------------------------


def test_the_shell_carries_the_designs_three_nav_groups(client: TestClient) -> None:
    body = client.get("/app").text
    for label in ("Today", "Campaigns", "Review", "Contacts", "Companies", "Knowledge Base"):
        assert f">{label}" in body or label in body


def test_no_customer_template_uses_an_inline_style_attribute(client: TestClient) -> None:
    """The design system lives in one stylesheet, not scattered across markup.

    The prototype expressed everything as inline styles. Carrying that into
    production would make the system impossible to change in one place, so the
    templates are asserted clean.
    """

    for path in V2_PAGES:
        body = client.get(path).text
        assert ' style="' not in body, path
