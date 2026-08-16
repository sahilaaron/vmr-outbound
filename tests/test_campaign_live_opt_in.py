"""The Campaign live opt-in: the switch that had no operator path.

Four Agents refuse every job until the Campaign's *effective* Agent
configuration carries ``{"live": true}`` — Research, Verification, Insights and
Personalization. Nothing in the product could set it. A Campaign therefore
showed every Agent enabled, execution running and no override at all, while
every Research job it claimed came straight back as ``research_not_live``; 18
Campaign Contacts sat at that stage on the live deployment with no supported way
forward.

This file is about the repair, and it is deliberately narrow. It does not
re-test the control precedence, the re-run, or the campaign-access boundary —
each has its own file. What it pins is the five things the repair had to be true
about:

* the opt-in is **configuration, not status**, so it can neither turn an Agent
  on nor be wiped by turning one on;
* it is **one Campaign's decision**, and reaches no other Campaign;
* it is **versioned**, so a stale page cannot silently overwrite a newer answer;
* **turning it off destroys nothing** — no job, no evidence, no stage history;
* **it releases nothing by itself.** Work already refused stays refused until an
  operator runs the stage again, and that re-run touches the stage they aimed at
  and no other.
"""

from __future__ import annotations

import inspect
import time
import uuid
from collections.abc import Iterator

import pytest
from app.core.auth.session import SESSION_COOKIE_NAME, SessionCodec
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.main import create_app
from app.models.agent import CampaignAgentOverride
from app.models.audit_event import AuditEvent
from app.models.enums import (
    AgentControlStatus,
    AgentIdentifier,
    AgentJobStatus,
    PipelineEventType,
    PipelineStageStatus,
)
from app.models.pipeline import CampaignContactAgentState
from app.models.verification_job import AgentJob
from app.services import campaigns as campaign_service
from app.services import pipeline, workbench_agents
from app.services.agents import controls
from app.services.agents import jobs as agent_jobs
from app.services.agents import rerun as agent_rerun
from app.services.agents.adapters import DEFAULT_ADAPTERS
from app.services.agents.orchestrator import stage_job_key
from app.services.agents.registry import AGENT_SPECS, LIVE_OPT_IN_AGENTS, PIPELINE_ORDER
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests import workbench_scenario
from tests.hosted_auth_factory import TEST_CLIENT_ID, seed_account

RESEARCH = AgentIdentifier.RESEARCH


@pytest.fixture()
def scenario(db_session: Session) -> workbench_scenario.Scenario:
    built = workbench_scenario.build(db_session)
    db_session.commit()
    return built


def _enable_globally(db: Session, agent_id: AgentIdentifier = RESEARCH) -> None:
    """The state the live deployment was in: Agent on, nothing opted in.

    Config is left empty on purpose. That is the whole starting condition — an
    enabled Agent whose every execution refuses.
    """

    controls.set_global_control(db, agent_id=agent_id, status=AgentControlStatus.ENABLED, config={})
    db.flush()


def _block_stage(
    db: Session,
    scenario: workbench_scenario.Scenario,
    key: str,
    *,
    agent_id: AgentIdentifier = RESEARCH,
    reason_code: str = "research_not_live",
    reason: str = "This campaign has not enabled live company research.",
) -> AgentJob:
    """Leave one contact exactly where ``AgentBlocked`` leaves them.

    The orchestrator's own handling of a blocked execution, reproduced through
    the same two services it uses: the job is *paused* carrying the refusal's
    code, and the stage is BLOCKED. Nothing is assigned to a column directly, so
    what the assertions read is the shape production stores.
    """

    membership = scenario.membership(key)
    for upstream in PIPELINE_ORDER:
        if upstream is agent_id:
            break
        state = pipeline.agent_state(
            db, campaign_contact_id=membership.id, agent_id=upstream, create=True
        )
        assert state is not None
        if state.status is not PipelineStageStatus.COMPLETED:
            pipeline.transition_stage(
                db,
                membership=membership,
                agent_id=upstream,
                target=PipelineStageStatus.COMPLETED,
                event_type=PipelineEventType.STAGE_COMPLETED,
                actor="test-setup",
                reason_code="test_setup",
            )

    existing = db.scalars(
        select(AgentJob.id).where(
            AgentJob.campaign_contact_id == membership.id,
            AgentJob.agent_id == agent_id,
        )
    ).all()
    job, _ = agent_jobs.enqueue_job(
        db,
        agent_id=agent_id,
        idempotency_key=stage_job_key(membership.id, agent_id, generation=len(existing) + 1),
        task_kind="advance_campaign_contact",
        max_attempts=AGENT_SPECS[agent_id].max_attempts,
        campaign_id=membership.campaign_id,
        campaign_contact_id=membership.id,
        contact_id=membership.contact_id,
        entity_type="campaign_contact",
        entity_id=membership.id,
    )
    agent_jobs.mark_paused(db, job, reason=reason, reason_code=reason_code)
    pipeline.transition_stage(
        db,
        membership=membership,
        agent_id=agent_id,
        target=PipelineStageStatus.BLOCKED,
        event_type=PipelineEventType.ELIGIBILITY_BLOCKED,
        actor="test-setup",
        job=job,
        reason_code=reason_code,
        reason_detail=reason,
    )
    membership.current_stage = agent_id
    membership.next_stage = agent_id
    membership.pipeline_status = PipelineStageStatus.BLOCKED
    db.flush()
    return job


