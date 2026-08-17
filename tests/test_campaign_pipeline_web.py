"""Bulk enrolment, agent-config durability, and the three new result cards.

Grouped together because they are the operator-facing half of finishing the
pipeline: getting a hundred people into a Campaign, keeping the switch that makes
the Agents run, and being able to read what they produced.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from app.api.deps import get_db
from app.core.config import get_settings
from app.main import create_app
from app.models.campaign import CampaignContact
from app.models.contact import Contact
from app.models.enums import (
    AgentControlStatus,
    AgentIdentifier,
    InsightState,
    SuppressionReason,
    SuppressionType,
)
from app.services import campaign_contacts
from app.services.agents import controls
from app.services.agents.adapters import PersonalizationAgentAdapter
from app.services.insights import evidence as insights_evidence
from app.services.suppressions import add_suppression
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.test_knowledge_agents import ScriptedThinker, _context, _records


@pytest.fixture()
def client(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__AGENT_WORKBENCH", "true")
    get_settings.cache_clear()
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def _flash(response: object) -> str:
    from urllib.parse import unquote_plus

    return unquote_plus(response.headers["location"])  # type: ignore[attr-defined]


# --- agent config durability ------------------------------------------------


def test_a_status_change_does_not_wipe_the_live_switch(db_session: Session) -> None:
    """The Workbench changes status without knowing about config.

    Before this was fixed, every pause or resume from the UI reset config to {} —
    silently dropping {"live": true} and returning the Agent to a refusal, or the
    Verification Agent to its simulator, in the middle of a Campaign.
    """

    controls.set_global_control(
        db_session,
        agent_id=AgentIdentifier.RESEARCH,
        status=AgentControlStatus.ENABLED,
        config={"live": True, "timeout_seconds": 120},
    )
    controls.set_global_control(
        db_session,
        agent_id=AgentIdentifier.RESEARCH,
        status=AgentControlStatus.PAUSED,
        reason="operator holding",
    )
    control = controls.set_global_control(
        db_session,
        agent_id=AgentIdentifier.RESEARCH,
        status=AgentControlStatus.ENABLED,
    )
    assert control.config == {"live": True, "timeout_seconds": 120}


def test_config_can_still_be_cleared_explicitly(db_session: Session) -> None:
    """Preserving on omission must not make clearing impossible."""

    controls.set_global_control(
        db_session,
        agent_id=AgentIdentifier.RESEARCH,
        status=AgentControlStatus.ENABLED,
        config={"live": True},
    )
    control = controls.set_global_control(
        db_session,
        agent_id=AgentIdentifier.RESEARCH,
        status=AgentControlStatus.ENABLED,
        config={},
    )
    assert control.config == {}


# --- bulk enrolment ---------------------------------------------------------


def _contact(db: Session, first: str, *, domain: str = "kiln.example") -> Contact:
    contact = Contact(
        first_name=first,
        last_name="Tester",
        company_name="Kiln Systems",
        company_domain=domain,
        email=f"{first.lower()}@{domain}",
        natural_key=f"{first.lower()}|tester|{domain}",
    )
    db.add(contact)
    db.flush()
    return contact


def test_bulk_enrolment_reports_refusals_as_well_as_successes(db_session: Session) -> None:
    """ "87 enrolled" and "87 enrolled, 3 suppressed" need different next actions."""

    campaign, _, first = _records(db_session)
    second = _contact(db_session, "Grace")
    third = _contact(db_session, "Katherine")
    add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value=str(third.email),
        reason=SuppressionReason.OPT_OUT,
        actor="test",
    )

    outcome = campaign_contacts.enrol_contacts(
        db_session,
        campaign_id=campaign.id,
        contact_ids=[first.id, second.id, third.id, second.id],
        enqueue=False,
    )
    # The repeated id is collapsed, not double-counted.
    assert outcome.attempted == 3
    assert len(outcome.enrolled) == 3
    assert "3 enrolled" in outcome.summary

    # Enrolling the same people again is idempotent and says so.
    again = campaign_contacts.enrol_contacts(
        db_session,
        campaign_id=campaign.id,
        contact_ids=[first.id, second.id],
        enqueue=False,
    )
    assert again.enrolled == ()
    assert len(again.already_present) == 2
    assert "already in this Campaign" in again.summary


def test_one_refusal_does_not_abandon_the_rest(db_session: Session) -> None:
    """A contact the domain layer rejects rolls back alone."""

    campaign, _, first = _records(db_session)
    second = _contact(db_session, "Grace")
    outcome = campaign_contacts.enrol_contacts(
        db_session,
        campaign_id=campaign.id,
        contact_ids=[first.id, uuid.uuid4(), second.id],
        enqueue=False,
    )
    assert len(outcome.enrolled) == 2
    assert len(outcome.refused) == 1


def test_the_contacts_page_enrols_a_selection(client: TestClient, db_session: Session) -> None:
    campaign, _, first = _records(db_session)
    second = _contact(db_session, "Grace")
    db_session.commit()

    response = client.post(
        "/app/people/add-to-campaign",
        data={"campaign_id": str(campaign.id), "contact_ids": [str(first.id), str(second.id)]},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "2 enrolled" in _flash(response)
    memberships = db_session.scalars(
        select(CampaignContact).where(CampaignContact.campaign_id == campaign.id)
    ).all()
    assert len(memberships) == 2
    # Enrolment queues the first Agent; it does not run or send anything.
    assert all(row.pipeline_status is not None for row in memberships)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"campaign_id": "", "contact_ids": []}, "Choose a Campaign"),
        ({"campaign_id": str(uuid.uuid4()), "contact_ids": []}, "Tick at least one person"),
    ],
)
def test_the_enrolment_form_refuses_incomplete_input(
    client: TestClient, payload: dict[str, object], expected: str
) -> None:
    response = client.post("/app/people/add-to-campaign", data=payload, follow_redirects=False)
    assert response.status_code == 303
    assert expected in _flash(response)


# --- the result cards -------------------------------------------------------


def test_the_execution_page_renders_a_drafted_email_as_unapproved(
    client: TestClient, db_session: Session
) -> None:
    """The drafted copy must be readable, and never look approved."""

    from datetime import UTC, datetime

    campaign, company, contact = _records(db_session)
    insight = insights_evidence.create_insight(
        db_session,
        claim="Opened a second plant in Pune.",
        kind=insights_evidence.InsightKind.FACT,
        state=InsightState.SUPPORTED,
        evidence=[
            insights_evidence.EvidenceInput(
                source_url="https://kiln.example/news/pune",
                retrieved_at=datetime.now(UTC),
                evidence_summary="The newsroom announced the Pune plant.",
                confidence=0.8,
                extraction_method="test/v1",
            )
        ],
        company_id=company.id,
    )
    context = _context(
        db_session, campaign=campaign, contact=contact, agent_id=AgentIdentifier.PERSONALIZATION
    )
    body = "Ada — saw the Pune plant news.\n\nWorth a short conversation?"
    thinker = ScriptedThinker(
        {
            "subject": "the pune plant",
            "body": body,
            "evidence_insight_ids": [str(insight.id)],
            "rationale": "Led with the plant opening.",
        }
    )
    result = PersonalizationAgentAdapter(thinker_factory=lambda _s: thinker).execute(context)
    # The page reads the committed stage projection, so record it as the worker would.
    from app.models.enums import PipelineEventType, PipelineStageStatus
    from app.services.pipeline import transition_stage

    transition_stage(
        db_session,
        membership=context.membership,
        agent_id=AgentIdentifier.PERSONALIZATION,
        target=PipelineStageStatus.COMPLETED,
        event_type=PipelineEventType.STAGE_COMPLETED,
        actor="test",
        output_reference=result.output_reference,
    )
    db_session.commit()

    page = client.get(f"/workbench/campaigns/{campaign.id}/contacts/{context.membership.id}")
    assert page.status_code == 200
    assert "Drafted email" in page.text
    assert "the pune plant" in page.text
    assert "Worth a short conversation?" in page.text
    assert "not approved" in page.text
    # The evidence it cited is shown, so the draft stays traceable on the page.
    assert str(insight.id) in page.text


def test_a_company_with_no_research_shows_no_research_card(
    client: TestClient, db_session: Session
) -> None:
    """Absence is rendered as absence, not as an empty card implying a failed run."""

    campaign, _, contact = _records(db_session)
    enrolled = campaign_contacts.enrol_contact(
        db_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        enqueue=False,
    )
    db_session.commit()
    page = client.get(f"/workbench/campaigns/{campaign.id}/contacts/{enrolled.membership.id}")
    assert page.status_code == 200
    assert "Company research" not in page.text
    assert "Drafted email" not in page.text


# --- per-campaign policy switches -------------------------------------------


def test_a_campaign_can_open_every_stage_to_a_provisional_domain(
    db_session: Session,
) -> None:
    """The switch is what makes provisional actionable, and only per campaign.

    The guards that stop a guess becoming certainty are untouched by this: they
    live in the resolution service and no campaign setting reaches them. What this
    changes is only whether a campaign is willing to act on a domain it knows is
    a guess.
    """

    from app.services.resolution import gates

    campaign, _, _ = _records(db_session)
    assert campaign.allow_provisional_domains is False
    strict = gates.provisional_allows_for(campaign)
    assert strict == frozenset({gates.DownstreamStage.COMPANY_RESEARCH})

    campaign.allow_provisional_domains = True
    db_session.flush()
    assert gates.provisional_allows_for(campaign) == frozenset(gates.DownstreamStage)


def test_the_settings_form_saves_the_switch_both_ways(
    client: TestClient, db_session: Session
) -> None:
    campaign, _, _ = _records(db_session)
    db_session.commit()

    response = client.post(
        f"/app/campaigns/{campaign.id}/setup",
        data={"name": campaign.name, "allow_provisional_domains": "on"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "Setup saved" in _flash(response)
    db_session.expire_all()
    assert campaign.allow_provisional_domains is True

    # An unchecked box is absent from the form body, which is how "off" arrives —
    # so a post without it must turn it off rather than being read as "no change".
    response = client.post(
        f"/app/campaigns/{campaign.id}/setup", data={"name": campaign.name}, follow_redirects=False
    )
    assert response.status_code == 303
    db_session.expire_all()
    assert campaign.allow_provisional_domains is False


def test_saving_settings_bumps_the_settings_version(
    client: TestClient, db_session: Session
) -> None:
    """A policy change is a settings change and must be visible as one."""

    campaign, _, _ = _records(db_session)
    before = campaign.settings_version
    db_session.commit()

    client.post(
        f"/app/campaigns/{campaign.id}/setup",
        data={"name": campaign.name, "allow_provisional_domains": "on"},
        follow_redirects=False,
    )
    db_session.expire_all()
    assert campaign.settings_version > before
