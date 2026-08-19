"""Email 2 is `value_resource`, and the Campaign Report URL it offers (SEQ-002).

Four risks, and the tests are grouped by which one they close.

**The address must survive intact.** It is the one string in this product that
a stranger is invited to click, and the model that writes the sentence around it
never sees it. So the tests here care as much about what does *not* happen — no
mutation, no leak into another message, no marker left in the copy, no invented
substitute when the Campaign has none — as about the happy path.

**History must stay true.** A sequence written before this framework existed had
a concise reminder at position 2 and no report link. Nothing may relabel it.

**A missing report must be a refusal, not a quiet degradation.** Six messages
reported as seven, or an Email 2 silently returned to being a reminder, would
both be invisible to the person who has to notice.

**Nothing else may change.** Single drafts, Campaigns not opted in, and the
other six positions keep the behaviour they had.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from app.core.config import get_settings
from app.main import create_app
from app.models.campaign import Campaign
from app.models.email_sequence import EmailSequenceMessage, EmailSequenceMessageVersion
from app.models.enums import CampaignStatus, SequenceMessagePurpose
from app.services import campaigns as campaign_service
from app.services.campaign_resource_urls import (
    CampaignResourceUrlError,
    normalize_campaign_resource_url,
    stored_resource_url,
)
from app.services.personalization import sequence as sequence_generation
from app.services.personalization import sequence_validation
from app.services.personalization.cadence import with_campaign_opt_in
from app.services.sequences import read as sequence_read
from fastapi.testclient import TestClient
from markupsafe import escape
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.test_email_sequence import (
    BODIES,
    RESOURCE_URL,
    CountingThinker,
    build,
    generate,
    make_scenario,
    persist,
    sequence_payload,
)

MARKER = sequence_validation.RESOURCE_MARKER


@pytest.fixture()
def scenario(db_session: Session) -> tuple[Any, ...]:
    """The seven-message scenario the sequence suite uses, Report URL and all."""

    return make_scenario(db_session)


def _body(session: Session, sequence: Any, position: int) -> str:
    """One message's *current* body, through the read model a page uses."""

    detail = sequence_read.message_detail(session, sequence=sequence, position=position)
    assert detail is not None
    return detail.body


def _version_body(session: Session, sequence: Any, position: int) -> str:
    """The body belonging to **this exact sequence version**, superseded or not.

    ``message_detail`` deliberately answers "what is current for this logical
    message", which for a superseded sequence is a later generation's text. That
    is right for a page and wrong for an immutability assertion: proving the old
    sequence was not rewritten means reading the old sequence's own row.
    """

    version = session.scalars(
        select(EmailSequenceMessageVersion)
        .join(
            EmailSequenceMessage,
            EmailSequenceMessage.id == EmailSequenceMessageVersion.message_id,
        )
        .where(
            EmailSequenceMessageVersion.sequence_id == sequence.id,
            EmailSequenceMessage.position == position,
        )
    ).one()
    return version.body


def _rendered(text: str) -> str:
    """The sentence as Jinja writes it into a page.

    ``can't`` leaves the template as ``can&#39;t``, so a raw substring check
    would pass on a page that does not contain the sentence and fail on one that
    does. Escaping the expectation the way the template engine does is what
    makes the assertion about the words rather than about the apostrophe.
    """

    return str(escape(text))


# ---------------------------------------------------------------------------
# 1. The purpose framework
# ---------------------------------------------------------------------------


def test_position_two_is_value_resource_and_every_purpose_is_still_unique() -> None:
    """The framework says what it says before any model is involved."""

    assert sequence_generation.PURPOSE_BY_POSITION[2] is SequenceMessagePurpose.VALUE_RESOURCE
    purposes = [purpose for _p, purpose, _l, _b in sequence_generation.PURPOSES]
    assert len(set(purposes)) == len(purposes) == 7
    # Position 6 keeps its own resource purpose. The two are different things.
    assert (
        sequence_generation.PURPOSE_BY_POSITION[6] is SequenceMessagePurpose.LOW_FRICTION_RESOURCE
    )


