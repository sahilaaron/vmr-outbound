"""Campaign enrolment and pipeline progression for imported contacts (§25.29-37)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from app.core.config import get_settings
from app.models.campaign import CampaignContact
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_evidence import ExactEmailVerification
from app.models.enums import (
    AgentControlStatus,
    AgentIdentifier,
    CampaignMembershipStatus,
    CompanyFieldSource,
    PipelineStageStatus,
    SuppressionReason,
    SuppressionType,
)
from app.models.verification_job import AgentJob
from app.services import campaign_contacts, suppressions
from app.services.agents import controls
from app.services.agents.adapters import DEFAULT_ADAPTERS, VerificationAgentAdapter
from app.services.agents.orchestrator import run_next
from app.services.agents.registry import PIPELINE_ORDER, get_agent_spec
from app.services.companies import provenance as company_provenance
from app.services.imports import campaign_import
from app.services.pipeline import agent_state
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tests import apollo_factory as af
from tests.test_email_agent_integration import ScriptedLiveProvider
from tests.test_research_claude_fallback import (
    FakeWorker,
    _claim,
)
from tests.test_research_claude_fallback import (
    ScriptedThinker as ResearchThinker,
)
from tests.test_research_claude_fallback import (
    _adapters as research_adapters,
)

pytestmark = pytest.mark.usefixtures("enable_csv_import")

WORKER = "import-pipeline-worker"
NOW = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _enable_research(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("FEATURES__COMPANY_RESEARCH", "true")
    monkeypatch.setenv("FEATURES__RESEARCH_CLAUDE_FALLBACK", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _import(session: Session, campaign: object, **overrides: str) -> object:
    return campaign_import.confirm(
        session,
        campaign_id=campaign.id,  # type: ignore[attr-defined]
        content=af.csv_bytes([af.row(**overrides)]),
        filename="apollo.csv",
    )


def _membership(session: Session, campaign_id: object) -> CampaignContact:
    return session.scalars(
        select(CampaignContact).where(CampaignContact.campaign_id == campaign_id)
    ).one()


def _enable(session: Session, *agents: AgentIdentifier, live: bool = True) -> None:
    for agent in agents:
        controls.set_global_control(
            session,
            agent_id=agent,
            status=AgentControlStatus.ENABLED,
            config={"live": True} if live else {},
        )
    session.flush()


def _drain(session: Session, adapters: object | None = None, rounds: int = 14) -> None:
    """Run the worker until it has no more due work."""

    for _ in range(rounds):
        outcome = run_next(session, worker_id=WORKER, adapters=adapters)  # type: ignore[arg-type]
        if outcome.job is None:
            return


def _size_evidence(session: Session, company: Company) -> None:
    """The Email discovery policy needs employee-count evidence to plan formats."""

    company_provenance.record_observation(
        session,
        company=company,
        field_name="company_size",
        value="120",
        source_kind=CompanyFieldSource.IMPORT,
        source_reference=f"import-size:{company.id}",
        observed_at=NOW,
        created_by="test",
    )
    company_provenance.reconcile_field(
        session, company=company, field_name="company_size", actor="test"
    )
    session.flush()


# --- 29-31. Enrolment -------------------------------------------------------


def test_an_imported_contact_is_enrolled_into_the_selected_campaign(
    db_session: Session,
) -> None:
    campaign = af.make_campaign(db_session)
    other = af.make_campaign(db_session)
    result = _import(db_session, campaign)
    assert result.imported == 1  # type: ignore[attr-defined]

    membership = _membership(db_session, campaign.id)
    assert membership.membership_status is CampaignMembershipStatus.ACTIVE
    assert membership.source_kind == "import"
    assert membership.source_batch_id == result.batch_id  # type: ignore[attr-defined]
    # And into no other campaign.
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(CampaignContact)
            .where(CampaignContact.campaign_id == other.id)
        )
        == 0
    )


def test_re_importing_does_not_duplicate_campaign_membership(db_session: Session) -> None:
    campaign = af.make_campaign(db_session)
    _import(db_session, campaign)
    # A second file that names the same person differently enough to be a new
    # row, but the same person.
    second = campaign_import.confirm(
        db_session,
        campaign_id=campaign.id,
        content=af.csv_bytes([af.row(**{"Title": "Director of Analytical Engines"})]),
        filename="apollo-v2.csv",
    )
    assert second.already_in_campaign == 1
    assert second.imported == 0
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(CampaignContact)
            .where(CampaignContact.campaign_id == campaign.id)
        )
        == 1
    )
    assert db_session.scalar(select(func.count()).select_from(Contact)) == 1


def test_the_same_contact_can_be_enrolled_into_another_campaign(db_session: Session) -> None:
    first = af.make_campaign(db_session)
    second = af.make_campaign(db_session)
    _import(db_session, first)
    result = _import(db_session, second)

    assert result.matched_existing == 1  # type: ignore[attr-defined]
    assert result.contacts_created == 0  # type: ignore[attr-defined]
    assert db_session.scalar(select(func.count()).select_from(Contact)) == 1
    assert db_session.scalar(select(func.count()).select_from(Company)) == 1
    assert db_session.scalar(select(func.count()).select_from(CampaignContact)) == 2
    # Each membership carries its own execution history.
    assert {row.campaign_id for row in db_session.scalars(select(CampaignContact)).all()} == {
        first.id,
        second.id,
    }


# --- 32-34. Stage legality and progression ----------------------------------


def test_email_and_verification_reach_legal_completed_states(db_session: Session) -> None:
    campaign = af.make_campaign(db_session, execution=True)
    _import(db_session, campaign)
    _enable(db_session, AgentIdentifier.EMAIL, AgentIdentifier.VERIFICATION)
    _drain(db_session)

    membership = _membership(db_session, campaign.id)
    for agent in (AgentIdentifier.EMAIL, AgentIdentifier.VERIFICATION):
        state = agent_state(
            db_session, campaign_contact_id=membership.id, agent_id=agent, create=False
        )
        assert state is not None, agent
        assert state.status is PipelineStageStatus.COMPLETED, agent
        assert state.completed_at is not None

    # The projection advanced in registry order, not by skipping ahead.
    assert membership.latest_completed_stage is not None
    assert PIPELINE_ORDER.index(membership.latest_completed_stage) >= PIPELINE_ORDER.index(
        AgentIdentifier.VERIFICATION
    )


def test_research_runs_normally_and_is_not_bypassed_by_the_import(
    db_session: Session,
) -> None:
    campaign = af.make_campaign(db_session, execution=True)
    _import(db_session, campaign)
    company = db_session.scalars(select(Company)).one()

    worker = FakeWorker()
    thinker = ResearchThinker(
        payload={
            "claims": [
                _claim(
                    "short_description",
                    "They build analytical engines.",
                )
            ]
        }
    )
    adapters = research_adapters(worker, thinker)
    _enable(
        db_session,
        AgentIdentifier.RESEARCH,
        AgentIdentifier.EMAIL,
        AgentIdentifier.VERIFICATION,
    )
    _drain(db_session, adapters)

    membership = _membership(db_session, campaign.id)
    research = agent_state(
        db_session,
        campaign_contact_id=membership.id,
        agent_id=AgentIdentifier.RESEARCH,
        create=False,
    )
    assert research is not None
    assert research.status is PipelineStageStatus.COMPLETED
    assert len(thinker.calls) == 1, "the primary Claude research source was not run"
    assert worker.calls == [], "the deterministic research worker ran in production"
    assert company.domain in thinker.calls[0].prompt

    # Research completed BEFORE Email — the import did not reorder the pipeline.
    email = agent_state(
        db_session,
        campaign_contact_id=membership.id,
        agent_id=AgentIdentifier.EMAIL,
        create=False,
    )
    assert email is not None
    assert email.status is PipelineStageStatus.COMPLETED
    assert research.completed_at is not None and email.completed_at is not None
    assert research.completed_at <= email.completed_at


def test_an_imported_contact_still_waits_for_research_when_research_is_blocked(
    db_session: Session,
) -> None:
    """The import must not let a Contact past a prerequisite it has not met."""

    campaign = af.make_campaign(db_session, execution=True)
    _import(db_session, campaign)
    # Research enabled but NOT live: the ordinary block for any contact.
    controls.set_global_control(
        db_session, agent_id=AgentIdentifier.RESEARCH, status=AgentControlStatus.ENABLED
    )
    _enable(db_session, AgentIdentifier.EMAIL, AgentIdentifier.VERIFICATION)
    _drain(db_session)

    membership = _membership(db_session, campaign.id)
    research = agent_state(
        db_session,
        campaign_contact_id=membership.id,
        agent_id=AgentIdentifier.RESEARCH,
        create=False,
    )
    assert research is not None
    assert research.status is PipelineStageStatus.BLOCKED
    assert research.reason_code == "research_not_live"
    # Email never ran, so no address was accepted on an unmet prerequisite.
    email = agent_state(
        db_session,
        campaign_contact_id=membership.id,
        agent_id=AgentIdentifier.EMAIL,
        create=False,
    )
    assert email is None or email.status is not PipelineStageStatus.COMPLETED


def test_downstream_stages_progress_through_their_existing_controls(
    db_session: Session,
) -> None:
    campaign = af.make_campaign(db_session, execution=True)
    _import(db_session, campaign)
    _enable(db_session, AgentIdentifier.EMAIL, AgentIdentifier.VERIFICATION)
    _drain(db_session)

    membership = _membership(db_session, campaign.id)
    # Insights and Personalization are disabled by default and skippable, so the
    # orchestrator steps over them exactly as it does for any other contact.
    for agent in (AgentIdentifier.INSIGHTS, AgentIdentifier.PERSONALIZATION):
        state = agent_state(
            db_session, campaign_contact_id=membership.id, agent_id=agent, create=False
        )
        assert state is not None, agent
        assert state.status is PipelineStageStatus.SKIPPED, agent
        assert state.reason_code == "control_disabled_autoskip"


# --- 35-36. Personalization input, and sending ------------------------------


def test_personalization_operates_on_the_imported_primary_address(
    db_session: Session,
) -> None:
    """The imported address is the address every later stage reads.

    Asserted through Personalization's own suppression gate rather than by
    inspecting a draft: that gate reads ``contact.email``, so suppressing the
    imported address and watching the gate refuse proves the stage is working
    from the imported address and not from something else.
    """

    campaign = af.make_campaign(db_session, execution=True)
    _import(db_session, campaign)
    contact = db_session.scalars(select(Contact)).one()
    assert contact.email == "ada@engines.example"

    membership = _membership(db_session, campaign.id)
    from app.services.personalization import generation as personalization_generation

    suppressions.add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value="ada@engines.example",
        reason=SuppressionReason.OPT_OUT,
        actor="test",
    )
    db_session.flush()

    with pytest.raises(personalization_generation.PreviewError) as excinfo:
        personalization_generation.generate(
            db_session,
            membership=membership,
            policy=None,  # type: ignore[arg-type]
            thinker=None,  # type: ignore[arg-type]
        )
    # The refusal names the opt-out that was placed on the IMPORTED address, so
    # the address Personalization read can only have been the imported one.
    assert "opt_out" in str(excinfo.value)
    assert excinfo.value.code == "suppression"


def test_sending_remains_unavailable_for_imported_contacts(db_session: Session) -> None:
    campaign = af.make_campaign(db_session, execution=True)
    _import(db_session, campaign)
    _enable(db_session, AgentIdentifier.EMAIL, AgentIdentifier.VERIFICATION)
    _drain(db_session)

    # Sending has no adapter and is not skippable: nothing steps over it.
    assert get_agent_spec(AgentIdentifier.SENDING).implemented is False
    assert get_agent_spec(AgentIdentifier.SENDING).skippable is False
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(AgentJob)
            .where(AgentJob.agent_id == AgentIdentifier.SENDING)
        )
        == 0
    )
    membership = _membership(db_session, campaign.id)
    sending = agent_state(
        db_session,
        campaign_contact_id=membership.id,
        agent_id=AgentIdentifier.SENDING,
        create=False,
    )
    assert sending is None or sending.status is not PipelineStageStatus.COMPLETED


# --- 37. Nothing changes for contacts that did not arrive by import ---------


def test_a_non_imported_contact_still_uses_the_ordinary_discovery_path(
    db_session: Session,
) -> None:
    campaign = af.make_campaign(db_session, execution=True)
    _import(db_session, campaign)
    company = db_session.scalars(select(Company)).one()
    _size_evidence(db_session, company)

    grace = Contact(
        first_name="Grace",
        last_name="Hopper",
        company_name=company.name,
        company_domain=company.domain,
        company_id=company.id,
        natural_key=f"grace|hopper|{company.domain}",
    )
    db_session.add(grace)
    db_session.flush()
    campaign_contacts.enrol_contact(
        db_session,
        campaign_id=campaign.id,
        contact_id=grace.id,
        source_type="manual",
        source_reference="not-an-import",
        enqueue=True,
    )
    _enable(db_session, AgentIdentifier.EMAIL, AgentIdentifier.VERIFICATION)

    provider = ScriptedLiveProvider(["ok"])
    adapters = dict(DEFAULT_ADAPTERS)
    adapters[AgentIdentifier.VERIFICATION] = VerificationAgentAdapter(
        provider_factory=lambda _settings: provider
    )
    _drain(db_session, adapters, rounds=24)

    grace_email_job = db_session.scalars(
        select(AgentJob).where(
            AgentJob.agent_id == AgentIdentifier.EMAIL,
            AgentJob.contact_id == grace.id,
        )
    ).one()
    assert grace_email_job.result is not None
    assert grace_email_job.result["domain_outcome"] != "imported_email_accepted"

    # The ordinary path generated candidates and asked a provider about one.
    assert provider.calls, "the ordinary discovery path did not reach the provider"
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(ExactEmailVerification)
            .where(ExactEmailVerification.contact_id == grace.id)
        )
        >= 1
    )

    # And the imported contact in the SAME campaign still spent nothing.
    imported_contact = db_session.scalars(
        select(Contact).where(Contact.email == "ada@engines.example")
    ).one()
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(ExactEmailVerification)
            .where(ExactEmailVerification.contact_id == imported_contact.id)
        )
        == 0
    )
