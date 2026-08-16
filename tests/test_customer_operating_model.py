"""VMR Outbound is autonomous until Ready for Sending.

The customer-facing UI was built around a mental model this product does not
have. It opened on "110 things want you" under a heading that read "Decisions
only you can make", badged the navigation with the same number, listed "Needs
you" against every campaign, and presented the Review page as an operator
backlog. Every one of those numbers was assembled from internal machine state:
drafts nobody had opened, contacts eligibility rules had blocked, Agent stages
that had failed, captures whose domain lookup had not resolved.

None of it was work the customer had been given. The operating model is:

    create a campaign -> add contacts -> wait -> Ready for Sending -> send by hand

So the customer sees three words for a contact — Processing, Ready for Sending,
Could not prepare — and no queue at all. The nine-Agent pipeline stays visible as
observability, and Admin keeps every failure, retry and error class it had.

These tests are the contract. Each one names the thing it forbids, because the
easiest way for this to regress is for a well-meaning change to add a count back
somewhere and for nothing to notice.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from app.api.deps import get_db
from app.core.config import get_settings
from app.main import create_app
from app.models.campaign import CampaignContact
from app.models.email_sequence import SEQUENCE_LENGTH, EmailSequence, EmailSequenceMessageReview
from app.models.enums import (
    AgentIdentifier,
    CampaignContactEligibility,
    ContactWorkflowState,
    PipelineStageStatus,
    SequenceGenerationStatus,
)
from app.models.pipeline import CampaignContactAgentState
from app.services import customer_status
from app.services.agents.registry import AGENT_SPECS, PIPELINE_ORDER
from app.services.personalization import cadence as cadence_service
from app.web.v2 import shell
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tests.test_email_sequence import BODIES, build
from tests.test_email_sequence import scenario as _scenario

scenario = _scenario

#: The ratified ladder, restated here so a cadence change has to face this file
#: too. Asserted against the constant *and* against a persisted sequence.
ELAPSED_DAYS: tuple[int, ...] = (0, 3, 7, 12, 18, 25, 35)

#: Phrases the customer surfaces must never carry again. Each one framed internal
#: machine work as something the customer owed the system.
FORBIDDEN_TASK_LANGUAGE: tuple[str, ...] = (
    "things want you",
    "thing want you",
    "Decisions only you can make",
    "Needs you",
    "Needs a decision from you",
    "Nothing needs you here",
    "waiting for you",
    "Waiting for you",
    "wants you",
)


def _client(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    *,
    sequences: bool = True,
) -> TestClient:
    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__AGENT_WORKBENCH", "true")
    if sequences:
        monkeypatch.setenv("FEATURES__EMAIL_SEQUENCES", "true")
    else:
        monkeypatch.delenv("FEATURES__EMAIL_SEQUENCES", raising=False)
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


def _membership(scenario: tuple[Any, ...]) -> CampaignContact:
    return scenario[3]


def _campaign_url(scenario: tuple[Any, ...]) -> str:
    return f"/app/campaigns/{_membership(scenario).campaign_id}"


def _contact_url(scenario: tuple[Any, ...]) -> str:
    membership = _membership(scenario)
    return f"/app/people/{scenario[2].id}?campaign={membership.campaign_id}"


def _desk_url(scenario: tuple[Any, ...], email: int) -> str:
    """The inline sending desk on Campaign Overview, open on one of the seven emails."""

    membership = _membership(scenario)
    return f"{_campaign_url(scenario)}?section=all&person={membership.id}&email={email}"


def _walk_to_personalization(db: Session, membership: CampaignContact) -> None:
    """Put a membership where a real one stands once its sequence is written.

    Every Agent up to and including Personalization completed, and the contact
    parked at Sending — which is exactly where the orchestrator leaves it, since
    Sending has no adapter and is disabled by default.
    """

    for agent_id in PIPELINE_ORDER:
        if agent_id is AgentIdentifier.SENDING:
            break
        db.add(
            CampaignContactAgentState(
                campaign_contact_id=membership.id,
                agent_id=agent_id,
                status=PipelineStageStatus.COMPLETED,
            )
        )
    membership.current_stage = AgentIdentifier.SENDING
    membership.next_stage = AgentIdentifier.SENDING
    membership.latest_completed_stage = AgentIdentifier.PERSONALIZATION
    membership.pipeline_status = PipelineStageStatus.DISABLED
    membership.eligibility_status = CampaignContactEligibility.ELIGIBLE
    db.flush()


def _ready(db: Session, scenario: tuple[Any, ...]) -> EmailSequence:
    """A contact with the complete, valid outbound package and nothing else."""

    sequence = build(db, scenario)
    _walk_to_personalization(db, _membership(scenario))
    return sequence


# ===========================================================================
# 1-2. The Today page carries no aggregate customer task number
# ===========================================================================


def test_today_has_no_generic_things_want_you_badge_or_card(
    client: TestClient, db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """Requirement 1. The aggregate is gone, not renamed.

    Asserted against a database that would previously have produced a non-zero
    total: a contact that is blocked *and* whose stage failed, which is also the
    double-count the old model could not avoid.
    """

    membership = _membership(scenario)
    membership.eligibility_status = CampaignContactEligibility.BLOCKED
    membership.pipeline_status = PipelineStageStatus.BLOCKED
    db_session.flush()

    body = client.get("/app").text
    for phrase in FORBIDDEN_TASK_LANGUAGE:
        assert phrase not in body, phrase
    assert "v2-nav-badge" not in body
    # The vocabulary that replaced it.
    assert "Campaigns in motion" in body
    assert "Ready for Sending" in body


def test_internal_failed_or_blocked_agent_state_creates_no_customer_task_count(
    client: TestClient, db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """Requirement 2. A machine failure is not a customer obligation.

    The strongest form of the claim: the shell module can no longer *compute* a
    customer task total, so a template cannot accidentally render one.
    """

    membership = _membership(scenario)
    membership.pipeline_status = PipelineStageStatus.FAILED
    db_session.add(
        CampaignContactAgentState(
            campaign_contact_id=membership.id,
            agent_id=AgentIdentifier.RESEARCH,
            status=PipelineStageStatus.FAILED,
            retryable=False,
            reason_code="provider_unavailable",
        )
    )
    db_session.flush()

    assert not hasattr(shell, "attention_counts")
    assert not hasattr(shell, "AttentionCounts")

    body = client.get("/app").text
    for phrase in FORBIDDEN_TASK_LANGUAGE:
        assert phrase not in body, phrase
    # It is still counted — as a fact about the contact, in the customer's words.
    assert "Could not prepare" in body


def test_customer_nav_carries_no_badge_of_any_kind(
    client: TestClient, db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """Requirement 13. Structural, not textual.

    ``nav_groups`` takes no argument at all now, so there is nothing a badge could
    be computed from, and ``NavItem`` has no field to hold one.
    """

    membership = _membership(scenario)
    membership.eligibility_status = CampaignContactEligibility.BLOCKED
    db_session.flush()

    for item in shell.primary_nav():
        assert not hasattr(item, "badge")
        assert not hasattr(item, "badge_tone")

    for path in ("/app", "/app/campaigns", "/app/people", "/app/library"):
        assert "v2-nav-badge" not in client.get(path).text, path


def test_emails_and_review_are_not_destinations(client: TestClient) -> None:
    """Requirement 13, second half: emails are Campaign output, not a place.

    The customer navigation is exactly Today · Campaigns · People · Library. A
    legacy Emails/Review link resolves back into Campaigns.
    """

    keys = [item.key for item in shell.primary_nav()]
    assert keys == ["today", "campaigns", "people", "library"]

    response = client.get("/app/review", follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"].startswith("/app/campaigns")


# ===========================================================================
# 3. The campaign page does not call stopped stages "Needs you"
# ===========================================================================


def test_the_campaign_page_does_not_call_stopped_stages_needs_you(
    client: TestClient, db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """Requirement 3."""

    membership = _membership(scenario)
    membership.pipeline_status = PipelineStageStatus.FAILED
    db_session.add(
        CampaignContactAgentState(
            campaign_contact_id=membership.id,
            agent_id=AgentIdentifier.EMAIL,
            status=PipelineStageStatus.FAILED,
            retryable=True,
            reason_code="provider_timeout",
        )
    )
    db_session.flush()

    body = client.get(_campaign_url(scenario)).text
    for phrase in FORBIDDEN_TASK_LANGUAGE:
        assert phrase not in body, phrase
    assert "Where people stand" in body
    assert "Could not prepare" in body


def test_the_campaigns_list_reports_progress_rather_than_arrears(
    client: TestClient, db_session: Session, scenario: tuple[Any, ...]
) -> None:
    body = client.get("/app/campaigns").text
    for phrase in FORBIDDEN_TASK_LANGUAGE:
        assert phrase not in body, phrase
    assert "Ready for Sending" in body
    assert "Could not prepare" in body


# ===========================================================================
# 4-6. The three-word customer vocabulary
# ===========================================================================


def test_a_processing_contact_renders_as_processing(
    client: TestClient, db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """Requirement 4."""

    membership = _membership(scenario)
    membership.current_stage = AgentIdentifier.RESEARCH
    membership.next_stage = AgentIdentifier.RESEARCH
    membership.pipeline_status = PipelineStageStatus.RUNNING
    db_session.flush()

    assert (
        customer_status.status_for_membership(db_session, campaign_contact_id=membership.id)
        is customer_status.CustomerContactStatus.PROCESSING
    )
    assert "Processing" in client.get(_campaign_url(scenario)).text
    assert "Processing" in client.get(_contact_url(scenario)).text


def test_a_contact_with_the_complete_package_renders_as_ready_for_sending(
    client: TestClient, db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """Requirement 5. The package is the seven messages, not a stage flag."""

    _ready(db_session, scenario)
    membership = _membership(scenario)

    assert (
        customer_status.status_for_membership(db_session, campaign_contact_id=membership.id)
        is customer_status.CustomerContactStatus.READY_FOR_SENDING
    )
    progress = customer_status.progress(db_session, campaign_id=membership.campaign_id)
    assert progress.ready_for_sending == 1
    assert progress.processing == 0
    assert progress.could_not_prepare == 0
    assert "Ready for Sending" in client.get(_campaign_url(scenario)).text
    assert "Ready for Sending" in client.get(_contact_url(scenario)).text


def test_a_skipped_personalization_stage_is_not_mistaken_for_a_package(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """The hole the artifact test closes.

    Personalization is skippable. A campaign with it switched off steps over the
    stage, and every contact then reports the chain complete while no message
    exists anywhere. Reading stage flags alone would call that Ready for Sending.
    """

    membership = _membership(scenario)
    for agent_id in PIPELINE_ORDER:
        if agent_id is AgentIdentifier.SENDING:
            break
        db_session.add(
            CampaignContactAgentState(
                campaign_contact_id=membership.id,
                agent_id=agent_id,
                status=(
                    PipelineStageStatus.SKIPPED
                    if agent_id is AgentIdentifier.PERSONALIZATION
                    else PipelineStageStatus.COMPLETED
                ),
            )
        )
    membership.next_stage = None
    membership.pipeline_status = PipelineStageStatus.COMPLETED
    db_session.flush()

    assert db_session.scalar(select(func.count(EmailSequence.id))) == 0
    assert (
        customer_status.status_for_membership(db_session, campaign_contact_id=membership.id)
        is customer_status.CustomerContactStatus.COULD_NOT_PREPARE
    )


def test_a_terminally_unpreparable_contact_renders_as_status_not_as_an_action(
    client: TestClient, db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """Requirement 6. Visible, explained, and carrying no obligation."""

    membership = _membership(scenario)
    membership.state = ContactWorkflowState.SUPPRESSED
    membership.eligibility_status = CampaignContactEligibility.BLOCKED
    membership.pipeline_status = PipelineStageStatus.BLOCKED
    membership.blocking_reasons = [
        {
            "code": "suppression",
            "detail": "the suppression ledger blocks this identity",
            "terminal": True,
        }
    ]
    db_session.flush()

    assert (
        customer_status.status_for_membership(db_session, campaign_contact_id=membership.id)
        is customer_status.CustomerContactStatus.COULD_NOT_PREPARE
    )
    body = client.get(_campaign_url(scenario)).text
    assert "Could not prepare" in body
    for phrase in FORBIDDEN_TASK_LANGUAGE:
        assert phrase not in body, phrase
    # The status carries no call to action of its own.
    assert "See what is holding them" not in body
    assert "Open the pipeline" not in body


def test_a_suppressed_contact_that_already_has_messages_is_still_not_ready(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """Policy outranks the artifact.

    A written sequence is not permission to contact somebody the suppression
    ledger blocks, so the order of the rules is load-bearing.
    """

    _ready(db_session, scenario)
    membership = _membership(scenario)
    membership.state = ContactWorkflowState.SUPPRESSED
    membership.eligibility_status = CampaignContactEligibility.BLOCKED
    db_session.flush()

    assert (
        customer_status.status_for_membership(db_session, campaign_contact_id=membership.id)
        is customer_status.CustomerContactStatus.COULD_NOT_PREPARE
    )


def test_the_customer_vocabulary_is_exactly_three_words() -> None:
    assert [status.value for status in customer_status.CustomerContactStatus] == [
        "processing",
        "ready_for_sending",
        "could_not_prepare",
    ]
    assert set(customer_status.STATUS_LABELS.values()) == {
        "Processing",
        "Ready for Sending",
        "Could not prepare",
    }


# ===========================================================================
# 7-9. Approval is not a prerequisite, and its absence is not a backlog
# ===========================================================================


def test_a_valid_seven_message_sequence_is_ready_without_any_human_approval(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """Requirement 7. Zero review rows, and Ready for Sending anyway."""

    sequence = _ready(db_session, scenario)
    membership = _membership(scenario)

    assert db_session.scalar(select(func.count(EmailSequenceMessageReview.id))) == 0
    assert sequence.generation_status is SequenceGenerationStatus.COMPLETE
    assert (
        customer_status.status_for_membership(db_session, campaign_contact_id=membership.id)
        is customer_status.CustomerContactStatus.READY_FOR_SENDING
    )


def test_the_absence_of_a_review_row_creates_no_customer_backlog(
    client: TestClient, db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """Requirement 8."""

    _ready(db_session, scenario)
    assert db_session.scalar(select(func.count(EmailSequenceMessageReview.id))) == 0

    for path in ("/app", _campaign_url(scenario), "/app/review", _contact_url(scenario)):
        body = client.get(path).text
        for phrase in FORBIDDEN_TASK_LANGUAGE:
            assert phrase not in body, f"{phrase} on {path}"


def test_optional_inspect_and_edit_remain_available(
    client: TestClient, db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """Requirement 9. Removing the obligation must not remove the capability.

    The seven emails are read and edited on the inline sending desk of Campaign
    Overview, which the person page points into; the desk shows one email at a
    time, so every one of the seven is opened here.
    """

    _ready(db_session, scenario)
    membership = _membership(scenario)

    contact = client.get(_contact_url(scenario))
    assert contact.status_code == 200
    assert "Open in Campaign" in contact.text
    for position in range(1, SEQUENCE_LENGTH + 1):
        desk = client.get(_desk_url(scenario, position))
        assert desk.status_code == 200
        # The body is readable in full ...
        assert f'id="desk-body-{position}"' in desk.text
        assert BODIES[position - 1] in desk.text
        # ... and the edit path is still offered, for this exact email.
        assert (
            f"/app/campaigns/{membership.campaign_id}/desk/{membership.id}/{position}/edit"
            in desk.text
        )


# ===========================================================================
# 10. Admin keeps every diagnostic the customer lost
# ===========================================================================


def test_admin_diagnostics_still_expose_failed_and_blocked_execution_state(
    client: TestClient, db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """Requirement 10. The simplification is customer-side only."""

    membership = _membership(scenario)
    membership.pipeline_status = PipelineStageStatus.FAILED
    db_session.add(
        CampaignContactAgentState(
            campaign_contact_id=membership.id,
            agent_id=AgentIdentifier.RESEARCH,
            status=PipelineStageStatus.FAILED,
            retryable=True,
            reason_code="provider_timeout",
            reason_detail="The research provider did not answer.",
        )
    )
    db_session.flush()

    failures = client.get("/admin/failures")
    assert failures.status_code == 200
    assert "failure" in failures.text.lower()

    overview = client.get("/admin")
    assert overview.status_code == 200
    assert "attention" in overview.text.lower()


def test_the_nine_agent_pipeline_remains_visible_as_observability(
    client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """Visible, and explicitly not the customer's to operate."""

    customer = client.get(_campaign_url(scenario)).text
    assert "Agent" not in customer.split("<main")[1].split("Recent activity")[0]

    campaign_id = _membership(scenario).campaign_id
    diagnostics = client.get(f"/app/admin/campaigns/{campaign_id}/diagnostics")
    assert diagnostics.status_code == 200
    for agent_id in PIPELINE_ORDER:
        assert AGENT_SPECS[agent_id].display_name in diagnostics.text


