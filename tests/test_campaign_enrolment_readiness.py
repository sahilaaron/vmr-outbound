"""Enrolling into a campaign that would burn the stage the contact needs.

The trap the start refusal did not cover
----------------------------------------
``readiness.execution_readiness`` answers "would starting this campaign step any
contact permanently past a disabled skippable Agent?", and the answer is derived
from the contacts already enrolled. An empty campaign therefore has nothing to
lose and is correctly allowed to start.

Then the contacts arrive. Every one of them is walked immediately by
``orchestrator.schedule_next``, straight past the disabled Agent into
``SKIPPED`` — an absorbing state with no outgoing transition, no re-run path and
no way back. The campaign that was safe to start an hour ago destroys everything
imported into it, silently, and the check that said so was right at the time.

So the question has to be asked again at the moment it changes: not "is this
campaign safe to start" but "is this campaign safe to *enrol into*". That is one
argument — the stage the incoming contact will be aimed at — and one refusal, at
the one choke point every enrolment surface passes through.

The way around it
-----------------
The start refusal is scoped to campaigns opted in to the seven-message workflow,
and the settings form that carries that opt-in is not administrator-only. Untick,
Resume, tick again, and the campaign is running in sequence mode having never
been checked. That is a write transition, so the write is what is refused.

A campaign that never opted in keeps the behaviour it has always had. The
auto-skip is what spares a single-draft operator from stepping past a disabled
stage by hand for every contact, and taking that away would obstruct a supported
workflow — so nothing here changes for those campaigns.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence
from urllib.parse import unquote_plus

import pytest
from app.api.deps import get_db
from app.core.config import get_settings
from app.main import create_app
from app.models.campaign import Campaign, CampaignContact
from app.models.contact import Contact
from app.models.enums import (
    AgentControlStatus,
    AgentIdentifier,
    CampaignStatus,
    PipelineEventType,
    PipelineStageStatus,
)
from app.models.pipeline import CampaignContactAgentState
from app.services import campaign_contacts
from app.services.agents import controls
from app.services.personalization.cadence import CADENCE_KEY, campaign_opted_in
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

#: The three Agents the walk steps over rather than waits for.
SKIPPABLE: tuple[AgentIdentifier, ...] = (
    AgentIdentifier.RESEARCH,
    AgentIdentifier.INSIGHTS,
    AgentIdentifier.PERSONALIZATION,
)


@pytest.fixture()
def client(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__AGENT_WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__EMAIL_SEQUENCES", "true")
    get_settings.cache_clear()
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def _campaign(db: Session, *, opted_in: bool = True, execution: bool = False) -> Campaign:
    campaign = Campaign(
        name=f"Enrolment readiness {uuid.uuid4()}",
        description="Enrolment readiness coverage",
        status=CampaignStatus.ACTIVE if execution else CampaignStatus.DRAFT,
        execution_enabled=execution,
        cadence_config={CADENCE_KEY: {"enabled": True}} if opted_in else None,
    )
    db.add(campaign)
    db.flush()
    return campaign


def _contact(db: Session, label: str) -> Contact:
    token = uuid.uuid4().hex[:10]
    contact = Contact(
        first_name=label,
        last_name="Lovelace",
        title="Head of Operations",
        company_name="Kiln Systems",
        company_domain="kiln.example",
        email=f"{label.lower()}.{token}@kiln.example",
        natural_key=f"{label.lower()}|lovelace|{token}|kiln.example",
    )
    db.add(contact)
    db.flush()
    return contact


def _set(db: Session, agents: Sequence[AgentIdentifier], status: AgentControlStatus) -> None:
    for agent_id in agents:
        controls.set_global_control(db, agent_id=agent_id, status=status, reason="test setup")


def _flash(response: object) -> str:
    return unquote_plus(response.headers["location"])  # type: ignore[attr-defined]


def _skipped_stages(db: Session, campaign: Campaign) -> list[CampaignContactAgentState]:
    """Every stage in this Campaign that was terminally stepped over.

    Asserted on rather than on the HTTP response, because a refusal that returns
    a tidy message while the walk still happened underneath it would pass every
    response-shaped assertion and fix nothing.
    """

    return list(
        db.scalars(
            select(CampaignContactAgentState)
            .join(
                CampaignContact,
                CampaignContact.id == CampaignContactAgentState.campaign_contact_id,
            )
            .where(
                CampaignContact.campaign_id == campaign.id,
                CampaignContactAgentState.status == PipelineStageStatus.SKIPPED,
            )
        ).all()
    )


def _walk_to_research(db: Session, campaign: Campaign) -> None:
    """Advance every membership in this Campaign to the Research Agent.

    Enrolment does not reach Research by itself — ``initialize_pipeline`` starts
    a contact at Identity, and Research is three stages further on. The terminal
    skip therefore happens minutes later, when the earlier Agents have finished
    and the walk arrives at a stage that is switched off. Asserting on stage
    state immediately after enrolment would pass on the unrepaired code and prove
    nothing at all.

    So the earlier stages are completed and the walk is asked to take its next
    step, which is exactly the moment the damage is done. On the unrepaired code
    a membership exists here and is stepped past Research into ``SKIPPED``; when
    the enrolment was refused there is no membership for this to walk.
    """

    from app.services.agents.orchestrator import schedule_next
    from app.services.pipeline import transition_stage

    for membership in _memberships(db, campaign):
        for done in (AgentIdentifier.IDENTITY, AgentIdentifier.COMPANY):
            transition_stage(
                db,
                membership=membership,
                agent_id=done,
                target=PipelineStageStatus.COMPLETED,
                event_type=PipelineEventType.STAGE_COMPLETED,
                actor="test",
                reason_code="test_walk",
                reason_detail="Completed so the walk can reach Research.",
            )
        schedule_next(db, membership=membership, actor="test")
    db.flush()


def _memberships(db: Session, campaign: Campaign) -> list[CampaignContact]:
    return list(
        db.scalars(select(CampaignContact).where(CampaignContact.campaign_id == campaign.id)).all()
    )


# ===========================================================================
# B-1. Started empty, populated later
# ===========================================================================


def test_enrolling_into_a_started_sequence_campaign_burns_nothing(
    client: TestClient, db_session: Session
) -> None:
    """The reviewer's exact route, driven through the surfaces it used.

    Create an empty campaign, opt it in, press Resume — all three accepted,
    because an empty campaign genuinely has nothing to lose — and then enrol.
    Under the old behaviour that last step was where the campaign was spent:
    every contact walked past the disabled Research Agent into ``SKIPPED`` and no
    later action recovered any of them.

    The pipeline state is what is asserted. A refusal that merely returned a
    message while the walk still happened would be no repair at all.
    """

    campaign = _campaign(db_session)
    _set(
        db_session,
        (AgentIdentifier.INSIGHTS, AgentIdentifier.PERSONALIZATION),
        AgentControlStatus.ENABLED,
    )
    _set(db_session, (AgentIdentifier.RESEARCH,), AgentControlStatus.DISABLED)
    contact = _contact(db_session, "Ada")
    db_session.commit()

    started = client.post(
        f"/app/campaigns/{campaign.id}/lifecycle",
        data={"action": "start"},
        follow_redirects=False,
    )
    assert started.status_code == 303
    assert "err=" not in _flash(started), "an empty campaign must still be startable"
    db_session.expire_all()

    response = client.post(
        "/app/people/add-to-campaign",
        data={"campaign_id": str(campaign.id), "contact_ids": [str(contact.id)]},
        follow_redirects=False,
    )
    assert response.status_code == 303
    db_session.expire_all()

    # Pipeline state first, deliberately. This is the assertion that fails on the
    # unrepaired code, and it fails by naming the damage rather than by noticing
    # that a sentence was worded differently.
    _walk_to_research(db_session, campaign)
    assert _skipped_stages(db_session, campaign) == []
    assert _memberships(db_session, campaign) == []
    assert "Research Agent" in _flash(response)


def test_the_permanent_contact_survives_a_refused_enrolment(
    client: TestClient, db_session: Session
) -> None:
    """Refusing the enrolment is only acceptable because it loses nothing.

    A contact is permanent and never required a campaign to exist. If the refusal
    took the person with it, refusing would be the more destructive of the two
    options rather than the safe one, and holding the membership unqueued would
    have been the right answer instead.
    """

    campaign = _campaign(db_session)
    _set(db_session, (AgentIdentifier.RESEARCH,), AgentControlStatus.DISABLED)
    contact = _contact(db_session, "Grace")
    db_session.commit()

    client.post(
        f"/app/campaigns/{campaign.id}/lifecycle",
        data={"action": "start"},
        follow_redirects=False,
    )
    client.post(
        "/app/people/add-to-campaign",
        data={"campaign_id": str(campaign.id), "contact_ids": [str(contact.id)]},
        follow_redirects=False,
    )
    db_session.expire_all()

    assert db_session.get(Contact, contact.id) is not None


def test_a_file_import_into_the_same_campaign_is_refused_whole(
    client: TestClient, db_session: Session
) -> None:
    """The import surface refuses before its first durable write.

    ``campaign_import.confirm`` writes a batch and then commits each row on its
    own SAVEPOINT, catching only database errors — a refusal raised from enrolment
    half-way down the file would escape as a 500 with a batch written and some
    rows already through. Refusing before any of it starts is what makes the
    answer whole, and what keeps the staged file available to import once the
    Agent is enabled.
    """

    campaign = _campaign(db_session, execution=True)
    _set(db_session, (AgentIdentifier.RESEARCH,), AgentControlStatus.DISABLED)
    db_session.commit()

    response = client.post(
        f"/app/campaigns/{campaign.id}/imports/staged/{uuid.uuid4()}/confirm",
        data={},
        follow_redirects=False,
    )
    assert response.status_code == 303
    # The staged upload does not exist, so the readiness refusal must not be the
    # reason -- ownership is checked first. This asserts the ordering, not the
    # refusal: the refusal itself is asserted through the service below, which is
    # the same guard the confirm route re-asks.
    assert "not available for this campaign" in _flash(response)

    with pytest.raises(campaign_contacts.CampaignContactError) as refusal:
        campaign_contacts.enrol_contact(
            db_session,
            campaign_id=campaign.id,
            contact_id=_contact(db_session, "Katherine").id,
            source_type="import",
        )
    assert "Research Agent" in str(refusal.value)


def test_a_fully_configured_running_campaign_still_accepts_contacts(
    client: TestClient, db_session: Session
) -> None:
    """The positive case. Without it the refusal above could be a blanket one."""

    campaign = _campaign(db_session)
    _set(db_session, SKIPPABLE, AgentControlStatus.ENABLED)
    contact = _contact(db_session, "Marie")
    db_session.commit()

    client.post(
        f"/app/campaigns/{campaign.id}/lifecycle",
        data={"action": "start"},
        follow_redirects=False,
    )
    response = client.post(
        "/app/people/add-to-campaign",
        data={"campaign_id": str(campaign.id), "contact_ids": [str(contact.id)]},
        follow_redirects=False,
    )
    assert "1 enrolled" in _flash(response)
    db_session.expire_all()
    assert len(_memberships(db_session, campaign)) == 1


# ===========================================================================
# B-2. Untick, start, re-tick
# ===========================================================================


def test_the_three_click_opt_out_and_back_in_cannot_reach_a_running_sequence(
    client: TestClient, db_session: Session
) -> None:
    """Driven through the real form, because the bypass is a property of the form.

    The start refusal only looks at campaigns that are opted in *at the moment
    Resume is pressed*, and the settings form is deliberately not
    administrator-only. So a blocked operator had a three-click route around it:
    untick the sequence, Resume, tick it again. Nothing refused, and the whole
    cohort was already spent.

    Every click is made here rather than calling the services, because a
    service-level test would prove only that each step is individually guarded —
    and each step individually always was.
    """

    campaign = _campaign(db_session)
    _set(db_session, (AgentIdentifier.RESEARCH,), AgentControlStatus.DISABLED)
    contact = _contact(db_session, "Rosalind")
    campaign_contacts.enrol_contact(
        db_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        enqueue=False,
    )
    version_before = campaign.settings_version
    db_session.commit()

    # Click one: untick the sequence.
    client.post(
        f"/app/campaigns/{campaign.id}/setup",
        data={"name": campaign.name},
        follow_redirects=False,
    )
    db_session.expire_all()
    assert campaign_opted_in(campaign) is False

    # Click two: Resume. The campaign is no longer opted in, so the start refusal
    # does not apply to it and it starts — exactly as a campaign that never opted
    # in has always started. This step is deliberately left alone: what closes the
    # bypass is refusing the way back in, not blocking a start that non-sequence
    # campaigns are entitled to.
    client.post(
        f"/app/campaigns/{campaign.id}/lifecycle",
        data={"action": "start"},
        follow_redirects=False,
    )
    db_session.expire_all()
    assert campaign.execution_enabled is True

    # Click three: tick the sequence again. This is the one that has to refuse.
    version_running = campaign.settings_version
    reticked = client.post(
        f"/app/campaigns/{campaign.id}/setup",
        data={"name": campaign.name, "sequence_enabled": "on"},
        follow_redirects=False,
    )
    assert "Research Agent" in _flash(reticked)
    db_session.expire_all()

    assert campaign_opted_in(campaign) is False
    assert campaign.execution_enabled is True
    assert campaign.settings_version == version_running >= version_before


def test_a_refused_opt_in_persists_no_part_of_the_save(
    client: TestClient, db_session: Session
) -> None:
    """``update_campaign`` writes every changed field in one SAVEPOINT.

    The refusal therefore has to happen above it. Raised below, it would either
    leave the cadence change applied or take the rename down with it, and either
    way the settings version would have moved to record a decision that was not
    made.
    """

    campaign = _campaign(db_session, opted_in=False, execution=True)
    _set(db_session, (AgentIdentifier.RESEARCH,), AgentControlStatus.DISABLED)
    contact = _contact(db_session, "Dorothy")
    campaign_contacts.enrol_contact(
        db_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        enqueue=False,
    )
    original_name = campaign.name
    original_version = campaign.settings_version
    db_session.commit()

    response = client.post(
        f"/app/campaigns/{campaign.id}/setup",
        data={"name": "Renamed alongside the switch", "sequence_enabled": "on"},
        follow_redirects=False,
    )
    assert "Research Agent" in _flash(response)
    db_session.expire_all()

    assert campaign.name == original_name, "the rename rode in on the refused write"
    assert campaign_opted_in(campaign) is False
    assert campaign.settings_version == original_version


def test_a_draft_campaign_with_nobody_in_it_may_still_be_opted_in(
    client: TestClient, db_session: Session
) -> None:
    """Configure first, populate second, is the ordinary order of work.

    An empty draft campaign has nothing to lose, so refusing here would obstruct
    the normal setup path to prevent nothing — the enrolment refusal is what
    protects the contacts, and it is asked at the moment they arrive.
    """

    campaign = _campaign(db_session, opted_in=False)
    _set(db_session, (AgentIdentifier.RESEARCH,), AgentControlStatus.DISABLED)
    db_session.commit()

    client.post(
        f"/app/campaigns/{campaign.id}/setup",
        data={"name": campaign.name, "sequence_enabled": "on"},
        follow_redirects=False,
    )
    db_session.expire_all()
    assert campaign_opted_in(campaign) is True