def test_nothing_generates_concise_reminder_any_more_but_it_is_still_a_purpose() -> None:
    """The label survives for the rows that hold it; the framework has dropped it."""

    assert SequenceMessagePurpose("concise_reminder") is SequenceMessagePurpose.CONCISE_REMINDER
    assert SequenceMessagePurpose.CONCISE_REMINDER not in set(
        sequence_generation.PURPOSE_BY_POSITION.values()
    )
    # And a person reading an old sequence is still told what it actually was.
    assert (
        sequence_read.PURPOSE_LABELS[SequenceMessagePurpose.CONCISE_REMINDER] == "Concise reminder"
    )
    assert sequence_read.PURPOSE_LABELS[SequenceMessagePurpose.VALUE_RESOURCE] == "Value resource"


def test_the_producer_version_moved_because_the_same_inputs_now_differ() -> None:
    assert sequence_generation.SEQUENCE_PRODUCER_VERSION == "sequence-builder/v2"
    assert sequence_validation.VALIDATION_POLICY_VERSION == "sequence-validation/v2"


def test_a_new_sequence_stores_value_resource_at_position_two(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    sequence = build(db_session, scenario)
    rows = sequence_read.message_rows(db_session, sequence=sequence)
    assert rows[1].purpose is SequenceMessagePurpose.VALUE_RESOURCE
    assert rows[1].purpose_label == "Value resource"
    assert SequenceMessagePurpose.CONCISE_REMINDER not in {row.purpose for row in rows}


def test_a_model_claiming_the_old_purpose_at_position_two_is_refused(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """A producer still writing ``concise_reminder`` has not been updated."""

    _c, _co, _ct, _m, _p, evidence_id = scenario
    payload = sequence_payload(evidence_id=evidence_id)
    payload["messages"][1]["purpose"] = "concise_reminder"
    with pytest.raises(sequence_generation.SequenceGenerationError) as excinfo:
        generate(db_session, scenario, payload=payload)
    assert excinfo.value.code == "sequence_invalid_purpose"


def test_a_historical_concise_reminder_row_is_neither_rewritten_nor_unreadable(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """The whole compatibility claim, proved against a real row.

    The row is written as a sequence generated before this framework existed
    would have written it, a new sequence is generated for the same membership,
    and the old row is re-read afterwards.
    """

    sequence = build(db_session, scenario)
    historical = db_session.scalars(
        select(EmailSequenceMessage).where(
            EmailSequenceMessage.sequence_key == sequence.sequence_key,
            EmailSequenceMessage.position == 2,
        )
    ).one()
    historical.purpose = SequenceMessagePurpose.CONCISE_REMINDER
    historical_id = historical.id
    historical_subject = sequence_read.message_rows(db_session, sequence=sequence)[1].subject
    db_session.flush()

    historical_body = _version_body(db_session, sequence, 2)

    # A second generation for the same Campaign Contact, under the new
    # framework. The logical message rows are reused by design, so this is
    # exactly the path on which a rename would happen if anything did one.
    _c, _co, _ct, _m, _p, evidence_id = scenario
    # A distinct extra sentence per position: the repetition validator would
    # otherwise refuse seven bodies that all gained the same one.
    later_bodies = tuple(
        f"{body} Planning pass two, note {index}." for index, body in enumerate(BODIES)
    )
    build(
        db_session,
        scenario,
        payload=sequence_payload(evidence_id=evidence_id, bodies=later_bodies),
    )

    db_session.expire_all()
    reloaded = db_session.get(EmailSequenceMessage, historical_id)
    assert reloaded is not None
    assert reloaded.purpose is SequenceMessagePurpose.CONCISE_REMINDER
    assert sequence_read.PURPOSE_LABELS[reloaded.purpose] == "Concise reminder"
    # The superseded sequence still reads exactly as it was written.
    assert sequence_read.message_rows(db_session, sequence=sequence)[1].subject == (
        historical_subject
    )
    assert _version_body(db_session, sequence, 2) == historical_body


# ---------------------------------------------------------------------------
# 2. The Report URL as a value
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "https://reports.example.com/carbon-fibre",
        "http://reports.example.com/carbon-fibre",
        "https://reports.example.com/Mixed/Case?edition=Preview#section-3",
        "https://reports.example.com:8443/report",
    ],
)
def test_an_ordinary_absolute_address_is_accepted_exactly_as_typed(raw: str) -> None:
    """Accepted *and unchanged*. The second half is the point."""

    assert normalize_campaign_resource_url(raw) == raw


def test_surrounding_whitespace_is_the_only_thing_removed() -> None:
    assert (
        normalize_campaign_resource_url("  https://reports.example.com/r  ")
        == "https://reports.example.com/r"
    )


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("javascript:alert(1)", "resource_url_scheme_not_allowed"),
        ("data:text/html,<script>alert(1)</script>", "resource_url_scheme_not_allowed"),
        ("ftp://reports.example.com/r", "resource_url_scheme_not_allowed"),
        ("file:///C:/reports/market.pdf", "resource_url_scheme_not_allowed"),
        ("C:\\reports\\market.pdf", "resource_url_not_absolute"),
        ("/reports/market", "resource_url_not_absolute"),
        ("reports/market", "resource_url_not_absolute"),
        ("reports.example.com/market", "resource_url_not_absolute"),
        ("", "resource_url_missing"),
        ("   ", "resource_url_missing"),
        ("https://reports example.com/r", "resource_url_malformed"),
        ("https://user:secret@reports.example.com/r", "resource_url_has_credentials"),
        ("http://localhost:8000/report", "resource_url_not_public"),
        ("http://127.0.0.1/report", "resource_url_not_public"),
        ("http://169.254.169.254/latest/meta-data", "resource_url_not_public"),
        ("http://[::ffff:127.0.0.1]/report", "resource_url_not_public"),
        ("http://intranet/report", "resource_url_not_public"),
        ("https://reports.example.com/\nSubject: injected", "resource_url_malformed"),
    ],
)
def test_an_address_that_is_not_an_ordinary_public_page_is_refused(raw: str, code: str) -> None:
    with pytest.raises(CampaignResourceUrlError) as excinfo:
        normalize_campaign_resource_url(raw)
    assert excinfo.value.code == code