# ---------------------------------------------------------------------------
# A. The contract: which Agents actually have this gate
# ---------------------------------------------------------------------------


def test_the_registry_names_exactly_the_adapters_that_refuse_without_the_opt_in() -> None:
    """The drift guard, and the reason the flag is a registry fact.

    A screen cannot show an operator a switch it has to know about by hand. The
    flag is read off the specs by every surface in this repair, so it must be
    the adapters' own answer rather than a second list that agrees with them
    today. Adding the gate to a fifth adapter fails here until the registry
    records it — and removing one fails here too, which is the half a hand-written
    list never catches.
    """

    with_gate = {
        agent_id
        for agent_id, adapter in DEFAULT_ADAPTERS.items()
        if 'config.get("live")' in inspect.getsource(type(adapter))
        or "_live_or_blocked(" in inspect.getsource(type(adapter))
    }
    assert with_gate, "no adapter reads the live gate — the source check is broken, not the code"
    assert with_gate == set(LIVE_OPT_IN_AGENTS)
    assert set(LIVE_OPT_IN_AGENTS) == {
        AgentIdentifier.RESEARCH,
        AgentIdentifier.VERIFICATION,
        AgentIdentifier.INSIGHTS,
        AgentIdentifier.PERSONALIZATION,
    }


def test_an_agent_without_the_gate_cannot_be_given_an_opt_in(
    db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    """Refused rather than written. A key no adapter reads is not a control."""

    with pytest.raises(controls.AgentControlError):
        controls.set_campaign_live_opt_in(
            db_session,
            campaign_id=scenario.campaign.id,
            agent_id=AgentIdentifier.COMPANY,
            live=True,
        )


# ---------------------------------------------------------------------------
# B. The service: one Campaign's configuration, and nothing else
# ---------------------------------------------------------------------------


def test_opting_in_puts_live_true_in_this_campaigns_effective_config(
    db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    """The UAT expectation, stated as the adapter reads it."""

    _enable_globally(db_session)
    assert not controls.campaign_live_opt_in(
        db_session, campaign=scenario.campaign, agent_id=RESEARCH
    )

    controls.set_campaign_live_opt_in(
        db_session,
        campaign_id=scenario.campaign.id,
        agent_id=RESEARCH,
        live=True,
        reason="pilot cohort approved",
    )

    effective = controls.effective_control(
        db_session, campaign=scenario.campaign, agent_id=RESEARCH
    )
    assert effective.config["live"] is True
    assert effective.status is AgentControlStatus.ENABLED


def test_the_opt_in_reaches_no_other_campaign(
    db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    _enable_globally(db_session)
    controls.set_campaign_live_opt_in(
        db_session, campaign_id=scenario.campaign.id, agent_id=RESEARCH, live=True
    )

    other = controls.effective_control(
        db_session, campaign=scenario.other_campaign, agent_id=RESEARCH
    )
    assert other.config.get("live") is not True
    assert not controls.campaign_live_opt_in(
        db_session, campaign=scenario.other_campaign, agent_id=RESEARCH
    )


def test_the_opt_in_carries_the_status_it_found_rather_than_choosing_one(
    db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    """Configuration must not be a way to switch an Agent on.

    The global control says paused. Opting in records the Campaign's permission
    and leaves the Agent exactly as paused as it was, so nothing claims work
    because somebody answered a question about spending.
    """

    controls.set_global_control(
        db_session, agent_id=RESEARCH, status=AgentControlStatus.PAUSED, config={}
    )
    db_session.flush()

    controls.set_campaign_live_opt_in(
        db_session, campaign_id=scenario.campaign.id, agent_id=RESEARCH, live=True
    )

    effective = controls.effective_control(
        db_session, campaign=scenario.campaign, agent_id=RESEARCH
    )
    assert effective.status is AgentControlStatus.PAUSED
    assert effective.config["live"] is True


def test_the_opt_in_survives_an_ordinary_status_change(
    db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    """Pausing and resuming an Agent must not quietly withdraw the permission.

    This is the failure the ``_KEEP_CONFIG`` sentinel exists for, asserted from
    the operator's side: an opt-in given once stays given until somebody
    withdraws it deliberately.
    """

    _enable_globally(db_session)
    controls.set_campaign_live_opt_in(
        db_session, campaign_id=scenario.campaign.id, agent_id=RESEARCH, live=True
    )

    commands = workbench_agents.WorkbenchCommands(db_session)
    version = _campaign_version(db_session, scenario.campaign.id, RESEARCH)
    paused = commands.set_campaign_override(
        scenario.campaign.id,
        RESEARCH,
        AgentControlStatus.PAUSED,
        expected_version=version,
        reason="holding the cohort",
    )
    assert paused.accepted
    resumed = commands.set_campaign_override(
        scenario.campaign.id,
        RESEARCH,
        AgentControlStatus.ENABLED,
        expected_version=_campaign_version(db_session, scenario.campaign.id, RESEARCH),
        reason="released",
    )
    assert resumed.accepted

    effective = controls.effective_control(
        db_session, campaign=scenario.campaign, agent_id=RESEARCH
    )
    assert effective.status is AgentControlStatus.ENABLED
    assert effective.config["live"] is True


def test_withdrawing_the_opt_in_keeps_the_rest_of_the_configuration(
    db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    """Research's worker settings are not collateral in a spending decision."""

    _enable_globally(db_session)
    controls.set_campaign_override(
        db_session,
        campaign_id=scenario.campaign.id,
        agent_id=RESEARCH,
        status=AgentControlStatus.ENABLED,
        config={"workers": ["website"], "claude_fallback": False},
    )
    db_session.flush()

    controls.set_campaign_live_opt_in(
        db_session, campaign_id=scenario.campaign.id, agent_id=RESEARCH, live=True
    )
    controls.set_campaign_live_opt_in(
        db_session, campaign_id=scenario.campaign.id, agent_id=RESEARCH, live=False
    )

    effective = controls.effective_control(
        db_session, campaign=scenario.campaign, agent_id=RESEARCH
    )
    # `false`, not absent: a removed key would re-inherit a global `live` if one
    # is ever set, which is the opposite of what withdrawing it means.
    assert effective.config["live"] is False
    assert effective.config["workers"] == ["website"]
    assert effective.config["claude_fallback"] is False


def test_withdrawing_the_opt_in_discards_no_work_and_no_evidence(
    db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    """Turning a permission off is not a way to lose what was already done."""

    _enable_globally(db_session)
    job = _block_stage(db_session, scenario, "healthy")
    membership = scenario.membership("healthy")
    events_before = _event_count(db_session, membership.id)

    controls.set_campaign_live_opt_in(
        db_session, campaign_id=scenario.campaign.id, agent_id=RESEARCH, live=True
    )
    controls.set_campaign_live_opt_in(
        db_session, campaign_id=scenario.campaign.id, agent_id=RESEARCH, live=False
    )

    db_session.refresh(job)
    assert job.status is AgentJobStatus.PAUSED
    state = pipeline.agent_state(
        db_session, campaign_contact_id=membership.id, agent_id=RESEARCH, create=False
    )
    assert state is not None
    assert state.status is PipelineStageStatus.BLOCKED
    assert state.reason_code == "research_not_live"
    assert _event_count(db_session, membership.id) == events_before


# ---------------------------------------------------------------------------
# C. The command: versioning, refusals, audit
# ---------------------------------------------------------------------------


def test_a_stale_page_cannot_overwrite_a_newer_decision(
    db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    """Two operators, one Campaign. The second one to press must be told.

    The page here was rendered before any override existed, so it carries no
    version at all — which is itself a claim about the world, and a false one by
    the time it is submitted.
    """

    _enable_globally(db_session)
    commands = workbench_agents.WorkbenchCommands(db_session)
    first = commands.set_campaign_live_opt_in(
        scenario.campaign.id, RESEARCH, live=True, expected_version=None
    )
    assert first.accepted

    stale = commands.set_campaign_live_opt_in(
        scenario.campaign.id, RESEARCH, live=False, expected_version=None
    )

    assert not stale.accepted
    assert "changed while the page was open" in stale.message
    assert controls.campaign_live_opt_in(
        db_session, campaign=scenario.campaign, agent_id=RESEARCH
    ), "the refused write must not have applied"


def test_the_decision_is_recorded_with_the_campaign_and_the_agent(
    db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    """An opt-in is a spending decision, so who made it has to survive it."""

    _enable_globally(db_session)
    outcome = workbench_agents.WorkbenchCommands(db_session).set_campaign_live_opt_in(
        scenario.campaign.id,
        RESEARCH,
        live=True,
        expected_version=None,
        reason="pilot cohort approved",
    )
    assert outcome.accepted
    db_session.flush()

    recorded = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == "workbench.agent.campaign_live_opt_in.on")
    ).all()
    assert len(recorded) == 1
    context = recorded[0].context or {}
    assert context["campaign_id"] == str(scenario.campaign.id)
    assert context["agent_id"] == RESEARCH.value
    assert context["live"] is True


def test_a_campaign_that_does_not_exist_is_refused_before_anything_is_written(
    db_session: Session,
) -> None:
    commands = workbench_agents.WorkbenchCommands(db_session)
    with pytest.raises(workbench_agents.WorkbenchCommandError):
        commands.set_campaign_live_opt_in(uuid.uuid4(), RESEARCH, live=True, expected_version=None)
    assert db_session.scalars(select(CampaignAgentOverride)).all() == []


# ---------------------------------------------------------------------------
# D. Recovery: what the opt-in does *not* do
# ---------------------------------------------------------------------------


def test_opting_in_releases_nothing_on_its_own(
    db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    """The permission changes what a run may do, never what is already queued.

    Sweeping held work back into the queue on a configuration change would spend
    money nobody asked to spend at that moment. The operator releases it, through
    the re-run the stage already offers.
    """

    _enable_globally(db_session)
    job = _block_stage(db_session, scenario, "healthy")

    outcome = workbench_agents.WorkbenchCommands(db_session).set_campaign_live_opt_in(
        scenario.campaign.id, RESEARCH, live=True, expected_version=None
    )
    assert outcome.accepted
    assert outcome.reconciled_jobs == 0

    db_session.refresh(job)
    assert job.status is AgentJobStatus.PAUSED


def test_the_rerun_after_opting_in_touches_the_stage_it_was_aimed_at(
    db_session: Session, scenario: workbench_scenario.Scenario
) -> None:
    """The supported recovery path, and its boundary.

    One contact is held at Research by ``research_not_live``; another is held at
    Research for a reason the opt-in has nothing to do with. Re-running Research
    after the opt-in is the operator's deliberate act and takes both — they are
    the stage they aimed at, and each is named on the page before the press.
    What it must not do is reach a *different* Agent's held work, which is what
    an automatic release keyed on "everything paused" would have done.
    """

    _enable_globally(db_session)
    blocked = _block_stage(db_session, scenario, "healthy")
    unrelated = _block_stage(
        db_session,
        scenario,
        "leased",
        agent_id=AgentIdentifier.INSIGHTS,
        reason_code="thinking_live_disabled",
        reason="Live execution is not enabled for the Insights Agent on this Campaign.",
    )

    controls.set_campaign_live_opt_in(
        db_session, campaign_id=scenario.campaign.id, agent_id=RESEARCH, live=True
    )
    outcome = agent_rerun.rerun_stage(
        db_session,
        campaign_id=scenario.campaign.id,
        agent_id=RESEARCH,
        reason="live research enabled for this campaign",
    )

    assert outcome.requeued == (scenario.membership("healthy").id,)
    db_session.refresh(blocked)
    assert blocked.status is AgentJobStatus.CANCELLED
    research_state = pipeline.agent_state(
        db_session,
        campaign_contact_id=scenario.membership("healthy").id,
        agent_id=RESEARCH,
        create=False,
    )
    assert research_state is not None
    assert research_state.status is PipelineStageStatus.WAITING

    # The other Agent's held contact was not touched by any of it.
    db_session.refresh(unrelated)
    assert unrelated.status is AgentJobStatus.PAUSED
    insights_state = pipeline.agent_state(
        db_session,
        campaign_contact_id=scenario.membership("leased").id,
        agent_id=AgentIdentifier.INSIGHTS,
        create=False,
    )
    assert insights_state is not None
    assert insights_state.status is PipelineStageStatus.BLOCKED
    assert insights_state.reason_code == "thinking_live_disabled"


# ---------------------------------------------------------------------------
# E. Over HTTP: who may press it, and for which Campaign
# ---------------------------------------------------------------------------

HOST = "srv1885453.hstgr.cloud"
ORIGIN = f"https://{HOST}"
SESSION_SECRET = "test-session-secret-value-at-least-32-chars"
ADMIN_EMAIL = "sahil@verifiedmarketresearch.com"
STAGING_DATABASE_URL = "postgresql+psycopg://vmr:secret@db.internal.example:5432/vmr_staging"


class _AlwaysReadyProbe:
    def __call__(self) -> None:
        return None


@pytest.fixture()
def hosted(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The hosted application with accounts on, built as staging builds it."""

    env = {
        "APP_ENV": "staging",
        "DEBUG": "false",
        "DRY_RUN": "true",
        "TRUSTED_HOSTS": f'["{HOST}"]',
        "DATABASE_URL": STAGING_DATABASE_URL,
        "FEATURES__WORKBENCH": "true",
        "FEATURES__AGENT_WORKBENCH": "true",
        "AUTH__ENABLED": "true",
        "AUTH__SESSION_SECRET": SESSION_SECRET,
        "AUTH__ALLOWED_OPERATOR_EMAILS": "[]",
        "AUTH__BOOTSTRAP_ADMIN_EMAIL": ADMIN_EMAIL,
        "AUTH__GOOGLE_CLIENT_ID": TEST_CLIENT_ID,
        "AUTH__GOOGLE_CLIENT_SECRET": "test-client-secret",
        "AUTH__PUBLIC_BASE_URL": ORIGIN,
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    app = create_app(readiness_probe=_AlwaysReadyProbe())
    try:
        yield TestClient(
            app, base_url=ORIGIN, follow_redirects=False, raise_server_exceptions=False
        )
    finally:
        get_settings.cache_clear()


def _sign_in(client: TestClient, *, role: str, email: str) -> tuple[str, str]:
    """A real account row and a real session cookie. Returns (user_id, csrf)."""

    from app.core.auth.session import OperatorSession, new_session_id

    account = seed_account(email=email, role=role)
    now = int(time.time())
    session_id = new_session_id()
    codec = SessionCodec(SESSION_SECRET)
    client.cookies.set(
        SESSION_COOKIE_NAME,
        codec.encode_session(
            OperatorSession(
                email=account.email,
                subject="",
                display_name="",
                session_id=session_id,
                issued_at=now,
                expires_at=now + 3600,
                user_id=account.user_id,
                auth_version=1,
            )
        ),
    )
    return account.user_id, codec.csrf_token(session_id)


def _committed_campaign(name: str, *, owner_id: str | None = None) -> uuid.UUID:
    """One campaign the application's own session can see, plus Research enabled.

    Committed through ``SessionLocal`` rather than the rolled-back test session,
    because a ``TestClient`` request runs against the application's session and
    would not see it otherwise.

    The campaign-level overrides a new campaign is created with are cleared
    first. That is not undoing the product's default so much as stating this
    file's premise out loud: every test below is about a campaign that has *not*
    yet been given the live opt-in, which is precisely the deployment state the
    repair was written for. Leaving the default in place would make each of them
    assert that a switch already on can be turned on.
    """

    with SessionLocal() as session:
        campaign = campaign_service.create_campaign(
            session,
            name=name,
            created_by_user_id=uuid.UUID(owner_id) if owner_id else None,
        )
        workbench_scenario.clear_new_campaign_defaults(session, campaign.id)
        controls.set_global_control(
            session, agent_id=RESEARCH, status=AgentControlStatus.ENABLED, config={}
        )
        session.commit()
        return campaign.id


def _live_now(campaign_id: uuid.UUID) -> bool:
    with SessionLocal() as session:
        campaign = campaign_service.get_campaign(session, campaign_id)
        assert campaign is not None
        return controls.campaign_live_opt_in(session, campaign=campaign, agent_id=RESEARCH)


def _post_live(client: TestClient, campaign_id: uuid.UUID, csrf: str, value: str = "1"):
    return client.post(
        f"/app/campaigns/{campaign_id}/agents/research/live",
        data={"live": value, "expected_version": "", "reason": "UAT", "_csrf": csrf},
        headers={"Sec-Fetch-Site": "same-origin"},
    )


def test_an_administrator_can_enable_live_research_for_one_campaign(hosted: TestClient) -> None:
    """The UAT path, end to end, through the page's own form."""

    _, csrf = _sign_in(hosted, role="admin", email=ADMIN_EMAIL)
    campaign_id = _committed_campaign("PE&VC MENA 200-1000")
    other_id = _committed_campaign("Untouched cohort")

    response = _post_live(hosted, campaign_id, csrf)

    assert response.status_code == 303, response.text
    assert _live_now(campaign_id) is True
    assert _live_now(other_id) is False

    withdrawn = hosted.post(
        f"/app/campaigns/{campaign_id}/agents/research/live",
        data={"live": "0", "expected_version": "1", "reason": "paused", "_csrf": csrf},
        headers={"Sec-Fetch-Site": "same-origin"},
    )
    assert withdrawn.status_code == 303, withdrawn.text
    assert _live_now(campaign_id) is False


def test_an_ordinary_operator_cannot_authorise_live_work_for_any_campaign(
    hosted: TestClient,
) -> None:
    """Their own campaign and somebody else's, both refused.

    The opt-in authorises real outbound research and metered provider spend for a
    whole cohort at once, so it sits with the administrators who hold the
    deployment's credentials — the same line the verification and logo.dev routes
    already draw. The campaign page stays readable to the operator, which is how
    they can see that the switch is off and say so.
    """

    owner_id, csrf = _sign_in(hosted, role="user", email="operator@vmr.example")
    own_campaign = _committed_campaign("Their own cohort", owner_id=owner_id)
    someone_elses = _committed_campaign("Another team's cohort")

    for campaign_id in (own_campaign, someone_elses):
        refused = _post_live(hosted, campaign_id, csrf)
        assert refused.status_code == 403, f"{campaign_id} -> {refused.status_code}"
        assert refused.json()["error"] == "admin_required"
        assert _live_now(campaign_id) is False

    assert hosted.get(f"/app/campaigns/{own_campaign}").status_code == 200


def test_the_campaign_page_names_the_missing_opt_in(hosted: TestClient) -> None:
    """The gap that made this a UAT blocker: nothing said the Agent was gated.

    Asserted on the Research stage panel, which is where an operator looking at
    contacts held by ``research_not_live`` actually lands.
    """

    _, _csrf = _sign_in(hosted, role="admin", email=ADMIN_EMAIL)
    campaign_id = _committed_campaign("PE&VC MENA 200-1000")

    page = hosted.get(f"/app/campaigns/{campaign_id}?stage=research")

    assert page.status_code == 200
    assert "Live Research work is not enabled for this campaign" in page.text
    assert "Enable live Research work" in page.text
    assert f"/app/campaigns/{campaign_id}/agents/research/live" in page.text


def test_an_agent_with_no_live_gate_offers_no_switch(hosted: TestClient) -> None:
    """Anti-vacuity: the panel is not simply always there.

    Asserted on the switch itself rather than on the words. The Agent list in
    the margin names every gated Agent on every stage view — that is the marker
    an operator needs in order to find this at all — so a text search would find
    "live work" on this page and prove nothing. What must be absent is the
    Company Agent's own form.
    """

    _, csrf = _sign_in(hosted, role="admin", email=ADMIN_EMAIL)
    campaign_id = _committed_campaign("PE&VC MENA 200-1000")

    page = hosted.get(f"/app/campaigns/{campaign_id}?stage=company")
    assert page.status_code == 200
    assert f"/app/campaigns/{campaign_id}/agents/company/live" not in page.text
    assert "Enable live Company work" not in page.text

    refused = hosted.post(
        f"/app/campaigns/{campaign_id}/agents/company/live",
        data={"live": "1", "expected_version": "", "_csrf": csrf},
        headers={"Sec-Fetch-Site": "same-origin"},
    )
    assert refused.status_code == 303
    assert "err=" in str(refused.headers["location"])


# ---------------------------------------------------------------------------
# Helpers used by more than one section
# ---------------------------------------------------------------------------


def _campaign_version(db: Session, campaign_id: uuid.UUID, agent_id: AgentIdentifier) -> int | None:
    override = db.scalars(
        select(CampaignAgentOverride).where(
            CampaignAgentOverride.campaign_id == campaign_id,
            CampaignAgentOverride.agent_id == agent_id,
        )
    ).one_or_none()
    return override.version if override else None


def _event_count(db: Session, campaign_contact_id: uuid.UUID) -> int:
    return len(
        db.scalars(
            select(CampaignContactAgentState).where(
                CampaignContactAgentState.campaign_contact_id == campaign_contact_id
            )
        ).all()
    )
