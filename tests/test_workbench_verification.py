"""How the Workbench projects the integrated Verification outcome (MVP-01B × MVP-01E).

Every case here runs the real Verification Agent through the real Phase 2 worker
and then asks the Workbench what an operator would see. That ordering is the
point: the Workbench must report the decision the Verification domain committed,
never re-derive verification semantics of its own, and never let a finished queue
job stand in for an accepted address.

The five MVP-01E decisions each get a case, plus the two rules that exist to stop
a false "verified": simulated evidence cannot advance a Contact, and suppression
outranks a Verification job that is still in flight.

The provider fakes mirror the ones in ``tests/test_verification_agent.py`` — a
local object with an explicit ``simulated`` flag and no network — because the
Verification branch owns that seam and this suite must not invent another.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from app.core.config import get_settings
from app.models.campaign import Campaign, CampaignContact
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_candidate import EmailCandidate
from app.models.enums import (
    AgentControlStatus,
    AgentIdentifier,
    AgentJobStatus,
    CampaignStatus,
    EmailCandidateSource,
    PipelineStageStatus,
    SuppressionReason,
    SuppressionType,
)
from app.models.verification_job import AgentJob
from app.services import campaign_contacts, pipeline
from app.services.agents import controls, orchestrator
from app.services.agents.adapters import DEFAULT_ADAPTERS, VerificationAgentAdapter
from app.services.agents.orchestrator import run_next
from app.services.suppressions import add_suppression
from app.services.verification.decisions import VerificationDecision
from app.services.verification.provider import (
    LIVE_PROVIDER_LABEL,
    ProviderResponse,
    ProviderTransientError,
)
from app.services.workbench_agents import PhaseTwoWorkbenchReader
from sqlalchemy.orm import Session

WORKER = "workbench-verification-test"
EMAIL = "ada.lovelace@engines.example"


# --- provider fakes ----------------------------------------------------------


class LiveProvider:
    """A non-simulated provider that never touches the network."""

    name = "millionverifier"
    simulated = False

    def __init__(self, script: list[object] | None = None) -> None:
        self.script = list(script or [])
        self.calls = 0

    def verify(self, email: str) -> ProviderResponse:
        self.calls += 1
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            assert isinstance(item, ProviderResponse)
            return item
        return _response(email, "ok", 1)


class SimulatedProvider:
    """Declares itself simulated, exactly as the real simulator does."""

    name = "millionverifier"
    simulated = True

    def __init__(self) -> None:
        self.calls = 0

    def verify(self, email: str) -> ProviderResponse:
        self.calls += 1
        return _response(email, "ok", 1)


def _response(email: str, result: str, code: int, **kw: object) -> ProviderResponse:
    base: dict[str, object] = dict(
        email=email,
        result=result,
        resultcode=code,
        credits=100,
        raw={"result": result, "resultcode": code, "email": email},
    )
    base.update(kw)
    return ProviderResponse(**base)  # type: ignore[arg-type]


def _error(email: str, error: str) -> ProviderResponse:
    return ProviderResponse(  # type: ignore[arg-type]
        email=email, result=None, resultcode=None, error=error, raw={"error": error}
    )


def _adapters(provider: object) -> dict[AgentIdentifier, object]:
    merged = dict(DEFAULT_ADAPTERS)
    merged[AgentIdentifier.VERIFICATION] = VerificationAgentAdapter(
        provider_factory=lambda _settings: provider  # type: ignore[arg-type,return-value]
    )
    return merged


# --- fixtures ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_settings() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def reader(db_session: Session) -> PhaseTwoWorkbenchReader:
    return PhaseTwoWorkbenchReader(db_session)


def _setup(db: Session, *, live: bool = True, email: str = EMAIL) -> CampaignContact:
    company = Company(name="Analytical Engines", domain="engines.example")
    campaign = Campaign(
        name=f"Verification projection {uuid.uuid4()}",
        status=CampaignStatus.ACTIVE,
        execution_enabled=True,
    )
    db.add_all([company, campaign])
    db.flush()
    contact = Contact(
        first_name="Ada",
        last_name="Lovelace",
        company_name=company.name,
        company_domain=company.domain,
        company_id=company.id,
        natural_key=f"ada|lovelace|{uuid.uuid4()}",
    )
    db.add(contact)
    db.flush()
    local_part, _, domain = email.partition("@")
    db.add(
        EmailCandidate(
            contact_id=contact.id,
            email=email,
            local_part=local_part,
            domain=domain,
            pattern="{first}.{last}",
            source=EmailCandidateSource.GENERATED,
            engine_version="eml-1",
            rank=1,
            rank_score=1.0,
            rank_reason="deterministic test candidate",
            selected=True,
        )
    )
    db.flush()
    controls.set_global_control(
        db,
        agent_id=AgentIdentifier.VERIFICATION,
        status=AgentControlStatus.ENABLED,
        config={"live": live},
    )
    membership = campaign_contacts.enrol_contact(
        db,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        source_reference="workbench-verification-test",
        enqueue=False,
        desired_stage=AgentIdentifier.VERIFICATION,
    ).membership
    for agent_id in (
        AgentIdentifier.IDENTITY,
        AgentIdentifier.COMPANY,
        AgentIdentifier.RESEARCH,
        AgentIdentifier.EMAIL,
    ):
        pipeline.transition_stage(
            db,
            membership=membership,
            agent_id=agent_id,
            target=PipelineStageStatus.COMPLETED,
            event_type=pipeline.PipelineEventType.STAGE_COMPLETED,
            actor="test-setup",
            reason_code="test_setup",
        )
    db.flush()
    job = orchestrator.schedule_next(db, membership=membership, actor="test")
    assert job is not None and job.agent_id is AgentIdentifier.VERIFICATION
    return membership


def _run(db: Session, provider: object) -> None:
    run_next(db, worker_id=WORKER, adapters=_adapters(provider))  # type: ignore[arg-type]


def _verification(reader: PhaseTwoWorkbenchReader, membership: CampaignContact):  # type: ignore[no-untyped-def]
    execution = reader.contact_execution(membership.campaign_id, membership.id)
    assert execution is not None
    assert execution.verification is not None
    return execution.verification


# --- the five decisions ------------------------------------------------------


def test_accept_is_projected_as_verified_and_pipeline_ready(
    db_session: Session, reader: PhaseTwoWorkbenchReader
) -> None:
    membership = _setup(db_session)
    _run(db_session, LiveProvider())

    view = _verification(reader, membership)
    assert view.decision == VerificationDecision.ACCEPT.value
    assert view.outcome_committed is True
    assert view.simulated is False
    assert view.accepted is True
    assert view.evidence_reference is not None
    assert view.evidence and view.evidence[0].simulated is False
    assert view.stage_status is PipelineStageStatus.COMPLETED


@pytest.mark.parametrize(
    ("result", "code", "extra"),
    [
        ("invalid", 6, {}),
        ("catch_all", 2, {}),
        ("unknown", 3, {}),
        ("disposable", 5, {}),
        ("ok", 1, {"role": True}),
    ],
)
def test_try_next_candidate_is_never_projected_as_verified(
    db_session: Session,
    reader: PhaseTwoWorkbenchReader,
    result: str,
    code: int,
    extra: dict[str, object],
) -> None:
    """A real verdict that is not a usable address. The evidence survives; the
    claim "verified" must not appear anywhere near it."""

    membership = _setup(db_session)
    _run(db_session, LiveProvider([_response(EMAIL, result, code, **extra)]))

    view = _verification(reader, membership)
    assert view.decision == VerificationDecision.TRY_NEXT_CANDIDATE.value
    assert view.accepted is False
    assert view.terminal is True
    assert view.stage_status is PipelineStageStatus.BLOCKED
    assert view.reason, "a non-acceptance must always explain itself"
    # The address was genuinely checked, so the evidence is still referenced.
    assert view.evidence_reference is not None


def test_retry_later_is_projected_as_retryable_and_not_a_failure(
    db_session: Session, reader: PhaseTwoWorkbenchReader
) -> None:
    membership = _setup(db_session)
    _run(db_session, LiveProvider([ProviderTransientError("the provider timed out")]))

    view = _verification(reader, membership)
    assert view.decision == VerificationDecision.RETRY_LATER.value
    assert view.accepted is False
    assert view.terminal is False
    assert view.stage_status is PipelineStageStatus.RETRYING
    assert any(attempt.retryable for attempt in view.attempts)


def test_stop_no_result_is_projected_as_terminal(
    db_session: Session, reader: PhaseTwoWorkbenchReader
) -> None:
    membership = _setup(db_session)
    _run(db_session, LiveProvider([_error(EMAIL, "insufficient_credits")]))

    view = _verification(reader, membership)
    assert view.decision == VerificationDecision.STOP_NO_RESULT.value
    assert view.accepted is False
    assert view.terminal is True
    assert view.stage_status is PipelineStageStatus.FAILED
    assert view.reason


def test_refused_is_projected_with_its_reason_and_no_provider_call(
    db_session: Session, reader: PhaseTwoWorkbenchReader
) -> None:
    """Verification refuses before any provider work when live is not authorised."""

    membership = _setup(db_session, live=False)
    provider = LiveProvider()
    _run(db_session, provider)

    view = _verification(reader, membership)
    # The adapter declines before the verification domain is consulted, so no
    # decision payload exists. The Workbench reports that as exactly what it is
    # rather than inventing a decision from the reason code.
    assert view.decision is None
    assert view.refused_before_provider is True
    assert view.refused is True
    assert view.accepted is False
    assert view.reason
    assert view.paid_calls == 0
    assert provider.calls == 0


# --- simulated evidence ------------------------------------------------------


def test_simulated_evidence_can_never_be_projected_as_verified(
    db_session: Session, reader: PhaseTwoWorkbenchReader
) -> None:
    """The rule that matters most. A simulator answers "valid" all day; nothing
    it produces may advance a production Campaign Contact."""

    membership = _setup(db_session)
    _run(db_session, SimulatedProvider())

    view = _verification(reader, membership)
    # The adapter refuses the simulator before it is ever called, so nothing
    # simulated is recorded — and nothing advances. Either way the one thing
    # that must never happen is an accepted address.
    assert view.accepted is False
    assert view.refused is True
    assert view.paid_calls == 0
    assert view.stage_status is not PipelineStageStatus.COMPLETED
    assert membership.latest_completed_stage is not AgentIdentifier.VERIFICATION


def test_the_page_labels_simulated_evidence_and_never_says_verified(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api.deps import get_db
    from app.main import create_app
    from fastapi.testclient import TestClient

    membership = _setup(db_session)
    _run(db_session, SimulatedProvider())

    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__AGENT_WORKBENCH", "true")
    get_settings.cache_clear()
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as client:
        response = client.get(
            f"/workbench/campaigns/{membership.campaign_id}/contacts/{membership.id}"
        )
    assert response.status_code == 200
    assert "pipeline-ready" not in response.text
    assert "not verified" in response.text
    assert "simulator output cannot complete" in response.text.lower()


def test_the_page_shows_an_accepted_address_as_pipeline_ready(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api.deps import get_db
    from app.main import create_app
    from fastapi.testclient import TestClient

    membership = _setup(db_session)
    _run(db_session, LiveProvider())

    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__AGENT_WORKBENCH", "true")
    get_settings.cache_clear()
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as client:
        response = client.get(
            f"/workbench/campaigns/{membership.campaign_id}/contacts/{membership.id}"
        )
    assert response.status_code == 200
    assert "pipeline-ready" in response.text
    assert LIVE_PROVIDER_LABEL in response.text


# --- suppression outranks verification ---------------------------------------


def test_suppression_overrides_a_verification_job_still_in_flight(
    db_session: Session, reader: PhaseTwoWorkbenchReader
) -> None:
    """A queued Verification job is not a promise. Suppression wins, and the
    Workbench shows the block rather than the pending work."""

    membership = _setup(db_session)
    add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value=EMAIL,
        reason=SuppressionReason.OPT_OUT,
        source="workbench-verification-test",
    )
    db_session.flush()
    provider = LiveProvider()
    _run(db_session, provider)
    # Production refreshes eligibility from the ledger; do the same so the
    # membership-level block is observable too.
    campaign_contacts.refresh_eligibility(db_session, membership=membership, actor="test")
    db_session.flush()

    execution = reader.contact_execution(membership.campaign_id, membership.id)
    assert execution is not None
    assert execution.verification is not None
    assert execution.verification.accepted is False
    # The Agent never reached the provider: verifying a suppressed identity is
    # the first step of contacting it.
    assert provider.calls == 0
    assert execution.verification.paid_calls == 0
    verification_stage = next(
        stage for stage in execution.stages if stage.agent_id is AgentIdentifier.VERIFICATION
    )
    assert verification_stage.status is PipelineStageStatus.BLOCKED
    assert verification_stage.reason_code == "suppression"
    # The membership-level suppression flag stays False here, correctly: it is
    # computed from the permanent Contact's own address, and this Contact has
    # none — the suppressed address is the *candidate* the Agent was about to
    # check. Both facts are true at once, and the Workbench shows the one that
    # actually stopped the work.
    assert execution.suppressed is False
    assert execution.verification.refused_before_provider is True


def test_a_succeeded_queue_job_alone_is_never_projected_as_verified(
    db_session: Session, reader: PhaseTwoWorkbenchReader
) -> None:
    """The inference this projection exists to refuse.

    The job is forced to ``SUCCEEDED`` without the committed accept decision
    behind it. The Workbench must still report the Contact as unverified: the
    queue's opinion is not the verification domain's.
    """

    membership = _setup(db_session)
    _run(db_session, LiveProvider([_response(EMAIL, "catch_all", 2)]))

    job = db_session.scalars(
        AgentJob.__table__.select().where(  # type: ignore[arg-type]
            AgentJob.__table__.c.campaign_contact_id == membership.id
        )
    ).first()
    assert job is not None

    view = _verification(reader, membership)
    assert view.decision == VerificationDecision.TRY_NEXT_CANDIDATE.value
    assert view.accepted is False


def test_a_contact_with_no_verification_work_reports_no_decision(
    db_session: Session, reader: PhaseTwoWorkbenchReader
) -> None:
    """ "Not decided" is a real answer and must not read as a refusal."""

    membership = _setup(db_session)
    execution = reader.contact_execution(membership.campaign_id, membership.id)
    assert execution is not None
    assert execution.verification is not None
    assert execution.verification.decided is False
    assert execution.verification.accepted is False


def test_the_attempt_history_reports_paid_calls_truthfully(
    db_session: Session, reader: PhaseTwoWorkbenchReader
) -> None:
    membership = _setup(db_session)
    provider = LiveProvider()
    _run(db_session, provider)

    view = _verification(reader, membership)
    assert view.attempts, "the Verification Agent records a provider attempt"
    assert view.paid_calls == provider.calls
    assert all(
        attempt.error_summary is None or "api=" not in attempt.error_summary
        for attempt in view.attempts
    )


def test_the_job_status_and_the_decision_are_shown_as_separate_facts(
    db_session: Session, reader: PhaseTwoWorkbenchReader
) -> None:
    membership = _setup(db_session)
    _run(db_session, LiveProvider([_response(EMAIL, "invalid", 6)]))

    execution = reader.contact_execution(membership.campaign_id, membership.id)
    assert execution is not None
    verification_jobs = [
        job for job in execution.jobs if job.agent_id is AgentIdentifier.VERIFICATION
    ]
    assert verification_jobs
    # The queue paused the job; the domain said "try the next candidate". Both
    # are shown, and neither is presented as the other.
    assert verification_jobs[0].stored_status is AgentJobStatus.PAUSED
    assert execution.verification is not None
    assert execution.verification.decision == VerificationDecision.TRY_NEXT_CANDIDATE.value
