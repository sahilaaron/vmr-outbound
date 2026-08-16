"""New campaigns are autonomous until Ready for Sending.

The defect, as the live deployment showed it
--------------------------------------------
A campaign created through the product reached ``SENDING / DISABLED`` and stayed
there. Three separate decisions combined to produce it, and each looked
defensible on its own:

* every Agent past Company inherited the registry's ``default_status`` of
  ``DISABLED``, so a new campaign was created switched off;
* ``cadence_config`` was ``NULL``, so ``campaign_opted_in`` read ``is True`` of a
  key nobody had ever written and the seven-message sequence never ran;
* the pipeline walked past Personalization into Sending, which is disabled, has
  no adapter and is not skippable, and parked there permanently.

The product's contract is the opposite of all three: preparation runs on its own
until a Ready package exists, and sending is a manual human action afterwards. So
this file pins the contract rather than the mechanism —

* a new campaign is created with every preparation Agent on and the seven-message
  sequence opted in on the canonical ladder, through **every** creation path;
* Sending is never enabled, never queued, and never a prerequisite of Ready;
* an administrator can still turn any of it off afterwards;
* an existing campaign is not rewritten by any of this.

What it deliberately does not re-test: control precedence
(``test_workbench_agents.py``), the live opt-in repair
(``test_campaign_live_opt_in.py``), sequence generation itself
(``test_email_sequence.py``) and the Sheets surface as a whole
(``test_google_sheets_integration.py``). Each owns its own boundary; this file
owns the defaults and the preparation→Ready edge.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.core.auth.session import SESSION_COOKIE_NAME, SessionCodec
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.main import create_app
from app.models.agent import CampaignAgentOverride
from app.models.campaign import Campaign, CampaignContact
from app.models.contact import Contact
from app.models.email_evidence import ExactEmailVerification
from app.models.enums import (
    AgentControlStatus,
    AgentIdentifier,
    AgentJobStatus,
    CampaignStatus,
    EmailVerificationResult,
    PipelineEventType,
    PipelineStageStatus,
)
from app.models.verification_job import AgentJob
from app.services import campaigns as campaign_service
from app.services import customer_status, pipeline
from app.services.agents import controls
from app.services.agents.readiness import execution_readiness
from app.services.agents.registry import (
    AGENT_SPECS,
    PIPELINE_ORDER,
    PREPARATION_AGENTS,
    PREPARATION_TERMINAL_AGENT,
    next_preparation_agent,
)
from app.services.campaign_contacts import enrol_contact
from app.services.integrations.sheets.contract import RowStatus
from app.services.integrations.sheets.results import result_for
from app.services.personalization.cadence import (
    DEFAULT_ELAPSED_DAYS,
    campaign_opted_in,
    resolve_cadence,
    with_campaign_opt_in,
)
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.gmail_factory import build_sequence
from tests.hosted_auth_factory import TEST_CLIENT_ID, seed_account

#: The cadence the product promises, written out rather than imported, so a
#: change to the constant has to be a deliberate change to this expectation too.
CANONICAL_ELAPSED_DAYS = (0, 3, 7, 12, 18, 25, 35)

HOST = "vmr.test"
ORIGIN = f"https://{HOST}"
SESSION_SECRET = "test-session-secret-value-at-least-32-chars"
ADMIN_EMAIL = "admin@vmr.example"
OPERATOR_EMAIL = "operator@vmr.example"


class _AlwaysReadyProbe:
    def __call__(self) -> bool:  # pragma: no cover - trivial harness
        return True


@pytest.fixture()
def hosted(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The application as a hosted operator meets it, with both UIs mounted."""

    env = {
        "APP_ENV": "local",
        "TRUSTED_HOSTS": f'["{HOST}"]',
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


def _committed_campaign_named(name: str) -> Campaign:
    """Read back a campaign a route created on the application's own session."""

    with SessionLocal() as session:
        campaign = session.scalars(select(Campaign).where(Campaign.name == name)).one()
        session.expunge(campaign)
        return campaign


def _overrides(
    session: Session, campaign_id: uuid.UUID
) -> dict[AgentIdentifier, AgentControlStatus]:
    rows = session.scalars(
        select(CampaignAgentOverride).where(CampaignAgentOverride.campaign_id == campaign_id)
    ).all()
    return {row.agent_id: row.status for row in rows}


def _override_config(
    session: Session, campaign_id: uuid.UUID, agent_id: AgentIdentifier
) -> dict[str, object]:
    row = session.scalars(
        select(CampaignAgentOverride).where(
            CampaignAgentOverride.campaign_id == campaign_id,
            CampaignAgentOverride.agent_id == agent_id,
        )
    ).one()
    return dict(row.config or {})


def _assert_preparation_defaults(session: Session, campaign: Campaign) -> None:
    """Every preparation Agent on, Sending untouched — the whole Part A contract.

    Asserted through :func:`execution_readiness` as well as the stored rows,
    because the two answer different questions. The rows say what was written;
    readiness says what the campaign would actually do when its master switch is
    pressed, which is the thing the customer experiences and the thing that used
    to be false.
    """

    statuses = _overrides(session, campaign.id)
    for agent_id in PREPARATION_AGENTS:
        if agent_id is AgentIdentifier.CAPTURE:
            # Enabled permanently by the control service itself; an override
            # would record a decision nobody can make or reverse.
            assert agent_id not in statuses
            continue
        assert statuses.get(agent_id) is AgentControlStatus.ENABLED, agent_id
        if AGENT_SPECS[agent_id].requires_live_opt_in:
            assert _override_config(session, campaign.id, agent_id).get("live") is True, agent_id

    assert AgentIdentifier.SENDING not in statuses
    readiness = execution_readiness(
        session, campaign=campaign, prospective_stage=PREPARATION_TERMINAL_AGENT
    )
    assert readiness.blocking == ()
    assert readiness.holding == ()


# ---------------------------------------------------------------------------
# A. New campaign defaults
# ---------------------------------------------------------------------------


def test_the_customer_creation_path_defaults_every_preparation_agent_on(
    hosted: TestClient, db_session: Session
) -> None:
    """The real customer path: the New campaign form in the customer UI."""

    _user_id, csrf = _sign_in(hosted, role="user", email=OPERATOR_EMAIL)

    created = hosted.post(
        "/app/campaigns/new",
        data={"name": "Pune CEOs", "description": "UAT cohort", "_csrf": csrf},
        headers={"Sec-Fetch-Site": "same-origin"},
    )
    assert created.status_code == 303, created.text

    campaign = _committed_campaign_named("Pune CEOs")
    with SessionLocal() as session:
        _assert_preparation_defaults(session, session.get(Campaign, campaign.id))


def test_the_workbench_creation_path_uses_the_same_defaults(
    hosted: TestClient, db_session: Session
) -> None:
    """The admin path. A second surface must not mean a second policy."""

    _user_id, csrf = _sign_in(hosted, role="admin", email=ADMIN_EMAIL)

    created = hosted.post(
        "/app/campaigns/new",
        data={"name": "Admin cohort", "status": "draft", "_csrf": csrf},
        headers={"Sec-Fetch-Site": "same-origin"},
    )
    assert created.status_code == 303, created.text

    campaign = _committed_campaign_named("Admin cohort")
    with SessionLocal() as session:
        _assert_preparation_defaults(session, session.get(Campaign, campaign.id))


def test_the_json_api_creation_path_uses_the_same_defaults(
    hosted: TestClient, db_session: Session
) -> None:
    """The Phase 2 JSON API, which a service caller reaches instead of a form."""

    _user_id, csrf = _sign_in(hosted, role="admin", email=ADMIN_EMAIL)

    created = hosted.post(
        "/api/campaigns",
        json={"name": "Service cohort"},
        headers={"Sec-Fetch-Site": "same-origin", "X-CSRF-Token": csrf},
    )
    assert created.status_code == 201, created.text

    with SessionLocal() as session:
        campaign = session.get(Campaign, uuid.UUID(created.json()["id"]))
        assert campaign is not None
        _assert_preparation_defaults(session, campaign)


def test_every_creation_surface_goes_through_the_one_service(db_session: Session) -> None:
    """The defaults cannot be reached around, because there is one way in.

    A structural assertion rather than a behavioural one, and deliberately so:
    the failure this repair fixes was three surfaces agreeing by accident, and a
    fourth surface constructing ``Campaign(...)`` directly would reinstate it
    without failing any test above. Model construction inside ``app/models`` and
    inside the campaign service itself is what those modules are for.
    """

    offenders: list[str] = []
    root = Path(__file__).resolve().parents[1] / "app"
    allowed = {
        root / "models" / "campaign.py",
        root / "services" / "campaigns.py",
    }
    pattern = re.compile(r"(?<![\w.])Campaign\(")
    for path in root.rglob("*.py"):
        if path in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#") or "CampaignContact(" in stripped:
                continue
            if pattern.search(stripped):
                offenders.append(f"{path.relative_to(root)}:{number}")
    assert not offenders, (
        "a Campaign is being constructed outside the creation service, which would "
        f"skip the product's defaults: {offenders}"
    )


def test_a_new_campaign_is_opted_in_to_the_seven_message_sequence(db_session: Session) -> None:
    campaign = campaign_service.create_campaign(db_session, name="Sequence default")
    db_session.flush()

    assert campaign_opted_in(campaign) is True


def test_a_new_campaigns_cadence_is_exactly_the_canonical_seven(db_session: Session) -> None:
    campaign = campaign_service.create_campaign(db_session, name="Cadence default")
    db_session.flush()

    cadence = resolve_cadence(campaign)
    assert cadence.elapsed_days == CANONICAL_ELAPSED_DAYS
    assert len(cadence.elapsed_days) == 7
    # The constant and the promise must be the same thing.
    assert DEFAULT_ELAPSED_DAYS == CANONICAL_ELAPSED_DAYS


def test_creating_a_campaign_never_enables_sending(db_session: Session) -> None:
    """Preparation being ready is not permission to send it."""

    campaign = campaign_service.create_campaign(db_session, name="No sending")
    db_session.flush()

    assert AgentIdentifier.SENDING not in _overrides(db_session, campaign.id)
    effective = controls.effective_control(
        db_session, campaign=campaign, agent_id=AgentIdentifier.SENDING
    )
    assert effective.status is AgentControlStatus.DISABLED


def test_a_caller_that_states_its_own_cadence_is_not_overruled(db_session: Session) -> None:
    """A default fills a gap; it does not overwrite an answer.

    Both directions matter. A caller creating a deliberately single-draft
    campaign must be able to, and unrelated keys in the column must survive.
    """

    campaign = campaign_service.create_campaign(
        db_session,
        name="Explicit cadence",
        cadence_config={"sequence": {"enabled": False}, "notes": "kept"},
    )
    db_session.flush()

    assert campaign_opted_in(campaign) is False
    assert (campaign.cadence_config or {})["notes"] == "kept"


# ---------------------------------------------------------------------------
# B. Ready must not depend on Sending
# ---------------------------------------------------------------------------


def _enrolled(session: Session, campaign: Campaign, *, email: str | None = None) -> CampaignContact:
    contact = Contact(
        first_name="Sahil",
        last_name="Aaron",
        company_name="Verified Market Research",
        company_domain="verifiedmarketresearch.example",
        email=email,
        natural_key=f"sahil|aaron|{uuid.uuid4()}",
    )
    session.add(contact)
    session.flush()
    # Execution on, because that is the state the pipeline actually walks in:
    # while the master switch is off every Agent resolves DISABLED, which is the
    # operator's pause rather than anything this file is about.
    if not campaign.execution_enabled:
        campaign_service.set_campaign_execution(session, campaign.id, enabled=True, actor="test")
        session.flush()
    result = enrol_contact(
        session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="fixture",
        actor="test",
    )
    session.flush()
    return result.membership


def _complete_preparation(session: Session, membership: CampaignContact) -> None:
    """Walk every preparation stage to COMPLETED through the real projection."""

    for agent_id in PREPARATION_AGENTS:
        state = pipeline.agent_state(
            session, campaign_contact_id=membership.id, agent_id=agent_id, create=True
        )
        assert state is not None
        if state.status is PipelineStageStatus.COMPLETED:
            continue
        pipeline.transition_stage(
            session,
            membership=membership,
            agent_id=agent_id,
            target=PipelineStageStatus.COMPLETED,
            event_type=PipelineEventType.STAGE_COMPLETED,
            actor="test",
            reason_code="test_setup",
        )
    session.flush()


def test_the_preparation_boundary_is_the_last_preparation_agent() -> None:
    """Sending keeps its place in the registry and stops being queued work."""

    assert PREPARATION_TERMINAL_AGENT is AgentIdentifier.PERSONALIZATION
    assert AgentIdentifier.SENDING in PIPELINE_ORDER
    assert AgentIdentifier.SENDING not in PREPARATION_AGENTS
    assert next_preparation_agent(AgentIdentifier.PERSONALIZATION) is None
    assert next_preparation_agent(AgentIdentifier.INSIGHTS) is AgentIdentifier.PERSONALIZATION


def test_completing_preparation_ends_the_pipeline_instead_of_parking_at_sending(
    db_session: Session,
) -> None:
    """The exact live failure, asserted on the state it left behind.

    Before this boundary existed the membership finished Personalization,
    advanced into Sending, found it disabled and not skippable, and stopped with
    ``pipeline_status=DISABLED`` and ``next_stage=SENDING`` — a row no retry, no
    job and no operator action would ever move again.
    """

    campaign = campaign_service.create_campaign(db_session, name="Boundary")
    db_session.flush()
    membership = _enrolled(db_session, campaign, email="sahil@vmr.example")

    _complete_preparation(db_session, membership)

    assert membership.latest_completed_stage is PREPARATION_TERMINAL_AGENT
    assert membership.next_stage is None
    assert membership.pipeline_status is PipelineStageStatus.COMPLETED


def test_sending_is_never_queued(db_session: Session) -> None:
    """No automatic send, and no job that could become one."""

    campaign = campaign_service.create_campaign(db_session, name="No sending job")
    db_session.flush()
    campaign_service.set_campaign_execution(db_session, campaign.id, enabled=True, actor="test")
    membership = _enrolled(db_session, campaign, email="sahil@vmr.example")

    _complete_preparation(db_session, membership)

    sending_jobs = db_session.scalars(
        select(AgentJob).where(
            AgentJob.campaign_contact_id == membership.id,
            AgentJob.agent_id == AgentIdentifier.SENDING,
        )
    ).all()
    assert sending_jobs == []
    assert all(job.status is not AgentJobStatus.IN_PROGRESS for job in sending_jobs)


def test_a_complete_package_is_ready_although_sending_is_disabled(db_session: Session) -> None:
    """The contract, stated as the customer reads it.

    The fixture campaign has Sending disabled and no sending adapter exists at
    all, and no review or approval row is written. None of those may stand
    between a finished package and Ready.
    """

    fixture = build_sequence(db_session, email="ada@kiln.example")
    db_session.flush()

    status = customer_status.status_for_membership(
        db_session, campaign_contact_id=fixture.membership.id
    )
    assert status is customer_status.CustomerContactStatus.READY_FOR_SENDING

    effective = controls.effective_control(
        db_session, campaign=fixture.campaign, agent_id=AgentIdentifier.SENDING
    )
    assert effective.status is AgentControlStatus.DISABLED
    assert AGENT_SPECS[AgentIdentifier.SENDING].implemented is False


def test_a_stage_parked_at_sending_is_still_ready_when_the_package_exists(
    db_session: Session,
) -> None:
    """A contact left at the old boundary is owed the truth about its package.

    The boundary above stops new contacts reaching this state; the ones already
    in it are not thereby wrong. A written, valid seven-message sequence is a
    finished package whatever stage 9 says about itself.
    """

    fixture = build_sequence(db_session, email="ada@kiln.example")
    pipeline.transition_stage(
        db_session,
        membership=fixture.membership,
        agent_id=AgentIdentifier.SENDING,
        target=PipelineStageStatus.DISABLED,
        event_type=PipelineEventType.AGENT_DISABLED,
        actor="test",
        reason_code="registry_default",
        reason_detail="sending is disabled",
    )
    db_session.flush()

    assert (
        customer_status.status_for_membership(db_session, campaign_contact_id=fixture.membership.id)
        is customer_status.CustomerContactStatus.READY_FOR_SENDING
    )


def test_a_stage_parked_at_sending_without_a_package_is_not_processing(
    db_session: Session,
) -> None:
    """Anti-vacuity for the test above, and the honest answer for the live row.

    ``Processing`` is a promise that something is still happening. A contact
    parked on an Agent that has no adapter — which the control service refuses to
    enable — is not waiting for anything, and saying otherwise is what made the
    live campaign look busy forever.
    """

    campaign = campaign_service.create_campaign(db_session, name="Parked")
    db_session.flush()
    membership = _enrolled(db_session, campaign, email="sahil@vmr.example")
    pipeline.transition_stage(
        db_session,
        membership=membership,
        agent_id=AgentIdentifier.SENDING,
        target=PipelineStageStatus.DISABLED,
        event_type=PipelineEventType.AGENT_DISABLED,
        actor="test",
        reason_code="registry_default",
        reason_detail="sending is disabled",
    )
    db_session.flush()

    assert (
        customer_status.status_for_membership(db_session, campaign_contact_id=membership.id)
        is customer_status.CustomerContactStatus.COULD_NOT_PREPARE
    )


# ---------------------------------------------------------------------------
# C. The Sheets projection
# ---------------------------------------------------------------------------


def _verify(session: Session, contact: Contact, email: str) -> None:
    session.add(
        ExactEmailVerification(
            email=email,
            result=EmailVerificationResult.VALID,
            provider="millionverifier",
            policy_version="ver-1",
            checked_at=datetime.now(UTC) - timedelta(hours=1),
            contact_id=contact.id,
        )
    )
    session.flush()


def test_a_sheet_row_is_ready_when_the_package_exists(db_session: Session) -> None:
    fixture = build_sequence(db_session, email="ada@kiln.example")
    _verify(db_session, fixture.contact, "ada@kiln.example")

    result = result_for(db_session, membership=fixture.membership)

    assert result.status is RowStatus.READY
    assert result.email_address == "ada@kiln.example"
    assert len(result.messages) == 7


def test_a_finished_pipeline_the_sheet_cannot_call_ready_is_not_pending(
    db_session: Session,
) -> None:
    """The gap between two definitions of "usable", closed truthfully.

    The app calls a contact ready on an address it holds plus a valid sequence.
    This surface is stricter — the address must be ``VALID`` under the
    verification policy, not merely present — so a finished pipeline can be Ready
    in the app and not ready here. It must not therefore be *pending* here: the
    pipeline has ended and nothing will run again.
    """

    fixture = build_sequence(db_session, email="ada@kiln.example")
    fixture.membership.next_stage = None
    fixture.membership.pipeline_status = PipelineStageStatus.COMPLETED
    db_session.flush()

    app_status = customer_status.status_for_membership(
        db_session, campaign_contact_id=fixture.membership.id
    )
    assert app_status is customer_status.CustomerContactStatus.READY_FOR_SENDING

    result = result_for(db_session, membership=fixture.membership)
    assert result.status is RowStatus.COULD_NOT_PREPARE
    assert result.safe_failure_reason


def test_a_sheet_row_parked_on_an_unstartable_stage_is_never_pending(
    db_session: Session,
) -> None:
    """The projection defect, asserted in the sheet's own vocabulary.

    ``pending`` is a promise that the row will move. This row cannot: the Agent
    holding it has no adapter, so no administrator action exists that would start
    it. It used to read ``Pending`` on every refresh, forever.
    """

    campaign = campaign_service.create_campaign(db_session, name="Sheet parked")
    db_session.flush()
    membership = _enrolled(db_session, campaign, email="sahil@vmr.example")
    _verify(db_session, db_session.get(Contact, membership.contact_id), "sahil@vmr.example")
    pipeline.transition_stage(
        db_session,
        membership=membership,
        agent_id=AgentIdentifier.SENDING,
        target=PipelineStageStatus.DISABLED,
        event_type=PipelineEventType.AGENT_DISABLED,
        actor="test",
        reason_code="registry_default",
        reason_detail="sending is disabled",
    )
    db_session.flush()

    result = result_for(db_session, membership=membership)

    assert result.status is not RowStatus.PENDING
    assert result.status is RowStatus.COULD_NOT_PREPARE
    assert result.safe_failure_reason


def test_a_sheet_row_still_being_worked_on_stays_pending(db_session: Session) -> None:
    """Anti-vacuity: the fix must not turn ordinary waiting into a failure."""

    campaign = campaign_service.create_campaign(db_session, name="Sheet waiting")
    db_session.flush()
    membership = _enrolled(db_session, campaign, email="sahil@vmr.example")

    result = result_for(db_session, membership=membership)

    assert result.status is RowStatus.PENDING
    assert result.safe_failure_reason is None


def test_a_sheet_row_an_agent_holds_is_processing(db_session: Session) -> None:
    campaign = campaign_service.create_campaign(db_session, name="Sheet running")
    db_session.flush()
    membership = _enrolled(db_session, campaign, email="sahil@vmr.example")
    membership.pipeline_status = PipelineStageStatus.RUNNING
    db_session.flush()

    assert result_for(db_session, membership=membership).status is RowStatus.PROCESSING


# ---------------------------------------------------------------------------
# D. What the defaults must not take away
# ---------------------------------------------------------------------------


def test_an_administrator_can_still_disable_a_preparation_agent(db_session: Session) -> None:
    """A default is a starting point, not a lock."""

    campaign = campaign_service.create_campaign(db_session, name="Still controllable")
    db_session.flush()

    controls.set_campaign_override(
        db_session,
        campaign_id=campaign.id,
        agent_id=AgentIdentifier.RESEARCH,
        status=AgentControlStatus.DISABLED,
        actor="admin",
        reason="deliberately off",
    )
    db_session.flush()

    effective = controls.effective_control(
        db_session, campaign=campaign, agent_id=AgentIdentifier.RESEARCH
    )
    assert effective.status is AgentControlStatus.DISABLED


def test_an_administrator_can_still_switch_the_sequence_off(db_session: Session) -> None:
    campaign = campaign_service.create_campaign(db_session, name="Sequence off later")
    db_session.flush()
    assert campaign_opted_in(campaign) is True

    campaign_service.update_campaign(
        db_session,
        campaign.id,
        cadence_config=with_campaign_opt_in(campaign, enabled=False),
        actor="admin",
        reason="single drafts for this cohort",
    )
    db_session.flush()

    assert campaign_opted_in(campaign) is False


def test_an_existing_campaign_is_not_rewritten(db_session: Session) -> None:
    """No startup backfill, no silent migration.

    A campaign that predates this repair — modelled the way one exists in the
    database, with no cadence configuration and no overrides — keeps exactly what
    it had. Creating another campaign afterwards does not reach it either.
    """

    legacy = Campaign(name="Legacy cohort", status=CampaignStatus.DRAFT, settings_version=1)
    db_session.add(legacy)
    db_session.flush()

    campaign_service.create_campaign(db_session, name="Brand new")
    db_session.flush()
    db_session.refresh(legacy)

    assert legacy.cadence_config is None
    assert campaign_opted_in(legacy) is False
    assert _overrides(db_session, legacy.id) == {}