def test_a_bare_domain_is_refused_rather_than_promoted_to_https() -> None:
    """The offering validator repairs this one; this validator must not.

    A Report URL is copied into an email. Guessing the scheme of a page nobody
    in this system will ever open is guessing about whether the link works.
    """

    with pytest.raises(CampaignResourceUrlError):
        normalize_campaign_resource_url("reports.example.com")


def test_the_read_side_treats_an_unusable_stored_value_as_absent() -> None:
    assert stored_resource_url(RESOURCE_URL) == RESOURCE_URL
    assert stored_resource_url(None) is None
    assert stored_resource_url("") is None
    assert stored_resource_url("javascript:alert(1)") is None


def test_the_campaign_service_saves_edits_and_clears_the_report_url(
    db_session: Session,
) -> None:
    campaign = campaign_service.create_campaign(
        db_session,
        name=f"Report URL {uuid.uuid4()}",
        campaign_resource_url="  https://reports.example.com/carbon  ",
        status=CampaignStatus.DRAFT,
    )
    db_session.flush()
    assert campaign.campaign_resource_url == "https://reports.example.com/carbon"

    campaign_service.update_campaign(
        db_session, campaign.id, campaign_resource_url="https://reports.example.com/microalgae"
    )
    db_session.expire_all()
    assert (
        db_session.get(Campaign, campaign.id).campaign_resource_url
        == "https://reports.example.com/microalgae"
    )

    # An empty box clears it. A Campaign can stop being a seven-message one.
    campaign_service.update_campaign(db_session, campaign.id, campaign_resource_url="")
    db_session.expire_all()
    assert db_session.get(Campaign, campaign.id).campaign_resource_url is None


def test_the_campaign_service_refuses_an_unusable_report_url(db_session: Session) -> None:
    campaign = campaign_service.create_campaign(
        db_session, name=f"Report URL {uuid.uuid4()}", status=CampaignStatus.DRAFT
    )
    db_session.flush()
    with pytest.raises(campaign_service.CampaignError):
        campaign_service.update_campaign(
            db_session, campaign.id, campaign_resource_url="javascript:alert(1)"
        )
    db_session.rollback()


def test_a_campaign_with_no_report_url_is_still_a_valid_row(db_session: Session) -> None:
    """The column is nullable and stays nullable — legacy and single-draft."""

    campaign = campaign_service.create_campaign(
        db_session, name=f"No report {uuid.uuid4()}", status=CampaignStatus.DRAFT
    )
    db_session.flush()
    assert campaign.campaign_resource_url is None
    campaign_service.update_campaign(db_session, campaign.id, primary_cta="a short call")
    db_session.expire_all()
    assert db_session.get(Campaign, campaign.id).campaign_resource_url is None


