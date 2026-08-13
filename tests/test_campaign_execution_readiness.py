"""Refusing to start a sequence campaign into a terminal auto-skip.

The trap, stated once
---------------------
A disabled *skippable* Agent is not held — it is stepped over, permanently. The
walk in ``orchestrator.schedule_next`` moves the stage to ``SKIPPED``, and
``SKIPPED`` is absorbing: it has an empty outgoing transition set, the scheduler
returns early for it, and the operator re-run path does not list it as stopped.
Enabling the Agent an hour later recovers nothing for any contact that already
went past it.

The walk starts for every contact in the campaign at once, when execution is
switched on. So the whole campaign is spent on a single click, and the three
skippable Agents — Research, Insights and Personalization — are exactly the ones
the seven-message workflow depends on. Skip Personalization and the campaign
produces no messages at all, silently, while reporting every contact complete.

What these tests pin
--------------------
Two failures that look alike and are not. A disabled *skippable* Agent is
**blocking**, because resuming burns it irreversibly. An Agent that is merely
disabled-but-not-skippable, or *paused*, is **holding**: work waits and resumes
when somebody enables it, so nothing is lost and refusing would only obstruct an
operator running a deliberately partial pipeline on purpose.

Everything here is also scoped. A Campaign that never opted in to sequences must
behave exactly as it did before, and a re-affirmation of execution on a Campaign
that is already running must not start failing — by then the walk has happened,
and refusing would block the reconcile rather than protect anything.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence

import pytest
from app.api.deps import get_db
from app.core.config import get_settings
from app.main import create_app
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import AgentControlStatus, AgentIdentifier, CampaignStatus
from app.services import campaign_contacts
from app.services import campaigns as campaign_service
from app.services.agents import controls
from app.services.agents.readiness import execution_readiness
from app.services.agents.registry import AGENT_SPECS
from app.services.personalization.cadence import CADENCE_KEY
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

#: The three Agents the walk steps over rather than waits for. Spelled out
#: rather than derived, so that marking a fourth Agent skippable fails
#: :func:`test_the_skippable_set_is_exactly_the_three_agents_enumerated_here`
#: instead of quietly widening every enumeration below.
SKIPPABLE: tuple[AgentIdentifier, ...] = (
    AgentIdentifier.RESEARCH,
    AgentIdentifier.INSIGHTS,
    AgentIdentifier.PERSONALIZATION,
)


def _client(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__AGENT_WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__EMAIL_SEQUENCES", "true")
    get_settings.cache_clear()
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


@pytest.fixture()
def client(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    with _client(db_session, monkeypatch) as app_client:
        yield app_client
    get_settings.cache_clear()


def _campaign(db: Session, *, opted_in: bool = True, execution: bool = False) -> Campaign:
    campaign = Campaign(
        name=f"Readiness {uuid.uuid4()}",
        description="Execution readiness coverage",
        status=CampaignStatus.DRAFT,
        execution_enabled=execution,
        cadence_config={CADENCE_KEY: {"enabled": True}} if opted_in else None,
    )
    db.add(campaign)
    db.flush()
    return campaign


def _enrol(
    db: Session,
    campaign: Campaign,
    *,
    desired_stage: AgentIdentifier = AgentIdentifier.SENDING,
    count: int = 1,
) -> None:
    """Put ``count`` contacts in the Campaign, aimed at ``desired_stage``.

    Enrolment matters to the check: a Campaign with no contacts has nothing to
    lose, and a stage no contact is enrolled through cannot be skipped.
    """

    for index in range(count):
        token = f"{uuid.uuid4().hex[:10]}"
        contact = Contact(
            first_name=f"Ada{index}",
            last_name="Lovelace",
            title="Head of Operations",
            company_name="Kiln Systems",
            company_domain="kiln.example",
            email=f"ada.{token}@kiln.example",
            natural_key=f"ada{index}|lovelace|{token}|kiln.example",
        )
        db.add(contact)
        db.flush()
        campaign_contacts.enrol_contact(
            db,
            campaign_id=campaign.id,
            contact_id=contact.id,
            source_type="manual",
            enqueue=False,
            desired_stage=desired_stage,
        )


def _set(db: Session, agents: Sequence[AgentIdentifier], status: AgentControlStatus) -> None:
    for agent_id in agents:
        controls.set_global_control(db, agent_id=agent_id, status=status, reason="test setup")


def _names(entries: Sequence[object]) -> set[str]:
    return {entry.display_name for entry in entries}  # type: ignore[attr-defined]


# ===========================================================================
# A. The trap, reproduced
# ===========================================================================


def test_starting_a_sequence_campaign_with_research_off_is_refused_outright(
    db_session: Session,
) -> None:
    """The old behaviour spent the whole campaign on one click, irreversibly.

    Research is disabled by registry default, so this is not an exotic
    misconfiguration — it is the state a freshly created deployment is in. Under
    the old behaviour, switching execution on walked every enrolled contact
    straight past Research into ``SKIPPED``. ``SKIPPED`` is absorbing: no
    outgoing transition, no re-run path, nothing to resume. Enabling Research
    afterwards would recover none of those contacts, which is what makes this
    worth a refusal rather than a warning.

    The refusal also has to leave nothing half-applied, so ``execution_enabled``
    is asserted as well as the exception: the check runs before the column is
    written precisely so a refusal is not a partial start.
    """

    campaign = _campaign(db_session)
    _enrol(db_session, campaign)
    _set(db_session, (AgentIdentifier.RESEARCH,), AgentControlStatus.DISABLED)

    with pytest.raises(campaign_service.CampaignError):
        campaign_service.set_campaign_execution(
            db_session, campaign.id, enabled=True, reconcile=False
        )

    db_session.expire(campaign)
    assert campaign.execution_enabled is False
    assert campaign.status is CampaignStatus.DRAFT


def test_the_refusal_names_the_agents_that_block_and_no_others(
    db_session: Session,
) -> None:
    """An operator can only act on a refusal that says what to switch on.

    Naming a *holding* Agent here would send them to an administrator to enable
    something that was never the obstacle, and the ask would look larger than it
    is. The names come from the registry rather than being spelled into the
    sentence, so the message cannot drift from what the pipeline runs.
    """

    campaign = _campaign(db_session)
    _enrol(db_session, campaign)
    _set(
        db_session,
        (AgentIdentifier.INSIGHTS, AgentIdentifier.PERSONALIZATION),
        AgentControlStatus.ENABLED,
    )

    readiness = execution_readiness(db_session, campaign=campaign)
    message = readiness.refusal_message()

    assert _names(readiness.blocking) == {"Research Agent"}
    assert "Research Agent" in message
    # Email and Verification are disabled too, and hold rather than block.
    assert _names(readiness.holding) >= {"Email Agent", "Verification Agent"}
    for name in ("Email Agent", "Verification Agent", "Insights Agent", "Personalization Agent"):
        assert name not in message, f"{name} is not blocking and must not be named"


# ===========================================================================
# B. Which Agents block, and which only hold
# ===========================================================================


def test_the_skippable_set_is_exactly_the_three_agents_enumerated_here() -> None:
    """Anti-vacuity for the enumeration below.

    The parametrized test is only meaningful while these are the whole skippable
    set. A fourth skippable Agent would be silently uncovered, so it fails here
    instead.
    """

    registered = {spec.identifier for spec in AGENT_SPECS.values() if spec.skippable}
    assert registered == set(SKIPPABLE)
    assert len(SKIPPABLE) == 3


@pytest.mark.parametrize("blocked", SKIPPABLE, ids=lambda agent: agent.value)
def test_any_one_skippable_agent_left_disabled_blocks_the_start(
    db_session: Session, blocked: AgentIdentifier
) -> None:
    """Each of the three is sufficient on its own, because each is terminal.

    Every case asserts the runnable state first and then the block, so a case
    that passed because the Campaign was never startable would fail on the first
    assertion rather than looking like coverage.
    """

    campaign = _campaign(db_session)
    _enrol(db_session, campaign)
    _set(db_session, SKIPPABLE, AgentControlStatus.ENABLED)

    assert execution_readiness(db_session, campaign=campaign).runnable is True

    _set(db_session, (blocked,), AgentControlStatus.DISABLED)
    readiness = execution_readiness(db_session, campaign=campaign)

    assert readiness.runnable is False
    assert _names(readiness.blocking) == {AGENT_SPECS[blocked].display_name}
    with pytest.raises(campaign_service.CampaignError):
        campaign_service.set_campaign_execution(
            db_session, campaign.id, enabled=True, reconcile=False
        )


@pytest.mark.parametrize(
    "waiting", [AgentIdentifier.EMAIL, AgentIdentifier.VERIFICATION], ids=lambda a: a.value
)
def test_a_disabled_non_skippable_agent_holds_work_rather_than_losing_it(
    db_session: Session, waiting: AgentIdentifier
) -> None:
    """Work waiting is recoverable; work skipped is not. That is the whole line.

    An operator deliberately running a partial pipeline — drafting without
    verifying, say — is doing something legitimate, and every contact stops at
    the disabled stage and continues the moment it is enabled. Refusing here
    would obstruct a supported workflow to prevent nothing.
    """

    campaign = _campaign(db_session)
    _enrol(db_session, campaign)
    _set(db_session, SKIPPABLE, AgentControlStatus.ENABLED)
    _set(
        db_session,
        (AgentIdentifier.EMAIL, AgentIdentifier.VERIFICATION),
        AgentControlStatus.ENABLED,
    )
    _set(db_session, (waiting,), AgentControlStatus.DISABLED)

    readiness = execution_readiness(db_session, campaign=campaign)
    assert _names(readiness.holding) == {AGENT_SPECS[waiting].display_name}
    assert readiness.blocking == ()
    assert readiness.runnable is True

    campaign_service.set_campaign_execution(db_session, campaign.id, enabled=True, reconcile=False)
    assert campaign.execution_enabled is True


@pytest.mark.parametrize("paused", SKIPPABLE, ids=lambda agent: agent.value)
def test_a_paused_skippable_agent_holds_rather_than_blocks(
    db_session: Session, paused: AgentIdentifier
) -> None:
    """Paused is a human standing in front of the stage, not the stage vanishing.

    Only a *disabled* skippable stage is stepped over. Paused work waits for
    somebody to resume it, so treating pause as a refusal would make a routine
    hold look like a broken campaign — and would tell the operator to ask an
    administrator for something they had switched on themselves a minute ago.
    """

    campaign = _campaign(db_session)
    _enrol(db_session, campaign)
    _set(db_session, SKIPPABLE, AgentControlStatus.ENABLED)
    _set(db_session, (paused,), AgentControlStatus.PAUSED)

    readiness = execution_readiness(db_session, campaign=campaign)
    assert readiness.blocking == ()
    assert AGENT_SPECS[paused].display_name in _names(readiness.holding)
    assert readiness.runnable is True

    campaign_service.set_campaign_execution(db_session, campaign.id, enabled=True, reconcile=False)
    assert campaign.execution_enabled is True


def test_a_fully_configured_sequence_campaign_starts(db_session: Session) -> None:
    """The positive case. Without it every refusal above could be a blanket one."""

    campaign = _campaign(db_session)
    _enrol(db_session, campaign, count=3)
    _set(db_session, SKIPPABLE, AgentControlStatus.ENABLED)

    assert execution_readiness(db_session, campaign=campaign).runnable is True
    campaign_service.set_campaign_execution(db_session, campaign.id, enabled=True, reconcile=False)

    assert campaign.execution_enabled is True
    assert campaign.status is CampaignStatus.ACTIVE


# ===========================================================================
# C. Scope — what this change must not have touched
# ===========================================================================


def test_a_campaign_that_never_opted_in_still_starts_with_research_disabled(
    db_session: Session,
) -> None:
    """Refusing changes behaviour campaigns have relied on since Phase 2.

    Single-draft campaigns have always been started with Research off, and the
    auto-skip is what spares their operator from skipping a disabled stage by
    hand for every contact. The refusal is therefore scoped to the seven-message
    workflow, which is where the brief asked for it, and this test is what keeps
    it there.
    """

    campaign = _campaign(db_session, opted_in=False)
    _enrol(db_session, campaign)
    _set(db_session, (AgentIdentifier.RESEARCH,), AgentControlStatus.DISABLED)

    campaign_service.set_campaign_execution(db_session, campaign.id, enabled=True, reconcile=False)

    assert campaign.execution_enabled is True


def test_a_campaign_with_no_contacts_starts_because_there_is_nothing_to_lose(
    db_session: Session,
) -> None:
    """The refusal protects enrolled people, and an empty campaign has none.

    Blocking here would stop the ordinary order of work — create the campaign,
    turn it on, then import — for no benefit, since the walk has nobody to walk.
    """

    campaign = _campaign(db_session)
    _set(db_session, (AgentIdentifier.RESEARCH,), AgentControlStatus.DISABLED)

    readiness = execution_readiness(db_session, campaign=campaign)
    assert readiness.blocking == ()
    assert readiness.holding == ()

    campaign_service.set_campaign_execution(db_session, campaign.id, enabled=True, reconcile=False)
    assert campaign.execution_enabled is True


def test_a_stage_no_contact_is_enrolled_through_cannot_block_the_start(
    db_session: Session,
) -> None:
    """A refusal an operator cannot act on and does not need is worse than none.

    A campaign whose contacts are aimed at company resolution never reaches
    Personalization, so Personalization being off costs it nothing. The
    anti-vacuity half matters as much: the *same* controls with contacts aimed
    at the end of the pipeline must still refuse, or this test would be passing
    because the check never fires.
    """

    early = _campaign(db_session)
    _enrol(db_session, early, desired_stage=AgentIdentifier.COMPANY)
    _set(db_session, SKIPPABLE, AgentControlStatus.DISABLED)

    assert execution_readiness(db_session, campaign=early).blocking == ()
    campaign_service.set_campaign_execution(db_session, early.id, enabled=True, reconcile=False)
    assert early.execution_enabled is True

    full = _campaign(db_session)
    _enrol(db_session, full, desired_stage=AgentIdentifier.SENDING)
    assert execution_readiness(db_session, campaign=full).blocking != ()


def test_re_affirming_execution_on_a_running_campaign_never_refuses(
    db_session: Session,
) -> None:
    """The check guards a state change, not a state.

    By the time execution is on, the walk has already happened; refusing then
    would not protect a single contact and would instead block the reconcile
    that projects the master switch onto durable Agent work. Pause and Resume
    are also idempotent by design, and a repeat call must stay so.
    """

    campaign = _campaign(db_session)
    _enrol(db_session, campaign)
    # Enrolled before execution was switched on, which is the only order this
    # state can now be reached in: a *running* sequence campaign whose skippable
    # Agents are off refuses new enrolments outright, because that is where the
    # walk would burn them. The campaign under test here is one that was already
    # running when the Agents were switched off, not one being populated while
    # unsafe -- so the switch is flipped directly rather than by enrolling into
    # an already-running campaign.
    campaign.execution_enabled = True
    db_session.flush()
    _set(db_session, SKIPPABLE, AgentControlStatus.DISABLED)

    assert execution_readiness(db_session, campaign=campaign).runnable is False

    campaign_service.set_campaign_execution(db_session, campaign.id, enabled=True, reconcile=False)
    assert campaign.execution_enabled is True


def test_sending_stays_switched_off_and_is_never_something_to_switch_on(
    db_session: Session,
) -> None:
    """Sending has no adapter, so refusing on it would have no remedy.

    Three claims, because the safety property and the usability property meet
    here: Sending cannot be enabled at all; it is therefore excluded from the
    readiness check rather than reported as a permanent obstacle; and starting a
    campaign does not enable it. The last one is the one that must never
    regress — execution is authority to draft, never authority to send.
    """

    campaign = _campaign(db_session)
    _enrol(db_session, campaign)
    _set(db_session, SKIPPABLE, AgentControlStatus.ENABLED)

    assert AGENT_SPECS[AgentIdentifier.SENDING].implemented is False
    with pytest.raises(controls.AgentControlError):
        controls.set_global_control(
            db_session,
            agent_id=AgentIdentifier.SENDING,
            status=AgentControlStatus.ENABLED,
        )

    readiness = execution_readiness(db_session, campaign=campaign)
    assert "Sending Agent" not in _names(readiness.blocking) | _names(readiness.holding)
    assert readiness.runnable is True

    campaign_service.set_campaign_execution(db_session, campaign.id, enabled=True, reconcile=False)
    db_session.flush()
    effective = controls.effective_control(
        db_session, campaign=campaign, agent_id=AgentIdentifier.SENDING
    )
    assert effective.status is AgentControlStatus.DISABLED


# ===========================================================================
# D. Over the wire
# ===========================================================================


def test_the_campaign_page_warns_before_the_button_is_pressed(
    client: TestClient, db_session: Session
) -> None:
    """Said before the click, not only after the refusal.

    A refusal that only appears once an operator has already decided to start is
    a worse experience than the same sentence on the page they decided from, and
    the page is where they can see the campaign is otherwise ready.
    """

    campaign = _campaign(db_session)
    _enrol(db_session, campaign)
    _set(
        db_session,
        (AgentIdentifier.INSIGHTS, AgentIdentifier.PERSONALIZATION),
        AgentControlStatus.ENABLED,
    )
    db_session.commit()

    response = client.get(f"/app/campaigns/{campaign.id}")
    assert response.status_code == 200
    body = response.text
    assert "This campaign cannot be started yet." in body
    assert "Research Agent" in body
    assert "a skipped stage cannot be re-run afterwards" in body


def test_the_execution_switch_refuses_over_http_and_changes_nothing(
    client: TestClient, db_session: Session
) -> None:
    """The refusal reaches the operator as a flash, and the switch stays off."""

    from urllib.parse import unquote_plus

    campaign = _campaign(db_session)
    _enrol(db_session, campaign)
    _set(db_session, (AgentIdentifier.RESEARCH,), AgentControlStatus.DISABLED)
    db_session.commit()

    response = client.post(
        f"/app/campaigns/{campaign.id}/execution",
        data={"enabled": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    flash = unquote_plus(response.headers["location"])
    assert "err=" in flash
    assert "Research Agent" in flash

    db_session.expire_all()
    refreshed = db_session.get(Campaign, campaign.id)
    assert refreshed is not None
    assert refreshed.execution_enabled is False


def test_the_json_api_answers_409_for_the_same_refusal(
    client: TestClient, db_session: Session
) -> None:
    """The preflight lives in the service, so both callers inherit it.

    Putting it in either route would have left the other able to start a
    campaign into the same terminal skip. Under hosted auth ``/api`` is
    administrator-only; authentication is not enforced in this suite, so the
    route is reachable here and the service-level guarantee is what is being
    checked.
    """

    campaign = _campaign(db_session)
    _enrol(db_session, campaign)
    _set(db_session, (AgentIdentifier.RESEARCH,), AgentControlStatus.DISABLED)
    db_session.commit()

    response = client.post(
        f"/api/campaigns/{campaign.id}/execution",
        json={"enabled": True},
    )
    assert response.status_code == 409
    assert "Research Agent" in response.json()["detail"]

    db_session.expire_all()
    refreshed = db_session.get(Campaign, campaign.id)
    assert refreshed is not None
    assert refreshed.execution_enabled is False
