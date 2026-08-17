"""Operator product controls: the switch that stopped needing a shell.

Hosted Beta UAT found the defect this whole slice answers. The Agent controls
were enabled, the Research jobs were paused with ``feature_disabled``, and the
only way to change that was SSH, an edit to ``/etc/vmr/vmr.env`` and a restart.
An administrator running the product should not need a shell to run the product.

This file asserts the four things that has to mean, and refuses to let any of
them be satisfied by accident:

* **The migration is behaviour-neutral.** An empty ``operational_settings`` table
  resolves to exactly the deployment's own ``FEATURES__`` values, so creating it
  changes nothing anywhere.
* **A stored row beats the environment.** That is the UAT requirement stated
  precisely: Company research goes on from the application while
  ``FEATURES__COMPANY_RESEARCH`` stays false in the environment.
* **Capability is a ceiling the row cannot lift, and it is checked on every
  read.** A provider with no credential cannot be turned on, and a row that
  somehow says otherwise still resolves to off.
* **The classification holds on the write path, not only in the form.** A
  deployment or security boundary is refused by name, and every ``FeatureFlags``
  field is accounted for by exactly one of the three classifications, so a flag
  added later with no decision fails here rather than appearing as an
  administrator-operable switch nobody classified.

The HTTP half is driven with the real hosted-session fixtures rather than an
unauthenticated client, because "an administrator can, a user cannot" is the
claim, and an unauthenticated client cannot tell those two apart. Every refusal
below is asserted on its body — the JSON ``error`` for an authorization refusal,
the flash text for a service refusal — so that a CSRF failure, a 404 or a
refusal from the wrong layer cannot pass as the refusal under test.

Section RECOVERY is the end-to-end one and the reason the slice exists: work
that a switched-off control already refused has to come back when the control is
switched on, without being skipped, cancelled or consumed.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote_plus

import httpx
import pytest
from app.core.auth.session import SESSION_COOKIE_NAME, SessionCodec
from app.core.config import get_settings
from app.core.features import FeatureFlags
from app.main import create_app
from app.models.audit_event import AuditEvent
from app.models.campaign import Campaign, CampaignContact
from app.models.company import Company
from app.models.contact import Contact
from app.models.enums import (
    AgentControlStatus,
    AgentIdentifier,
    AgentJobStatus,
    CampaignStatus,
    PipelineEventType,
    PipelineStageStatus,
)
from app.models.operational_setting import OperationalSetting
from app.models.pipeline import PipelineEvent
from app.models.verification_job import AgentJob
from app.services import campaign_contacts, pipeline
from app.services.agents import controls as agent_controls
from app.services.agents import jobs as agent_jobs
from app.services.agents import orchestrator
from app.services.agents.adapters import DEFAULT_ADAPTERS, ResearchAgentAdapter
from app.services.agents.registry import AGENT_SPECS
from app.services.operations import settings as operational
from app.services.research.contracts import ResearchRequest, SourcedFact, WorkerResult
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tests.hosted_auth_factory import TEST_CLIENT_ID, seed_account

ACTOR = "admin@vmr.example"
WORKER = "operational-configuration-worker"
RESEARCH_DOMAIN = "engines.example"


# ---------------------------------------------------------------------------
# Service-level helpers
# ---------------------------------------------------------------------------


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Any:
    """Rebuild ``Settings`` with ``env`` applied, and hand the result back.

    Every service function here takes an explicit ``settings``. Passing the one
    this returns is what makes a test's own environment its own: ``get_settings``
    is cached, and a test that changed a variable but read a stale cache would
    assert against a configuration that no longer exists.
    """

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    return get_settings()


def _audit_rows(session: Session, action: str) -> list[AuditEvent]:
    return list(
        session.scalars(
            select(AuditEvent).where(AuditEvent.action == action).order_by(AuditEvent.created_at)
        ).all()
    )


# ---------------------------------------------------------------------------
# Research scaffolding for the recovery section
# ---------------------------------------------------------------------------


class _FakeWorker:
    """A research source under full test control; it never reaches a network.

    It is only ever asked to run in the counterfactual — a Research job that gets
    past the feature gate. Every assertion in this file is about the gate, so the
    facts it would return do not matter; that it cannot browse does.
    """

    name = "fake"
    version = "test-1"

    def __init__(self) -> None:
        self.calls: list[ResearchRequest] = []

    def run(self, request: ResearchRequest) -> WorkerResult:
        self.calls.append(request)
        fact = SourcedFact(
            field="company_name",
            value="Analytical Engines Ltd",
            source_url=f"https://{RESEARCH_DOMAIN}/about",
            retrieved_at=datetime.now(UTC),
            extraction_method="explicit_statement:explicit",
            confidence=0.9,
            excerpt="...Analytical Engines Ltd...",
        )
        return WorkerResult(
            worker=self.name,
            worker_version=self.version,
            facts=(fact,),
            warnings=(),
            raw={"pages": [{"url": f"https://{RESEARCH_DOMAIN}/", "page_type": "home"}]},
            sufficient=True,
        )


def _research_adapters(worker: _FakeWorker) -> dict[AgentIdentifier, Any]:
    merged = dict(DEFAULT_ADAPTERS)
    merged[AgentIdentifier.RESEARCH] = ResearchAgentAdapter(
        workers_factory=lambda _names=None: (worker,)
    )
    return merged


def _research_campaign(session: Session) -> Campaign:
    campaign = Campaign(
        name=f"Research {uuid.uuid4()}",
        status=CampaignStatus.ACTIVE,
        execution_enabled=True,
    )
    session.add(campaign)
    session.flush()
    agent_controls.set_global_control(
        session,
        agent_id=AgentIdentifier.RESEARCH,
        status=AgentControlStatus.ENABLED,
        config={"live": True},
    )
    session.flush()
    return campaign


def _queued_research_job(
    session: Session, campaign: Campaign, *, label: str
) -> tuple[CampaignContact, AgentJob]:
    """One membership standing at Research with a due job, built the real way.

    Nothing here writes a status: the Campaign Contact is enrolled through
    ``campaign_contacts``, the earlier stages are completed through ``pipeline``,
    and the job is whatever ``orchestrator.schedule_next`` decided to create.
    """

    domain = f"{label}.{RESEARCH_DOMAIN}"
    company = Company(name=f"Analytical Engines {label}", domain=domain)
    session.add(company)
    session.flush()
    contact = Contact(
        first_name="Ada",
        last_name=label.title(),
        company_name=company.name,
        company_domain=company.domain,
        company_id=company.id,
        natural_key=f"ada|{label}|{uuid.uuid4()}",
    )
    session.add(contact)
    session.flush()

    membership = campaign_contacts.enrol_contact(
        session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        source_reference=f"operational-configuration-{label}",
        enqueue=False,
        desired_stage=AgentIdentifier.RESEARCH,
    ).membership
    for agent_id in (AgentIdentifier.IDENTITY, AgentIdentifier.COMPANY):
        pipeline.transition_stage(
            session,
            membership=membership,
            agent_id=agent_id,
            target=PipelineStageStatus.COMPLETED,
            event_type=PipelineEventType.STAGE_COMPLETED,
            actor="test-setup",
            reason_code="test_setup",
        )
    session.flush()
    job = orchestrator.schedule_next(session, membership=membership, actor="test-setup")
    assert job is not None, "the Research stage was not scheduled"
    assert job.agent_id is AgentIdentifier.RESEARCH
    session.flush()
    return membership, job


def _research_stage_status(session: Session, membership: CampaignContact) -> PipelineStageStatus:
    state = pipeline.agent_state(
        session,
        campaign_contact_id=membership.id,
        agent_id=AgentIdentifier.RESEARCH,
        create=False,
    )
    assert state is not None
    return state.status


# ---------------------------------------------------------------------------
# The hosted client, for the role-bearing half
# ---------------------------------------------------------------------------

HOST = "srv1885453.hstgr.cloud"
ORIGIN = f"https://{HOST}"
SESSION_SECRET = "test-session-secret-value-at-least-32-chars"
ADMIN_EMAIL = "sahil@verifiedmarketresearch.com"
#: Named only to satisfy the hosted startup validation, which refuses a local
#: database host in staging. Nothing connects to it: ``app/db/session.py`` bound
#: its engine to the suite's test database at import, long before this runs.
STAGING_DATABASE_URL = "postgresql+psycopg://vmr:secret@db.internal.example:5432/vmr_staging"


class _AlwaysReadyProbe:
    def __call__(self) -> None:
        return None


def _hosted_env(**overrides: str) -> dict[str, str]:
    """A hosted staging configuration with the administrator surface mounted.

    ``FEATURES__COMPANY_RESEARCH`` is deliberately absent: the environment says
    off, which is the state the UAT deployment was in and the state the HTTP
    tests below have to change without touching it.
    """

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
    env.update(overrides)
    return env


def _build_hosted_client(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> TestClient:
    for key, value in _hosted_env(**overrides).items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    app = create_app(readiness_probe=_AlwaysReadyProbe())
    return TestClient(app, base_url=ORIGIN, follow_redirects=False, raise_server_exceptions=False)


@pytest.fixture()
def hosted_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    try:
        yield _build_hosted_client(monkeypatch)
    finally:
        get_settings.cache_clear()


def _attach_session(client: TestClient, user_id: str, email: str) -> str:
    """Sign an account in through the real cookie codec and return its CSRF token.

    Deliberately not the login form: a failure to sign in would look identical to
    the refusal a test in this file is asserting.
    """

    from app.core.auth.session import OperatorSession, new_session_id

    now = int(time.time())
    session_id = new_session_id()
    codec = SessionCodec(SESSION_SECRET)
    client.cookies.set(
        SESSION_COOKIE_NAME,
        codec.encode_session(
            OperatorSession(
                email=email,
                subject="",
                display_name="",
                session_id=session_id,
                issued_at=now,
                expires_at=now + 3600,
                user_id=user_id,
                auth_version=1,
            )
        ),
    )
    return codec.csrf_token(session_id)


def _admin_session(client: TestClient) -> str:
    account = seed_account(email=ADMIN_EMAIL, role="admin")
    return _attach_session(client, account.user_id, account.email)


def _user_session(client: TestClient) -> str:
    account = seed_account(email="operator@vmr.example")
    return _attach_session(client, account.user_id, account.email)


def _post_control(
    client: TestClient, key: str, csrf: str, *, enabled: str = "true", **extra: str
) -> httpx.Response:
    """One control POST, carrying what a real browser would send.

    The CSRF token and the same-origin ``Sec-Fetch-Site`` are supplied so that a
    refusal is the one under test rather than either layer of the cross-site
    defence answering first.
    """

    return client.post(
        f"/admin/configuration/controls/{key}",
        data={"_csrf": csrf, "enabled": enabled, **extra},
        headers={"Sec-Fetch-Site": "same-origin"},
    )


def _flash(response: httpx.Response) -> str:
    """The flash text a 303 is carrying, decoded from its ``Location``."""

    return unquote_plus(response.headers["location"])


def _control_cell(body: str, key: str) -> str:
    """The rendered fragment for one control's row, so a match cannot be stray.

    Asserting ``"ON" in body`` on a page with eighteen controls proves nothing
    about the one under test.
    """

    marker = f'action="/admin/configuration/controls/{key}"'
    index = body.find(marker)
    assert index != -1, f"no form for {key} on the page"
    return body[max(0, index - 1200) : index + 600]


# ---------------------------------------------------------------------------
# SERVICE LAYER
# ---------------------------------------------------------------------------


def test_with_no_stored_row_every_control_resolves_to_its_deployment_default(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The migration is behaviour-neutral, stated as an assertion.

    ``operational_settings`` is created empty on every existing deployment, and
    the only acceptable consequence of that is none at all. So with no row, the
    resolved flag set must be the environment's flag set, field for field —
    including the ones this test deliberately switches on, which is what stops it
    passing merely because everything defaults to false.
    """

    settings = _settings(
        monkeypatch,
        FEATURES__COMPANY_RESEARCH="true",
        # Company research depends on it, so a deployment that defaults Research
        # on and this off is not "Research on" — it is Research with no source.
        FEATURES__RESEARCH_CLAUDE_FALLBACK="true",
        FEATURES__CSV_IMPORT="true",
        FEATURES__EMAIL_GENERATION="true",
        FEATURES__AUTOMATIC_COMPANY_DOMAIN_RESOLUTION="true",
    )
    assert db_session.scalar(select(func.count(OperationalSetting.key))) == 0

    resolved = operational.effective_flags(db_session, settings)

    for field in FeatureFlags.model_fields:
        assert getattr(resolved, field) == getattr(settings.features, field), field
    # And the deliberately-enabled ones really are on, so the loop above is not
    # comparing false to false eighteen times.
    assert resolved.company_research is True
    assert resolved.csv_import is True
    for key in operational.CONTROLS_BY_KEY:
        assert operational.enabled(db_session, key, settings) == getattr(settings.features, key)