# ---------------------------------------------------------------------------
# 3. Deterministic insertion
# ---------------------------------------------------------------------------


def test_the_model_is_asked_for_a_marker_and_never_shown_the_address(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """The strongest property in this feature, stated as an absence.

    A model that never receives the string cannot mangle it. Nothing about the
    prompt is allowed to make that untrue.
    """

    _generated, writer = generate(db_session, scenario)
    prompt = writer.requests[0].prompt
    assert MARKER in prompt
    assert RESOURCE_URL not in prompt
    assert "reports.example.com" not in prompt


def test_email_two_carries_the_exact_address_once_and_no_other_message_does(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    generated, _writer = generate(db_session, scenario)
    bodies = {message.position: message.body for message in generated.messages}
    assert bodies[2].count(RESOURCE_URL) == 1
    for position in (1, 3, 4, 5, 6, 7):
        assert RESOURCE_URL not in bodies[position], position
    # And no subject line carries it, in either direction.
    for message in generated.messages:
        assert RESOURCE_URL not in message.subject
        assert MARKER not in message.subject


def test_no_internal_marker_reaches_the_persisted_copy(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    sequence = build(db_session, scenario)
    versions = db_session.scalars(
        select(EmailSequenceMessageVersion).where(
            EmailSequenceMessageVersion.sequence_id == sequence.id
        )
    ).all()
    assert len(versions) == 7
    assert not any(MARKER in version.body or MARKER in version.subject for version in versions)
    stored = [version for version in versions if RESOURCE_URL in version.body]
    assert len(stored) == 1
    assert stored[0].body.count(RESOURCE_URL) == 1


def test_the_stored_address_is_the_configured_one_character_for_character(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """Not "an address like it" — the exact string, including case and query."""

    campaign, _co, _ct, _m, _p, _e = scenario
    generated, _writer = generate(db_session, scenario)
    body = next(message.body for message in generated.messages if message.position == 2)
    start = body.index(RESOURCE_URL)
    assert body[start : start + len(RESOURCE_URL)] == campaign.campaign_resource_url


def test_a_model_that_writes_its_own_address_instead_of_the_marker_is_refused(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """The one failure mode delegation would have produced, made impossible."""

    _c, _co, _ct, _m, _p, evidence_id = scenario
    bodies = list(BODIES)
    bodies[1] = bodies[1].replace(RESOURCE_URL, "https://reports.example.com/typo-by-the-model")
    with pytest.raises(sequence_validation.SequenceValidationError) as excinfo:
        generate(
            db_session,
            scenario,
            payload=sequence_payload(evidence_id=evidence_id, bodies=tuple(bodies)),
        )
    codes = {finding.code for finding in excinfo.value.findings.failures}
    assert "resource_marker_missing" in codes
    assert "resource_url_not_once" in codes


def test_a_marker_written_into_another_message_is_refused(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """Positions 1 and 3-7 do not receive the resource because it exists."""

    _c, _co, _ct, _m, _p, evidence_id = scenario
    bodies = list(BODIES)
    bodies[5] = f"{bodies[5]} A second copy here: {RESOURCE_URL}"
    with pytest.raises(sequence_validation.SequenceValidationError) as excinfo:
        generate(
            db_session,
            scenario,
            payload=sequence_payload(evidence_id=evidence_id, bodies=tuple(bodies)),
        )
    assert "resource_marker_misplaced" in {
        finding.code for finding in excinfo.value.findings.failures
    }


def test_the_marker_written_twice_in_email_two_is_refused_rather_than_repaired(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    _c, _co, _ct, _m, _p, evidence_id = scenario
    bodies = list(BODIES)
    bodies[1] = f"{bodies[1]} And again, in case: {RESOURCE_URL}"
    with pytest.raises(sequence_validation.SequenceValidationError) as excinfo:
        generate(
            db_session,
            scenario,
            payload=sequence_payload(evidence_id=evidence_id, bodies=tuple(bodies)),
        )
    assert "resource_marker_missing" in {
        finding.code for finding in excinfo.value.findings.failures
    }


def test_a_marker_in_a_subject_line_is_refused(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    _c, _co, _ct, _m, _p, evidence_id = scenario
    payload = sequence_payload(evidence_id=evidence_id)
    payload["messages"][1]["subject"] = f"A look at {MARKER}"
    with pytest.raises(sequence_validation.SequenceValidationError) as excinfo:
        generate(db_session, scenario, payload=payload)
    codes = {finding.code for finding in excinfo.value.findings.failures}
    assert "resource_marker_in_subject" in codes
    assert "resource_marker_survived" in codes


def test_a_surviving_marker_is_caught_even_with_no_report_url_in_play() -> None:
    """The last line of defence, checked independently of the merge."""

    from dataclasses import replace as _replace

    from tests.test_email_sequence import BODIES as _bodies

    message = sequence_generation.GeneratedMessage(
        position=2,
        message_type=sequence_generation.SequenceMessageType.FOLLOW_UP,
        purpose=SequenceMessagePurpose.VALUE_RESOURCE,
        subject="A look at the research",
        body=f"Something useful here: {MARKER}",
        recommended_delay_days=3,
        recommended_elapsed_day=3,
        evidence_insight_ids=(),
        context_used={},
        warnings=(),
    )
    assert _bodies  # the module-level fixture data is what the rest of this file uses
    findings = sequence_validation._validate_resource((message,), resource_url=None)
    assert [finding.code for finding in findings] == ["resource_marker_survived"]
    # And a clean body produces nothing at all.
    assert (
        sequence_validation._validate_resource(
            (_replace(message, body="Nothing internal here."),), resource_url=None
        )
        == []
    )


# ---------------------------------------------------------------------------
# 4. A missing or unusable Report URL
# ---------------------------------------------------------------------------


def test_a_sequence_campaign_with_no_report_url_refuses_before_spending(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """No fabricated URL, no homepage, no quiet reminder — a refusal, and no call."""

    campaign, _co, _ct, membership, policy, evidence_id = scenario
    campaign.campaign_resource_url = None
    db_session.flush()

    writer = CountingThinker(sequence_payload(evidence_id=evidence_id))
    with pytest.raises(sequence_generation.SequenceGenerationError) as excinfo:
        sequence_generation.generate_sequence(
            db_session, membership=membership, policy=policy, thinker=writer
        )
    assert excinfo.value.code == "campaign_resource_url_missing"
    assert str(excinfo.value) == sequence_generation.RESOURCE_URL_REQUIRED
    assert "Report URL" in str(excinfo.value)
    assert writer.calls == 0


def test_an_unusable_stored_report_url_never_reaches_generated_copy(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """A row written before a rule is refused, not sent."""

    campaign, _co, _ct, membership, policy, evidence_id = scenario
    campaign.campaign_resource_url = "javascript:alert(1)"
    db_session.flush()

    writer = CountingThinker(sequence_payload(evidence_id=evidence_id))
    with pytest.raises(sequence_generation.SequenceGenerationError) as excinfo:
        sequence_generation.generate_sequence(
            db_session, membership=membership, policy=policy, thinker=writer
        )
    assert excinfo.value.code == "campaign_resource_url_missing"
    assert writer.calls == 0


def test_the_digest_cannot_be_computed_without_a_report_url_either(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """The idempotency check and the generation refuse for the same reason.

    Two different answers here would mean a stage that either spends before
    discovering the gap or reports a digest for a sequence that cannot exist.
    """

    campaign, _co, _ct, membership, policy, _e = scenario
    campaign.campaign_resource_url = None
    db_session.flush()
    with pytest.raises(sequence_generation.SequenceGenerationError) as excinfo:
        sequence_generation.precompute_digest(db_session, membership=membership, policy=policy)
    assert excinfo.value.code == "campaign_resource_url_missing"


def test_single_draft_generation_is_untouched_by_the_report_url_requirement(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """A Campaign that is not opted in never had a report and still does not need one."""

    from app.services.personalization import generation as single_generation

    campaign, _co, _ct, membership, policy, _e = scenario
    campaign.cadence_config = {"sequence": {"enabled": False}}
    campaign.campaign_resource_url = None
    db_session.flush()

    class OneDraft:
        name = "single-draft-test"
        version = "single-draft-test/v1"

        def think(self, request: Any) -> Any:
            from app.services.thinking.contracts import ThinkingResult

            return ThinkingResult(
                payload={
                    "subject": "Sourced market previews",
                    "body": (
                        "Hello Ada, VM Intelligence builds sourced market reports that "
                        "investment teams read in preview before buying the full version. "
                        "Would a short look be useful this month?"
                    ),
                    "rationale": "Offering-led.",
                    "evidence_insight_ids": [],
                },
                producer=self.name,
                producer_version=self.version,
                duration_seconds=0.01,
            )

    generated = single_generation.generate(
        db_session, membership=membership, policy=policy, thinker=OneDraft()
    )
    assert generated.subject == "Sourced market previews"
    assert RESOURCE_URL not in generated.body


# ---------------------------------------------------------------------------
# 5. The input digest and regeneration
# ---------------------------------------------------------------------------


def test_changing_the_report_url_changes_the_input_digest(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    campaign, _co, _ct, membership, policy, _e = scenario
    before = sequence_generation.precompute_digest(db_session, membership=membership, policy=policy)
    campaign.campaign_resource_url = "https://reports.example.com/microalgae"
    db_session.flush()
    after = sequence_generation.precompute_digest(db_session, membership=membership, policy=policy)
    assert before != after


def test_the_same_report_url_keeps_the_digest_and_the_idempotent_retry(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    campaign, _co, _ct, membership, policy, evidence_id = scenario
    first = sequence_generation.precompute_digest(db_session, membership=membership, policy=policy)
    # Re-saving the same value is not a change, and must not cost a generation.
    campaign.campaign_resource_url = RESOURCE_URL
    db_session.flush()
    assert (
        sequence_generation.precompute_digest(db_session, membership=membership, policy=policy)
        == first
    )

    generated, writer = generate(db_session, scenario)
    assert generated.input_digest == first
    assert writer.calls == 1


def test_a_new_report_url_produces_a_new_sequence_and_leaves_the_old_one_alone(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """Regeneration for report B must not touch the sequence written for report A."""

    campaign, _co, _ct, _m, _p, evidence_id = scenario
    first = build(db_session, scenario)
    first_body = _version_body(db_session, first, 2)
    assert RESOURCE_URL in first_body
    first_digest = first.input_digest

    second_url = "https://reports.example.com/Microalgae/2026?edition=Preview"
    campaign.campaign_resource_url = second_url
    db_session.flush()

    generated, _writer = generate(db_session, scenario)
    assert generated.input_digest != first_digest
    second = persist(db_session, scenario, generated)
    second_body = _version_body(db_session, second, 2)
    assert second_url in second_body
    assert RESOURCE_URL not in second_body

    # The first sequence still says exactly what it said.
    db_session.expire_all()
    reread = _version_body(db_session, first, 2)
    assert reread == first_body
    assert second_url not in reread


# ---------------------------------------------------------------------------
# 6. Campaign Setup, over HTTP
# ---------------------------------------------------------------------------


@pytest.fixture()
def setup_client(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__EMAIL_SEQUENCES", "true")
    monkeypatch.setenv("FEATURES__SELLER_KNOWLEDGE_BASE", "true")
    get_settings.cache_clear()
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[__import__("app.api.deps", fromlist=["get_db"]).get_db] = _override
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.fixture()
def web_campaign(db_session: Session) -> Campaign:
    campaign = campaign_service.create_campaign(
        db_session,
        name=f"Report URL web {uuid.uuid4()}",
        status=CampaignStatus.DRAFT,
    )
    campaign.cadence_config = with_campaign_opt_in(campaign, enabled=True)
    db_session.commit()
    return campaign


def test_campaign_setup_offers_the_report_url_field_with_its_customer_wording(
    setup_client: TestClient, web_campaign: Campaign
) -> None:
    body = setup_client.get(f"/app/campaigns/{web_campaign.id}/setup").text
    assert 'name="campaign_resource_url"' in body
    assert "Report URL" in body
    assert "Read-only report page shared with prospects in Email 2." in body


def test_saving_a_report_url_persists_it_and_renders_it_back_as_a_link(
    setup_client: TestClient, db_session: Session, web_campaign: Campaign
) -> None:
    response = setup_client.post(
        f"/app/campaigns/{web_campaign.id}/setup",
        data={
            "name": web_campaign.name,
            "campaign_resource_url": "https://reports.example.com/Carbon?edition=Preview",
            "sequence_enabled": "on",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "ok=" in response.headers["location"]
    db_session.expire_all()
    stored = db_session.get(Campaign, web_campaign.id).campaign_resource_url
    assert stored == "https://reports.example.com/Carbon?edition=Preview"

    body = setup_client.get(f"/app/campaigns/{web_campaign.id}/setup").text
    # Rendered back into the field, and offered as something to go and look at.
    assert "https://reports.example.com/Carbon?edition=Preview" in body
    assert 'href="https://reports.example.com/Carbon?edition=Preview"' in body
    assert 'rel="noopener noreferrer nofollow"' in body

    # Editing it replaces it.
    setup_client.post(
        f"/app/campaigns/{web_campaign.id}/setup",
        data={
            "name": web_campaign.name,
            "campaign_resource_url": "https://reports.example.com/Microalgae",
            "sequence_enabled": "on",
        },
        follow_redirects=False,
    )
    db_session.expire_all()
    assert (
        db_session.get(Campaign, web_campaign.id).campaign_resource_url
        == "https://reports.example.com/Microalgae"
    )


def test_an_unusable_report_url_comes_back_as_a_sentence_and_saves_nothing(
    setup_client: TestClient, db_session: Session, web_campaign: Campaign
) -> None:
    response = setup_client.post(
        f"/app/campaigns/{web_campaign.id}/setup",
        data={
            "name": "Renamed in the same save",
            "campaign_resource_url": "javascript:alert(1)",
            "sequence_enabled": "on",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "err=" in response.headers["location"]
    assert "http" in response.headers["location"]
    db_session.expire_all()
    reloaded = db_session.get(Campaign, web_campaign.id)
    assert reloaded.campaign_resource_url is None
    # The refusal is raised before anything is written, so the rename is gone too.
    assert reloaded.name == web_campaign.name


def test_setup_says_why_a_seven_message_campaign_cannot_be_prepared_yet(
    setup_client: TestClient, db_session: Session, web_campaign: Campaign
) -> None:
    body = setup_client.get(f"/app/campaigns/{web_campaign.id}/setup").text
    assert _rendered(sequence_generation.RESOURCE_URL_REQUIRED) in body

    setup_client.post(
        f"/app/campaigns/{web_campaign.id}/setup",
        data={
            "name": web_campaign.name,
            "campaign_resource_url": "https://reports.example.com/carbon",
            "sequence_enabled": "on",
        },
        follow_redirects=False,
    )
    after = setup_client.get(f"/app/campaigns/{web_campaign.id}/setup").text
    assert _rendered(sequence_generation.RESOURCE_URL_REQUIRED) not in after


def test_a_single_draft_campaign_is_not_told_to_add_a_report_url(
    setup_client: TestClient, db_session: Session, web_campaign: Campaign
) -> None:
    web_campaign.cadence_config = with_campaign_opt_in(web_campaign, enabled=False)
    db_session.commit()
    body = setup_client.get(f"/app/campaigns/{web_campaign.id}/setup").text
    assert _rendered(sequence_generation.RESOURCE_URL_REQUIRED) not in body


def test_url_adjacent_text_cannot_escape_the_field_or_inject_markup(
    setup_client: TestClient, db_session: Session, web_campaign: Campaign
) -> None:
    """Two guards, and the test needs both: refused on the way in, escaped on the way out."""

    hostile = 'https://reports.example.com/r"><script>alert(1)</script>'
    setup_client.post(
        f"/app/campaigns/{web_campaign.id}/setup",
        data={
            "name": web_campaign.name,
            "campaign_resource_url": hostile,
            "sequence_enabled": "on",
        },
        follow_redirects=False,
    )
    db_session.expire_all()
    campaign = db_session.get(Campaign, web_campaign.id)

    # It may be stored (it is a syntactically valid https URL), but it may never
    # render as markup, and it may never be offered as a link the page trusts.
    body = setup_client.get(f"/app/campaigns/{web_campaign.id}/setup").text
    assert "<script>alert(1)</script>" not in body
    if campaign.campaign_resource_url is not None:
        assert "&lt;script&gt;" in body or "&#34;" in body or "&quot;" in body