# ===========================================================================
# 11-12. Nothing sends itself, and the cadence is untouched
# ===========================================================================


def test_no_automatic_send_action_is_introduced(
    client: TestClient, db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """Requirement 11."""

    _ready(db_session, scenario)
    routes = {
        route.path for route in create_app().routes if getattr(route, "path", "").startswith("/app")
    }
    assert not [path for path in routes if path.endswith("/send")]

    for position in range(1, SEQUENCE_LENGTH + 1):
        body = client.get(_desk_url(scenario, position)).text
        assert "Nothing here sends" in body
        for claim in ("has been sent", "will be sent", "scheduled to send"):
            assert claim not in body

    today = client.get("/app").text
    for claim in ("has been sent", "will be sent", "scheduled to send"):
        assert claim not in today


def test_the_seven_message_cadence_is_unchanged(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """Requirement 12. Both the constant and what actually got written."""

    assert cadence_service.DEFAULT_ELAPSED_DAYS == ELAPSED_DAYS

    sequence = build(db_session, scenario)
    from app.services.sequences import read as sequence_read

    rows = sequence_read.message_rows(db_session, sequence=sequence)
    assert len(rows) == SEQUENCE_LENGTH
    assert tuple(row.recommended_elapsed_day for row in rows) == ELAPSED_DAYS


# ===========================================================================
# 14. A genuine customer requirement is distinct from a machine failure
# ===========================================================================


def test_a_genuine_setup_condition_is_kept_separate_from_a_machine_failure(
    client: TestClient, db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """Requirement 14.

    "You have not resumed this campaign" is the customer's. "The Research Agent
    failed" is not. They render in different cards, under different headings, and
    the setup card says so in as many words.
    """

    campaign = scenario[0]
    campaign.execution_enabled = False
    membership = _membership(scenario)
    membership.pipeline_status = PipelineStageStatus.FAILED
    db_session.add(
        CampaignContactAgentState(
            campaign_contact_id=membership.id,
            agent_id=AgentIdentifier.RESEARCH,
            status=PipelineStageStatus.FAILED,
            retryable=False,
        )
    )
    db_session.flush()

    body = client.get(_campaign_url(scenario)).text
    # The customer-owned condition is said in the customer's words: paused and
    # resumable, or held by an administrator setting when an Agent is off.
    assert (
        "Paused. Resume the Campaign" in body
        or "Preparation is being held by an administrator setting." in body
    )
    assert "Research Agent" not in body
    # And the machine outcome is reported separately, as status.
    assert "Where people stand" in body
    assert "Could not prepare" in body


def test_a_configured_running_campaign_shows_no_setup_card_at_all(
    client: TestClient, db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """No manufactured urgency: a campaign with nothing to fix says nothing."""

    campaign = scenario[0]
    campaign.execution_enabled = True
    _ready(db_session, scenario)

    body = client.get(_campaign_url(scenario)).text
    assert "Resume Campaign" not in body
    assert "Start Campaign" not in body


# ===========================================================================
# Projection hygiene
# ===========================================================================


def test_one_contact_is_counted_exactly_once(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """The double-count the old model could not avoid.

    A contact whose eligibility is blocked *and* whose pipeline status is blocked
    *and* which holds an unrecoverable stage failure contributed three separate
    entries to the old total. It is one contact, and it is counted once.
    """

    membership = _membership(scenario)
    membership.eligibility_status = CampaignContactEligibility.BLOCKED
    membership.pipeline_status = PipelineStageStatus.BLOCKED
    db_session.add(
        CampaignContactAgentState(
            campaign_contact_id=membership.id,
            agent_id=AgentIdentifier.COMPANY,
            status=PipelineStageStatus.FAILED,
            retryable=False,
        )
    )
    db_session.flush()

    progress = customer_status.progress(db_session, campaign_id=membership.campaign_id)
    assert progress.total == 1
    assert progress.could_not_prepare == 1
    assert progress.processing == 0
    assert progress.ready_for_sending == 0


def test_the_projection_writes_nothing(db_session: Session, scenario: tuple[Any, ...]) -> None:
    """A page render must not move the state machine it is describing."""

    _ready(db_session, scenario)
    membership = _membership(scenario)
    before = (
        membership.pipeline_status,
        membership.eligibility_status,
        membership.current_stage,
        membership.next_stage,
    )
    stage_rows = db_session.scalar(select(func.count(CampaignContactAgentState.id)))

    customer_status.progress(db_session, campaign_id=membership.campaign_id)
    customer_status.statuses_for_campaign(db_session, campaign_id=membership.campaign_id)
    customer_status.status_for_membership(db_session, campaign_contact_id=membership.id)

    db_session.refresh(membership)
    assert (
        membership.pipeline_status,
        membership.eligibility_status,
        membership.current_stage,
        membership.next_stage,
    ) == before
    assert db_session.scalar(select(func.count(CampaignContactAgentState.id))) == stage_rows


def test_an_unknown_membership_has_no_status(db_session: Session) -> None:
    assert (
        customer_status.status_for_membership(db_session, campaign_contact_id=uuid.uuid4()) is None
    )