def test_a_stored_row_wins_over_the_environment_default(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The UAT requirement itself: Company research goes on without a VPS edit.

    The environment stays exactly as the hosted deployment had it —
    ``FEATURES__COMPANY_RESEARCH`` unset, therefore false — and the administrator
    turns the control on from the application. If the environment were treated as
    a ceiling rather than a default, this would satisfy the letter of "operator
    control" and leave the UAT finding precisely where it was, so the assertion
    that the environment is *still* false is as important as the one that the
    control is now on.
    """

    settings = _settings(monkeypatch, FEATURES__RESEARCH_CLAUDE_FALLBACK="true")
    monkeypatch.delenv("FEATURES__COMPANY_RESEARCH", raising=False)
    get_settings.cache_clear()
    settings = get_settings()

    assert settings.features.company_research is False
    assert operational.enabled(db_session, "company_research", settings) is False
    before = operational.refusal(db_session, "company_research", settings)
    assert before is not None
    # The refusal is "nobody has turned it on", not "it cannot be turned on":
    # the required Claude source is available in this deployment, so the only
    # thing missing is the administrator's decision.
    assert "Admin → Configuration" in before

    change = operational.set_control(
        db_session,
        key="company_research",
        enabled_value=True,
        actor=ACTOR,
        reason="UAT: research jobs are paused",
        settings=settings,
    )
    db_session.flush()

    assert change.changed is True
    assert change.enabled is True
    assert AgentIdentifier.RESEARCH in change.reclaim_agents
    assert operational.enabled(db_session, "company_research", settings) is True
    assert operational.effective_flags(db_session, settings).company_research is True
    assert operational.refusal(db_session, "company_research", settings) is None
    # The environment was never touched. That is the whole point: no shell, no
    # edit to /etc/vmr/vmr.env, no restart.
    assert get_settings().features.company_research is False


def test_capability_is_a_ceiling_that_a_stored_row_cannot_lift(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider with no credential cannot be switched on, and stays off if forced.

    Two halves, and the second is the one that matters. Refusing the *write* is
    easy and insufficient: a row could arrive from an older release, a restore, a
    hand-run UPDATE, or a key removed from the environment after somebody turned
    the provider on. So the row is inserted directly through the ORM, bypassing
    the write path entirely, and the read path must still resolve it to off and
    still name the missing credential.

    The final third of the test supplies the credential and re-reads the *same*
    row, so "resolves to off" is shown to be the capability gate rather than a
    blanket refusal of the key.
    """

    settings = _settings(monkeypatch)
    monkeypatch.delenv("LOGO_DEV_API_KEY", raising=False)
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.has_logo_dev_key() is False

    with pytest.raises(operational.OperationalSettingError) as refused:
        operational.set_control(
            db_session,
            key="salesnav_domain_enrichment",
            enabled_value=True,
            actor=ACTOR,
            settings=settings,
        )
    assert "LOGO_DEV_API_KEY" in str(refused.value)
    assert db_session.get(OperationalSetting, "salesnav_domain_enrichment") is None

    # The read-time gate, proven against a row the write path never sanctioned.
    db_session.add(
        OperationalSetting(
            key="salesnav_domain_enrichment",
            enabled=True,
            reason="inserted behind the service's back",
            updated_by="restore",
            version=1,
        )
    )
    db_session.flush()

    assert operational.enabled(db_session, "salesnav_domain_enrichment", settings) is False
    assert operational.effective_flags(db_session, settings).salesnav_domain_enrichment is False
    read_refusal = operational.refusal(db_session, "salesnav_domain_enrichment", settings)
    assert read_refusal is not None
    assert "LOGO_DEV_API_KEY" in read_refusal
    view = operational.configuration_view(db_session, settings)
    row = next(r for r in view.controls if r.key == "salesnav_domain_enrichment")
    assert row.requested is True
    assert row.effective is False, "the screen must not report a held-off control as on"

    # Same row, credential now configured: the gate lifts, so the refusal above
    # was the capability and not the key.
    settings = _settings(monkeypatch, LOGO_DEV_API_KEY="logo-dev-key-for-tests")
    assert settings.has_logo_dev_key() is True
    assert operational.enabled(db_session, "salesnav_domain_enrichment", settings) is True


def test_a_deployment_or_security_setting_has_no_write_path(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The classification is enforced in the service, not implied by the form.

    ``workbench`` decides whether the operator routers are mounted at all, which
    no database row can change without a restart, and ``contact_capture_intake``
    is refused before serving by startup validation whose whole job is to reject
    exactly that state. A hand-crafted POST naming either one has to be refused
    by the write path, in terms an administrator can read.

    The three enumerations at the end are the part that keeps working after this
    slice ships: every ``FeatureFlags`` field must be classified exactly once, so
    a flag added next month is either an operator control, a deployment boundary
    or declared-and-inert — and never an unreviewed switch that quietly appears
    on an administrator screen.
    """

    settings = _settings(monkeypatch)

    for key in ("workbench", "contact_capture_intake"):
        with pytest.raises(operational.OperationalSettingError) as refused:
            operational.set_control(
                db_session, key=key, enabled_value=True, actor=ACTOR, settings=settings
            )
        message = str(refused.value)
        assert key in message
        assert "deployment setting" in message
        assert db_session.get(OperationalSetting, key) is None

    for key in operational.DEPLOYMENT_ONLY:
        for wanted in (True, False):
            with pytest.raises(operational.OperationalSettingError):
                operational.set_control(
                    db_session, key=key, enabled_value=wanted, actor=ACTOR, settings=settings
                )

    controls = set(operational.CONTROLS_BY_KEY)
    deployment = set(operational.DEPLOYMENT_ONLY)
    inert = set(operational.DECLARED_NOT_CONSULTED)
    assert controls & deployment == set(), "a key cannot be both operable and deployment-only"
    assert controls & inert == set()
    assert deployment & inert == set()
    fields = set(FeatureFlags.model_fields)
    assert controls | deployment | inert == fields, (
        "every feature flag must be classified: operator control, deployment "
        "boundary, or declared-and-not-consulted"
    )


def test_a_stale_version_is_refused_and_an_unchanged_setting_writes_nothing(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Optimistic concurrency, and the double-submit that must not look like one.

    Two administrators on the same Configuration screen must not silently
    overwrite each other, so a submitted version that no longer matches the row
    is a refusal rather than a write. The second half is the opposite failure: a
    double-submitted form, or two administrators agreeing, is not an error and
    must not bump the version or leave an audit event claiming a change that
    never happened.
    """

    settings = _settings(monkeypatch, FEATURES__RESEARCH_CLAUDE_FALLBACK="true")
    operational.set_control(
        db_session,
        key="company_research",
        enabled_value=True,
        actor=ACTOR,
        reason="first decision",
        settings=settings,
    )
    db_session.flush()
    row = db_session.get(OperationalSetting, "company_research")
    assert row is not None
    assert row.version == 1

    with pytest.raises(operational.OperationalSettingError) as stale:
        operational.set_control(
            db_session,
            key="company_research",
            enabled_value=False,
            actor="someone-else@vmr.example",
            expected_version=0,
            settings=settings,
        )
    assert "changed since the page was loaded" in str(stale.value)
    db_session.refresh(row)
    assert row.enabled is True
    assert row.version == 1

    audit_before = len(_audit_rows(db_session, "operational_setting.updated"))
    unchanged = operational.set_control(
        db_session,
        key="company_research",
        enabled_value=True,
        actor="someone-else@vmr.example",
        reason="clicked twice",
        expected_version=1,
        settings=settings,
    )
    db_session.flush()

    assert unchanged.changed is False
    assert unchanged.enabled is True
    assert unchanged.reclaim_agents == ()
    db_session.refresh(row)
    assert row.version == 1, "an unchanged setting must not bump the version"
    assert row.updated_by == ACTOR, "an unchanged setting must not rewrite the row"
    assert row.reason == "first decision"
    assert len(_audit_rows(db_session, "operational_setting.updated")) == audit_before


def test_every_change_writes_an_audit_event_naming_the_operator_and_the_reason(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Who turned Research off" is a question somebody always asks a week later.

    The mutable row is deliberately not an append-only version ledger — a switch
    is not a document — so the audit event is the entire history, and it has to
    carry the operator, the key, both states and the stated reason.
    """

    settings = _settings(monkeypatch, FEATURES__RESEARCH_CLAUDE_FALLBACK="true")
    operational.set_control(
        db_session,
        key="company_research",
        enabled_value=True,
        actor=ACTOR,
        reason="UAT: unblock the paused Research jobs",
        settings=settings,
    )
    operational.set_control(
        db_session,
        key="company_research",
        enabled_value=False,
        actor="second@vmr.example",
        reason="crawl volume during the demo",
        expected_version=1,
        settings=settings,
    )
    db_session.flush()

    events = _audit_rows(db_session, "operational_setting.updated")
    assert len(events) == 2

    first, second = events
    assert first.actor == ACTOR
    assert first.entity_type == "operational_setting"
    assert first.entity_id == "company_research"
    # No previous state, because there was no row: nobody had expressed an
    # opinion before, which is not the same as "it was off".
    assert first.previous_state is None
    assert first.new_state == "on"
    assert first.reason == "UAT: unblock the paused Research jobs"
    assert first.context == {"key": "company_research"}

    assert second.actor == "second@vmr.example"
    assert second.entity_id == "company_research"
    assert second.previous_state == "on"
    assert second.new_state == "off"
    assert second.reason == "crawl volume during the demo"


# ---------------------------------------------------------------------------
# HTTP LAYER
# ---------------------------------------------------------------------------


def test_an_administrator_turns_company_research_on_from_the_screen(
    hosted_client: TestClient,
) -> None:
    """The UAT requirement, end to end over HTTP, with the environment untouched.

    The deployment's ``FEATURES__COMPANY_RESEARCH`` is absent — off — for the
    whole of this test. The administrator posts and the Configuration page then
    reports the control as in force, which is the outcome that previously
    required SSH and a restart.

    It takes two posts now, and the first half of the test is the reason: Claude
    web research is the required source, so the screen refuses Company research
    while its prerequisite is off and names what to turn on instead of accepting
    a switch that would leave every Research job blocked.
    """

    csrf = _admin_session(hosted_client)
    assert get_settings().features.company_research is False

    refused = _post_control(hosted_client, "company_research", csrf, reason="UAT: unblock Research")
    assert refused.status_code == 303, refused.text[:300]
    refusal = _flash(refused)
    assert refusal.startswith("/admin/configuration?err=")
    assert "Claude Research availability" in refusal
    assert "Turn that on first" in refusal

    prerequisite = _post_control(hosted_client, "research_claude_fallback", csrf)
    assert prerequisite.status_code == 303, prerequisite.text[:300]
    assert "?ok=" in _flash(prerequisite)

    response = _post_control(
        hosted_client, "company_research", csrf, reason="UAT: unblock Research"
    )

    assert response.status_code == 303, response.text[:300]
    flash = _flash(response)
    assert flash.startswith("/admin/configuration?ok=")
    assert "Company research is now on." in flash

    page = hosted_client.get("/admin/configuration")
    assert page.status_code == 200
    cell = _control_cell(page.text, "company_research")
    assert ">ON<" in cell, "the page must report the control as in force"
    assert "Switch off" in cell, "the only control offered for an on control is switching it off"
    assert "No decision recorded yet" not in cell


def test_a_plain_user_may_neither_read_nor_change_the_configuration_screen(
    hosted_client: TestClient,
) -> None:
    """An operational switch is administrator work, and the refusal says so.

    Asserted on the JSON body rather than on the status code alone. A 403 from
    the cross-site backstop or from a CSRF failure would be indistinguishable by
    status, and either would let an authorization hole pass this test: the POST
    below carries a valid token for its own session precisely so that the only
    thing left to refuse it is the role.
    """

    csrf = _user_session(hosted_client)

    read = hosted_client.get("/admin/configuration")
    assert read.status_code == 403
    assert read.json()["error"] == "admin_required"

    write = _post_control(hosted_client, "company_research", csrf)
    assert write.status_code == 403
    assert write.json()["error"] == "admin_required"
    assert write.json()["error"] != "csrf_failed"

    # Nothing was recorded, so the refusal happened before the service.
    from app.db.session import SessionLocal

    with SessionLocal() as check:
        assert check.scalar(select(func.count(OperationalSetting.key))) == 0


def test_this_slice_adds_no_sending_authority(hosted_client: TestClient) -> None:
    """No switch here can start outbound sending, and none was quietly added.

    The Sending Agent is registered and unimplemented, and this slice is about
    making existing controls operable rather than about creating new capability.
    An operator control named ``sending`` would be a way to authorise sending
    from an Admin screen, so its absence is asserted from three directions: the
    registry has no such control, the write path refuses the key by name, and the
    Agent itself is still unimplemented.
    """

    assert "sending" not in {spec.key for spec in operational.PRODUCT_CONTROLS}
    assert "sending" not in operational.CONTROLS_BY_KEY
    assert "sending" not in FeatureFlags.model_fields

    csrf = _admin_session(hosted_client)
    for key in ("sending", "database_url", "not_a_flag"):
        response = _post_control(hosted_client, key, csrf)
        assert response.status_code == 303, key
        flash = _flash(response)
        assert flash.startswith("/admin/configuration?err=")
        assert f"{key} is not an operator-controlled setting." in flash

    # The registry, not the form: no adapter exists to execute a Sending job.
    assert AGENT_SPECS[AgentIdentifier.SENDING].implemented is False
    assert AgentIdentifier.SENDING not in DEFAULT_ADAPTERS


def test_the_configuration_screen_reports_credentials_without_printing_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capability evidence is a name and a yes/no, never a value.

    The screen has to answer "why can this not be turned on?", and the honest
    answer names the credential. Naming is not printing: a provider key rendered
    into an HTML page is a secret in a browser cache, a screenshot and a support
    ticket. So both keys are configured for this test — which is the only state
    in which a leak is possible at all — and the page must say "configured: yes"
    while containing neither value.
    """

    logo_secret = "logo-dev-secret-must-never-render-4f2a"
    verifier_secret = "millionverifier-secret-must-never-render-9c71"
    client = _build_hosted_client(
        monkeypatch,
        LOGO_DEV_API_KEY=logo_secret,
        MILLIONVERIFIER_API_KEY=verifier_secret,
    )
    try:
        _admin_session(client)
        page = client.get("/admin/configuration")
        assert page.status_code == 200
        body = page.text

        assert logo_secret not in body
        assert verifier_secret not in body
        # Nor any fragment long enough to be useful.
        assert logo_secret[:16] not in body
        assert verifier_secret[:16] not in body

        # Still reported as configured, so the assertions above are not passing
        # because the page failed to render the evidence at all.
        assert "logo.dev credential configured: yes" in body
        assert "MillionVerifier credential configured: yes" in body
    finally:
        client.close()
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# RESEARCH RECOVERY
# ---------------------------------------------------------------------------


def test_turning_company_research_on_returns_the_jobs_it_had_paused(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The UAT failure and its repair, driven through the real worker.

    This is the test the whole slice exists for. With the control off, a Research
    job runs, the adapter refuses it as ``feature_disabled``, and the orchestrator
    holds it: the job is PAUSED and the stage is BLOCKED. That state is exactly
    what the hosted deployment was sitting in, and turning the switch back on has
    to undo it without an operator finding and retrying every job by hand.

    What must *not* happen is as important as what must. Paused is recoverable;
    skipped, cancelled and failed are not. So the assertions below pin the job's
    identity — the same row, no replacement — and check that the stage never
    passed through a skip on its way back, because a "recovery" that consumes the
    work would satisfy a naive status assertion and lose the contact's research.
    """

    # Claude Research availability is on, so `company_research` is the only
    # thing switched off and `feature_disabled` is the accurate classification
    # of the refusal. (The other half of that pair — availability itself off —
    # is `test_a_claude_availability_pause_is_reclaimed_when_availability_returns`.)
    settings = _settings(monkeypatch, FEATURES__RESEARCH_CLAUDE_FALLBACK="true")
    monkeypatch.delenv("FEATURES__COMPANY_RESEARCH", raising=False)
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.features.company_research is False

    worker = _FakeWorker()
    campaign = _research_campaign(db_session)
    membership, job = _queued_research_job(db_session, campaign, label="paused")
    job_id = job.id

    outcome = orchestrator.run_next(
        db_session, worker_id=WORKER, adapters=_research_adapters(worker)
    )
    db_session.flush()

    assert outcome.job is not None
    assert "Company research is switched off" in outcome.message
    assert worker.calls == [], "the refused job must not have reached a research source"
    db_session.refresh(job)
    assert job.status is AgentJobStatus.PAUSED
    assert job.error_class == "feature_disabled"
    assert _research_stage_status(db_session, membership) is PipelineStageStatus.BLOCKED

    operational.set_control(
        db_session,
        key="company_research",
        enabled_value=True,
        actor=ACTOR,
        reason="UAT: unblock the paused Research jobs",
        settings=settings,
    )
    db_session.flush()

    resumed = orchestrator.reclaim_feature_paused_jobs(
        db_session, agent_ids=(AgentIdentifier.RESEARCH,), actor=ACTOR
    )
    db_session.flush()

    assert resumed == 1
    db_session.refresh(job)
    # The same row, brought back — not a replacement, and not a second job.
    assert job.id == job_id
    assert (
        db_session.scalar(
            select(func.count(AgentJob.id)).where(
                AgentJob.campaign_contact_id == membership.id,
                AgentJob.agent_id == AgentIdentifier.RESEARCH,
            )
        )
        == 1
    )
    assert job.status is AgentJobStatus.PENDING
    # Spelled out as well as pinned, because these are the outcomes a "recovery"
    # that consumed the work would have produced. A job has no SKIPPED status of
    # its own — skipping is a stage decision — so that half is asserted on the
    # pipeline events below.
    assert job.status not in {
        AgentJobStatus.CANCELLED,
        AgentJobStatus.SUCCEEDED,
        AgentJobStatus.FAILED,
    }
    assert job.next_run_at is not None
    assert job.error is None
    assert job.error_class is None
    assert job.last_error is None
    assert job.finished_at is None

    stage = _research_stage_status(db_session, membership)
    assert stage is not PipelineStageStatus.BLOCKED
    assert stage is PipelineStageStatus.WAITING

    events = list(
        db_session.scalars(
            select(PipelineEvent).where(
                PipelineEvent.campaign_contact_id == membership.id,
                PipelineEvent.agent_id == AgentIdentifier.RESEARCH,
            )
        ).all()
    )
    assert any(
        event.event_type is PipelineEventType.ELIGIBILITY_RESTORED
        and event.reason_code == "feature_enabled_reclaim"
        for event in events
    )
    assert not any(event.event_type is PipelineEventType.STAGE_SKIPPED for event in events), (
        "the work must be returned to the queue, never consumed"
    )
    assert not any(event.to_status is PipelineStageStatus.SKIPPED for event in events)

    reclaim_events = _audit_rows(db_session, "agent_job.feature_paused_reclaimed")
    assert len(reclaim_events) == 1
    assert reclaim_events[0].actor == ACTOR
    assert reclaim_events[0].entity_id == AgentIdentifier.RESEARCH.value
    assert reclaim_events[0].previous_state == "paused"
    assert reclaim_events[0].new_state == "queued"
    assert reclaim_events[0].context == {"resumed": 1}


def test_reclamation_touches_only_the_pauses_the_feature_caused(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A feature coming back on says nothing about any other reason for a pause.

    An operator paused the membership. An Agent control was switched off. Those
    pauses have their own causes and their own resolutions, and a Research switch
    being flipped is not one of them — resuming them here would quietly restart
    work an operator deliberately stopped.

    All three jobs sit on the same Agent, in the same table, in the same call's
    scope, so nothing but the pause classification distinguishes them. That is
    what makes the two untouched rows an assertion rather than a coincidence.
    """

    settings = _settings(monkeypatch, FEATURES__RESEARCH_CLAUDE_FALLBACK="true")
    monkeypatch.delenv("FEATURES__COMPANY_RESEARCH", raising=False)
    get_settings.cache_clear()
    settings = get_settings()

    worker = _FakeWorker()
    campaign = _research_campaign(db_session)
    jobs_by_label: dict[str, AgentJob] = {}
    for label in ("feature", "membership", "agent"):
        _membership, job = _queued_research_job(db_session, campaign, label=label)
        jobs_by_label[label] = job

    for _ in jobs_by_label:
        orchestrator.run_next(db_session, worker_id=WORKER, adapters=_research_adapters(worker))
    db_session.flush()
    for job in jobs_by_label.values():
        db_session.refresh(job)
        assert job.status is AgentJobStatus.PAUSED
        assert job.error_class == "feature_disabled"

    # Re-classify two of them the way the services that own those pauses do.
    agent_jobs.mark_paused(
        db_session,
        jobs_by_label["membership"],
        reason="an operator paused this Campaign Contact",
        reason_code="membership_paused",
    )
    agent_jobs.mark_paused(
        db_session,
        jobs_by_label["agent"],
        reason="the Research Agent control is disabled",
        reason_code="agent_disabled",
    )
    db_session.flush()

    operational.set_control(
        db_session,
        key="company_research",
        enabled_value=True,
        actor=ACTOR,
        settings=settings,
    )
    db_session.flush()

    resumed = orchestrator.reclaim_feature_paused_jobs(
        db_session, agent_ids=(AgentIdentifier.RESEARCH,), actor=ACTOR
    )
    db_session.flush()

    assert resumed == 1
    db_session.refresh(jobs_by_label["feature"])
    assert jobs_by_label["feature"].status is AgentJobStatus.PENDING

    for label, expected_class in (("membership", "membership_paused"), ("agent", "agent_disabled")):
        held = jobs_by_label[label]
        db_session.refresh(held)
        assert held.status is AgentJobStatus.PAUSED, label
        assert held.error_class == expected_class, label
        assert held.last_error is not None, label
        assert held.error is not None, label


def test_a_control_reports_the_agents_whose_paused_work_it_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The link between a switch and the reclaim, kept honest in both directions.

    ``company_research`` gates the Research Agent, so turning it on has paused
    work to reconcile. Most controls gate nothing — ``csv_import`` is a screen,
    not an execution stage — and for those the answer must be an empty tuple, so
    that a control being switched on does not trigger a reclaim sweep over an
    Agent it has no relationship with.
    """

    _settings(monkeypatch)

    assert AgentIdentifier.RESEARCH in operational.agents_gated_by("company_research")
    assert operational.agents_gated_by("csv_import") == ()
    assert operational.agents_gated_by("suppressions") == ()
    # Not a control at all — a deployment boundary and an unknown key both
    # answer "no agents" rather than raising, because the caller asks before it
    # knows.
    assert operational.agents_gated_by("workbench") == ()
    assert operational.agents_gated_by("not_a_flag") == ()

    # Every declared gate must name a registered Agent, so a rename cannot leave
    # a control pointing at an Agent that no longer exists.
    for spec in operational.PRODUCT_CONTROLS:
        for agent_id in spec.gates_agents:
            assert agent_id in AGENT_SPECS, f"{spec.key} gates an unregistered Agent"


# ---------------------------------------------------------------------------
# CLAUDE-PRIMARY RESEARCH: readiness that agrees with runtime, and a working undo
# ---------------------------------------------------------------------------
#
# Research now has one required source. That turned an optional extra into a
# prerequisite, and these cases pin the two operator-facing consequences the
# change has to carry with it: the Configuration screen must not report Research
# as in force while the source it requires is unavailable, and the switch that
# makes the source available again must return the work it had refused.


def _claude_blocked_research_job(
    db_session: Session, campaign: Campaign, worker: _FakeWorker, *, label: str
) -> tuple[CampaignContact, AgentJob]:
    """One Research job driven to a real ``claude_research_unavailable`` pause.

    Through the actual worker loop and the actual adapter, so the pause is the
    product's own classification rather than a status written by the test.
    """

    membership, job = _queued_research_job(db_session, campaign, label=label)
    outcome = orchestrator.run_next(
        db_session, worker_id=WORKER, adapters=_research_adapters(worker)
    )
    db_session.flush()
    assert outcome.job is not None
    db_session.refresh(job)
    assert job.status is AgentJobStatus.PAUSED
    assert job.error_class == "claude_research_unavailable"
    assert _research_stage_status(db_session, membership) is PipelineStageStatus.BLOCKED
    return membership, job


def test_company_research_is_not_effective_while_claude_research_is_unavailable(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deployment-default trap, asserted from the screen and from a real job.

    Every deployment that ran Research before this change is in exactly this
    state -- ``company_research`` on from the environment, Claude availability
    off -- and the previous model reported it as ``effective=True`` with no
    mention of Claude while every Research job blocked. Readiness has to agree
    with runtime, and the screen has to say what to do about it.
    """

    settings = _settings(monkeypatch, FEATURES__COMPANY_RESEARCH="true")
    monkeypatch.delenv("FEATURES__RESEARCH_CLAUDE_FALLBACK", raising=False)
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.features.company_research is True
    assert settings.features.research_claude_fallback is False

    view = operational.configuration_view(db_session, settings)
    research = next(row for row in view.controls if row.key == "company_research")

    assert research.requested is True, "the deployment does ask for Research"
    assert research.effective is False, "but it cannot execute, so it is not in force"
    assert research.capability.available is False
    assert research.capability.reason is not None
    assert "Claude Research availability" in research.capability.reason
    assert "Turn that on first" in research.capability.reason
    assert operational.enabled(db_session, "company_research", settings) is False

    # The same answer from the runtime read every service uses, and from a real
    # job driven through the real worker: the screen and the pipeline agree.
    worker = _FakeWorker()
    campaign = _research_campaign(db_session)
    _claude_blocked_research_job(db_session, campaign, worker, label="trap")
    assert worker.calls == [], "the deterministic crawler must never stand in"

    # Turning the prerequisite on is all it takes, and the dependency is not
    # circular: availability resolves without asking Company research anything.
    operational.set_control(
        db_session,
        key="research_claude_fallback",
        enabled_value=True,
        actor=ACTOR,
        reason="Claude CLI is configured on this host",
        settings=settings,
    )
    db_session.flush()

    assert operational.enabled(db_session, "research_claude_fallback", settings) is True
    assert operational.enabled(db_session, "company_research", settings) is True
    after = operational.configuration_view(db_session, settings)
    research_after = next(row for row in after.controls if row.key == "company_research")
    assert research_after.effective is True
    assert research_after.capability.available is True


def test_company_research_cannot_be_switched_on_before_its_required_source(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write path refuses the misleading state rather than only hiding it."""

    settings = _settings(monkeypatch)
    monkeypatch.delenv("FEATURES__RESEARCH_CLAUDE_FALLBACK", raising=False)
    get_settings.cache_clear()
    settings = get_settings()

    with pytest.raises(operational.OperationalSettingError) as refused:
        operational.set_control(
            db_session,
            key="company_research",
            enabled_value=True,
            actor=ACTOR,
            settings=settings,
        )
    assert "Claude Research availability" in str(refused.value)
    assert db_session.get(OperationalSetting, "company_research") is None

    # The order is enforced, not merely suggested: availability first, and then
    # the same write succeeds.
    operational.set_control(
        db_session,
        key="research_claude_fallback",
        enabled_value=True,
        actor=ACTOR,
        settings=settings,
    )
    db_session.flush()
    change = operational.set_control(
        db_session,
        key="company_research",
        enabled_value=True,
        actor=ACTOR,
        settings=settings,
    )
    db_session.flush()
    assert change.changed is True
    assert operational.enabled(db_session, "company_research", settings) is True


def test_a_claude_availability_pause_is_reclaimed_when_availability_returns(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The undo for the refusal this repair makes mandatory.

    ``gates_agents`` promises that turning a control back on returns the work it
    had refused. Research now refuses in two vocabularies -- the stage is off, or
    the source it requires is unavailable -- and a promise that recovers only the
    first leaves an operator resetting a hundred Contacts by hand at precisely
    the moment this repair has just blocked their batch.

    Four properties, because three of them are the ones a naive fix would break:
    the pause is recovered, recovering it twice does nothing the second time, an
    unrelated terminal failure is not resurrected, and no deterministic crawler
    call happens at any point.
    """

    settings = _settings(monkeypatch, FEATURES__COMPANY_RESEARCH="true")
    monkeypatch.delenv("FEATURES__RESEARCH_CLAUDE_FALLBACK", raising=False)
    get_settings.cache_clear()
    settings = get_settings()

    worker = _FakeWorker()
    campaign = _research_campaign(db_session)
    membership, job = _claude_blocked_research_job(db_session, campaign, worker, label="blocked")
    job_id = job.id

    # An unrelated Research job that genuinely failed. Terminal is not paused,
    # and no switch may resurrect it.
    _dead_membership, dead = _queued_research_job(db_session, campaign, label="dead")
    agent_jobs.mark_failed(
        db_session,
        dead,
        error_class="domain_not_authorized",
        reason="the domain of this contact is not authorized for research",
    )
    db_session.flush()

    change = operational.set_control(
        db_session,
        key="research_claude_fallback",
        enabled_value=True,
        actor=ACTOR,
        reason="Claude CLI restored",
        settings=settings,
    )
    db_session.flush()
    assert AgentIdentifier.RESEARCH in change.reclaim_agents

    resumed = orchestrator.reclaim_feature_paused_jobs(
        db_session, agent_ids=(AgentIdentifier.RESEARCH,), actor=ACTOR
    )
    db_session.flush()

    # 1. exactly one reclaim, and the same row rather than a replacement
    assert resumed == 1
    db_session.refresh(job)
    assert job.id == job_id
    assert job.status is AgentJobStatus.PENDING
    assert job.error is None and job.error_class is None and job.last_error is None
    assert job.next_run_at is not None
    assert _research_stage_status(db_session, membership) is PipelineStageStatus.WAITING
    assert (
        db_session.scalar(
            select(func.count(AgentJob.id)).where(
                AgentJob.campaign_contact_id == membership.id,
                AgentJob.agent_id == AgentIdentifier.RESEARCH,
            )
        )
        == 1
    ), "reclaiming must return the paused job, never queue a second one"

    # 2. repeating the gesture resumes nothing and duplicates nothing
    again = orchestrator.reclaim_feature_paused_jobs(
        db_session, agent_ids=(AgentIdentifier.RESEARCH,), actor=ACTOR
    )
    db_session.flush()
    assert again == 0
    db_session.refresh(job)
    assert job.status is AgentJobStatus.PENDING
    assert (
        db_session.scalar(
            select(func.count(AgentJob.id)).where(AgentJob.agent_id == AgentIdentifier.RESEARCH)
        )
        == 2
    ), "the two jobs this test created, and no others"

    # 3. the unrelated terminal failure is untouched
    db_session.refresh(dead)
    assert dead.status is AgentJobStatus.FAILED
    assert dead.error_class == "domain_not_authorized"
    assert dead.finished_at is not None

    # 4. nothing deterministic ran at any point, including during the reclaim
    assert worker.calls == []


def test_reclamation_does_not_resume_a_claude_pause_it_was_not_asked_about(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owning the classification is the safety property, and it still holds.

    Adding a second recoverable code must not widen the sweep to pauses that
    have their own causes: an operator pause is still nobody's business but its
    own, even when it landed on a job the new code had paused first.
    """

    _settings(monkeypatch, FEATURES__COMPANY_RESEARCH="true")
    monkeypatch.delenv("FEATURES__RESEARCH_CLAUDE_FALLBACK", raising=False)
    get_settings.cache_clear()

    worker = _FakeWorker()
    campaign = _research_campaign(db_session)
    _claude_blocked_research_job(db_session, campaign, worker, label="claude")
    _held_membership, held = _claude_blocked_research_job(
        db_session, campaign, worker, label="held"
    )
    agent_jobs.mark_paused(
        db_session,
        held,
        reason="an operator paused this Campaign Contact",
        reason_code="membership_paused",
    )
    db_session.flush()

    resumed = orchestrator.reclaim_feature_paused_jobs(
        db_session, agent_ids=(AgentIdentifier.RESEARCH,), actor=ACTOR
    )
    db_session.flush()

    assert resumed == 1
    db_session.refresh(held)
    assert held.status is AgentJobStatus.PAUSED
    assert held.error_class == "membership_paused"
    assert worker.calls == []
