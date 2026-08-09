"""The sequence Review queue, Contact page and Admin diagnosis, over HTTP.

Two things these tests exist to protect.

**The page must not become seven emails per contact.** The Review queue is a
list, and a list that renders seven full bodies per row is unusable at any real
volume. The assertions here are as much about what is *absent* from a collapsed
card as about what is present.

**Every empty state must say which empty it is.** "No sequence" covering
feature-off, campaign-not-opted-in, not-generated-yet and generation-failed is
how an operator ends up waiting for something that is switched off. Each state
is asserted separately and by its own words.
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
from app.models.contact import Contact
from app.models.draft import DraftVersion
from app.models.email_sequence import SEQUENCE_LENGTH, EmailSequenceMessageReview
from app.models.enums import SequenceReviewDecision, SequenceReviewState
from app.services.sequences import read as sequence_read
from app.services.sequences import review as sequence_review
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

# `scenario` is a pytest fixture defined in the sibling module. Imported under a
# private alias and re-exported once, so ruff sees one definition rather than a
# redefinition on every test that takes the fixture by name.
from tests.test_email_sequence import BODIES, SUBJECTS, build
from tests.test_email_sequence import scenario as _scenario

scenario = _scenario


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


@pytest.fixture()
def client_without_sequences(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    with _client(db_session, monkeypatch, sequences=False) as app_client:
        yield app_client
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Review queue
# ---------------------------------------------------------------------------


def test_the_review_queue_renders_one_compact_card_per_campaign_contact(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """Tests 76-79: one card, seven-message status, subject, excerpt, counts."""

    build(db_session, scenario)
    response = client.get("/app/review")
    assert response.status_code == 200
    body = response.text

    assert body.count("v2-seq-card") == 1
    assert f"of {SEQUENCE_LENGTH} messages" in body
    assert SUBJECTS[0] in body
    # Approved on arrival, with nobody's name on it. Both halves are asserted:
    # the count alone would pass whether or not the page said who approved.
    assert f"{SEQUENCE_LENGTH} approved" in body
    assert f"{SEQUENCE_LENGTH} unreviewed" in body
    assert "approved by you" not in body
    # No count on the card claims anything is queued. The lede says "nothing is
    # held up waiting for you", which is the opposite claim, so the assertion
    # targets the tally wording rather than the word itself.
    assert "waiting</span>" not in body
    assert "View sequence" in body


def test_a_collapsed_card_does_not_render_any_message_body(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """Tests 81, 87: the queue is a list, not seven emails per row."""

    build(db_session, scenario)
    body = client.get("/app/review").text

    # The excerpt of the initial message is expected; nothing else is.
    for text in BODIES[1:]:
        assert text not in body, "a collapsed card must not carry a follow-up body"
    assert BODIES[0] not in body, "even the initial body is only shown expanded"


def test_expanding_a_card_shows_the_selector_and_exactly_one_body(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """Tests 80-82: a message selector, and one message revealed by default."""

    sequence = build(db_session, scenario)
    response = client.get(f"/app/review?sequence={sequence.id}&step=1")
    assert response.status_code == 200
    body = response.text

    # The selector offers all seven, labelled Initial | F1 … F6.
    for label in ("Initial", "F1", "F2", "F3", "F4", "F5", "F6"):
        assert f">{label}<" in body
    # Exactly one body is present.
    shown = [text for text in BODIES if text in body]
    assert shown == [BODIES[0]]


def test_selecting_a_later_step_reveals_only_that_message(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """Test 82: the action targets the selected message."""

    sequence = build(db_session, scenario)
    body = client.get(f"/app/review?sequence={sequence.id}&step=5").text

    shown = [text for text in BODIES if text in body]
    assert shown == [BODIES[4]]
    rows = sequence_read.message_rows(db_session, sequence=sequence)
    assert f"/app/review/sequence/messages/{rows[4].version_id}/approve" in body
    assert f"/app/review/sequence/messages/{rows[0].version_id}/approve" not in body


def test_an_out_of_range_step_falls_back_to_the_initial_message(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """A mistyped step is a navigation slip, not a missing page."""

    sequence = build(db_session, scenario)
    for step in ("0", "99", "banana"):
        response = client.get(f"/app/review?sequence={sequence.id}&step={step}")
        assert response.status_code == 200
        assert BODIES[0] in response.text


def test_approving_one_message_over_http_records_that_exact_version(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """Tests 65, 82, 85."""

    sequence = build(db_session, scenario)
    rows = sequence_read.message_rows(db_session, sequence=sequence)
    response = client.post(
        f"/app/review/sequence/messages/{rows[2].version_id}/approve",
        data={"back": f"/app/review?sequence={sequence.id}&step=3", "reason": "reads well"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    stored = db_session.scalars(select(EmailSequenceMessageReview)).all()
    assert len(stored) == 1
    assert stored[0].message_version_id == rows[2].version_id
    assert stored[0].decision is SequenceReviewDecision.APPROVED


def test_bulk_approval_over_http_names_every_version(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """Test 66, via the form the page actually renders."""

    sequence = build(db_session, scenario)
    rows = sequence_read.message_rows(db_session, sequence=sequence)
    page = client.get(f"/app/review?sequence={sequence.id}&step=1").text
    # The hidden field carries every exact version id.
    for row in rows:
        assert str(row.version_id) in page

    response = client.post(
        f"/app/review/sequence/{sequence.id}/approve",
        data={
            "version_ids": ",".join(str(row.version_id) for row in rows),
            "back": f"/app/review?sequence={sequence.id}",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert db_session.scalar(select(func.count(EmailSequenceMessageReview.id))) == SEQUENCE_LENGTH
    db_session.refresh(sequence)
    assert sequence.review_state is SequenceReviewState.APPROVED


def test_a_stale_bulk_submission_is_refused_over_http(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """Test 68."""

    sequence = build(db_session, scenario)
    rows = sequence_read.message_rows(db_session, sequence=sequence)
    stale = [str(row.version_id) for row in rows[:-1]] + [str(uuid.uuid4())]
    response = client.post(
        f"/app/review/sequence/{sequence.id}/approve",
        data={"version_ids": ",".join(stale), "back": "/app/review"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "err=" in response.headers["location"]
    assert db_session.scalar(select(func.count(EmailSequenceMessageReview.id))) == 0


def test_editing_over_http_writes_one_new_version(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """Test 70: the edit form works, and it touches one message."""

    sequence = build(db_session, scenario)
    rows = sequence_read.message_rows(db_session, sequence=sequence)
    response = client.post(
        f"/app/review/sequence/messages/{rows[1].version_id}/edit",
        data={
            "subject": "A shorter reminder",
            "body": "Rewritten by the operator, and shorter than what the Agent produced.",
            "back": f"/app/review?sequence={sequence.id}&step=2",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    after = sequence_read.message_rows(db_session, sequence=sequence)
    assert after[1].version_id != rows[1].version_id
    assert after[1].message_version == 2
    assert after[1].edit_label == "human-edited"
    assert [row.version_id for row in after if row.position != 2] == [
        row.version_id for row in rows if row.position != 2
    ]


def test_an_off_site_redirect_target_is_discarded(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """The `back` field is echoed into a Location header, so it is constrained."""

    sequence = build(db_session, scenario)
    rows = sequence_read.message_rows(db_session, sequence=sequence)
    for hostile in ("https://evil.example/steal", "//evil.example", "/etc/passwd"):
        response = client.post(
            f"/app/review/sequence/messages/{rows[0].version_id}/approve",
            data={"back": hostile},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/app/review")


def test_user_content_is_escaped_on_the_review_page(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """Test 86."""

    sequence = build(db_session, scenario)
    rows = sequence_read.message_rows(db_session, sequence=sequence)
    sequence_review.edit_message(
        db_session,
        message_version_id=rows[0].version_id,
        subject="<script>alert('subject')</script>",
        body="<img src=x onerror=alert('body')> and some ordinary words after it.",
    )
    body = client.get(f"/app/review?sequence={sequence.id}&step=1").text

    assert "<script>alert('subject')</script>" not in body
    assert "&lt;script&gt;" in body
    assert "<img src=x onerror" not in body


def test_the_queue_is_truthful_when_no_sequence_exists(
    db_session: Session, client: TestClient
) -> None:
    """The unfiltered queue with the feature on says pending, not campaign-off.

    With no ``?campaign=`` there is no single campaign whose opt-in could be
    reported, so the page must not claim one has not opted in.
    """

    body = client.get("/app/review").text
    assert "No sequence has been written yet" in body
    assert "not set up to generate sequences" not in body


def test_a_campaign_that_never_opted_in_is_told_so_rather_than_told_to_wait(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """D-2: the campaign_off state is reachable and carries its own wording.

    This replaces an earlier test that asserted the *absence* of a string no
    route could produce. That test passed trivially and proved nothing.
    """

    campaign, _company, _contact, membership, _policy, _evidence = scenario
    campaign.cadence_config = {"sequence": {"enabled": False}}
    db_session.flush()

    body = client.get(f"/app/review?campaign={membership.campaign_id}").text
    assert "not set up to generate sequences" in body
    assert "No sequence has been written yet" not in body
    assert "each campaign opts in separately" in body


def test_a_failed_generation_is_not_described_as_still_running(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """D-2: the failed state is reachable and does not say 'when it finishes'."""

    from app.models.enums import SequenceValidationStatus

    _campaign, _company, contact, membership, _policy, _evidence = scenario
    sequence = build(db_session, scenario)
    sequence.validation_status = SequenceValidationStatus.FAILED
    db_session.flush()

    body = client.get(f"/app/contacts/{contact.id}?campaign={membership.campaign_id}").text
    assert "did not produce a usable sequence" in body
    assert "When it finishes" not in body
    assert "Nothing partial was kept" in body


def test_the_review_page_omits_the_section_when_the_feature_is_off_and_nothing_exists(
    db_session: Session, client_without_sequences: TestClient, scenario: tuple[Any, ...]
) -> None:
    """Feature off with no sequence anywhere: no section, legacy drafts intact."""

    _campaign, _company, contact, membership, _policy, _evidence = scenario
    db_session.add(
        DraftVersion(
            contact_id=contact.id,
            campaign_id=membership.campaign_id,
            version_number=1,
            subject="A legacy single draft",
            body="Written before sequences existed, and unchanged by them.",
        )
    )
    db_session.flush()

    body = client_without_sequences.get("/app/review").text
    assert "v2-seq-card" not in body
    assert "Sequences are switched off" not in body
    # Test 84: the legacy card is still readable.
    assert "A legacy single draft" in body


# ---------------------------------------------------------------------------
# Contact page
# ---------------------------------------------------------------------------


def test_the_contact_page_renders_a_summary_and_seven_rows(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """Tests 88-90."""

    _campaign, _company, contact, membership, _policy, _evidence = scenario
    build(db_session, scenario)
    response = client.get(f"/app/contacts/{contact.id}?campaign={membership.campaign_id}")
    assert response.status_code == 200
    body = response.text

    assert "The seven-message sequence" in body
    assert "sequence v1" in body
    assert f"of {SEQUENCE_LENGTH} written" in body or "of 7 written" in body
    # Seven rows, each carrying position, purpose, timing, subject, state, version.
    for subject in SUBJECTS:
        assert subject in body
    assert "Day 0 — first message" in body
    assert "Day 35 — 10 days later" in body
    # Every row says approved-by-default rather than the bare word, and the
    # summary says in as many words that nobody has looked.
    assert body.count("approved by default") >= SEQUENCE_LENGTH
    assert "not by anyone" in body
    assert "review is optional" in body
    assert "planned timing, not a schedule" in body


def test_the_review_queue_carries_no_bodies_until_a_sequence_is_opened(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """Tests 91, 99 — now asserted where the rule still holds.

    This rule was written for both surfaces and is still load-bearing for the
    *queue*: a list of forty contacts that rendered seven bodies each would
    transfer megabytes to show a page of cards.

    Beta 1 deliberately changed the other half. The Contact page is one contact
    whose seven messages an operator came to read, copy and edit, so it now
    renders all seven -- see
    ``test_the_contact_page_renders_every_body_without_paging`` below, which
    replaces the assertion that used to live here.
    """

    sequence = build(db_session, scenario)
    closed = client.get("/app/review").text
    assert not any(text in closed for text in BODIES)

    opened = client.get(f"/app/review?sequence={sequence.id}&step=4").text
    shown = [text for text in BODIES if text in opened]
    assert shown == [BODIES[3]]


def test_the_contact_page_renders_every_body_without_paging(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """The Beta 1 contract for this page, replacing the one-at-a-time rule."""

    _campaign, _company, contact, membership, _policy, _evidence = scenario
    build(db_session, scenario)
    body = client.get(f"/app/contacts/{contact.id}?campaign={membership.campaign_id}").text
    shown = [text for text in BODIES if text in body]
    assert shown == list(BODIES), "every message must be readable without a second request"


def test_row_expansion_shows_lineage_and_secondary_identifiers(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """Tests 92-94, and section 19's rule about where exact ids belong."""

    _campaign, _company, contact, membership, _policy, _evidence = scenario
    sequence = build(db_session, scenario)
    body = client.get(f"/app/contacts/{contact.id}?campaign={membership.campaign_id}&step=1").text

    assert "What this message was written from" in body
    assert "Research" in body and "Insights" in body and "Company Intelligence" in body
    assert "never cited as proof" in body
    # Exact ids exist, but inside a collapsed diagnostic block rather than as
    # dominant labels.
    assert "Exact identifiers" in body
    assert str(sequence.sequence_key) in body
    assert body.index("Exact identifiers") > body.index("Subject")


def test_the_contact_page_keeps_an_existing_sequence_visible_when_the_feature_is_off(
    db_session: Session, client_without_sequences: TestClient, scenario: tuple[Any, ...]
) -> None:
    """D-3: switching the feature off must not conceal recorded human work.

    This replaces an earlier test that asserted the section *disappears* — which
    is the defect, not the requirement.
    """

    _campaign, _company, contact, membership, _policy, _evidence = scenario
    sequence = build(db_session, scenario)
    rows = sequence_read.message_rows(db_session, sequence=sequence)
    sequence_review.approve_sequence(
        db_session,
        sequence_id=sequence.id,
        expected_version_ids=tuple(row.version_id for row in rows),
    )

    body = client_without_sequences.get(
        f"/app/contacts/{contact.id}?campaign={membership.campaign_id}"
    ).text

    assert "The seven-message sequence" in body
    assert "Read-only" in body
    assert "switched off in this environment" in body
    assert "7 approved" in body
    # Read-only means read-only: no action form is offered.
    assert f"/app/review/sequence/messages/{rows[0].version_id}/approve" not in body


def test_the_contact_page_is_truthful_when_nothing_has_been_generated(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """Tests 96-97: pending is named, and is not confused with failure."""

    _campaign, _company, contact, membership, _policy, _evidence = scenario
    body = client.get(f"/app/contacts/{contact.id}?campaign={membership.campaign_id}").text
    assert "No sequence has been written yet" in body
    assert "did not produce a usable sequence" not in body


def test_a_historical_single_draft_contact_page_still_reads(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """Test 95: the legacy card survives alongside the sequence section."""

    _campaign, _company, contact, membership, _policy, _evidence = scenario
    db_session.add(
        DraftVersion(
            contact_id=contact.id,
            campaign_id=membership.campaign_id,
            version_number=1,
            subject="A legacy single draft",
            body="One message, written before sequences existed.",
        )
    )
    db_session.flush()
    response = client.get(f"/app/contacts/{contact.id}?campaign={membership.campaign_id}")
    assert response.status_code == 200
    # No fabricated follow-ups appear for it.
    assert "F6" not in response.text


def test_an_unknown_contact_still_404s(db_session: Session, client: TestClient) -> None:
    """Test 98: adding a section did not weaken the not-found path."""

    assert client.get(f"/app/contacts/{uuid.uuid4()}").status_code == 404
    assert client.get("/app/contacts/not-a-uuid").status_code == 404


def test_a_contact_in_a_campaign_without_a_sequence_sees_no_other_contacts_sequence(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """Test 98/85: the section is scoped to this membership, not to the campaign."""

    campaign, company, _contact, _membership, _policy, _evidence = scenario
    build(db_session, scenario)

    other = Contact(
        first_name="Bystander",
        last_name="Example",
        company_name=company.name,
        company_domain=company.domain,
        company_id=company.id,
        email=f"bystander-{uuid.uuid4()}@kiln.example",
        natural_key=f"bystander|{uuid.uuid4()}",
    )
    db_session.add(other)
    db_session.flush()
    membership = CampaignContact(campaign_id=campaign.id, contact_id=other.id)
    db_session.add(membership)
    db_session.flush()

    body = client.get(f"/app/contacts/{other.id}?campaign={campaign.id}").text
    assert "No sequence has been written yet" in body
    assert SUBJECTS[0] not in body


# ---------------------------------------------------------------------------
# Side-effect freedom
# ---------------------------------------------------------------------------


def test_reading_any_sequence_page_writes_nothing(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """Tests 75, 110: a page load is not an action."""

    from app.models.audit_event import AuditEvent

    _campaign, _company, contact, membership, _policy, _evidence = scenario
    sequence = build(db_session, scenario)
    before = db_session.scalar(select(func.count(AuditEvent.id)))

    client.get("/app/review")
    client.get(f"/app/review?sequence={sequence.id}&step=3")
    client.get(f"/app/contacts/{contact.id}?campaign={membership.campaign_id}&step=6")

    assert db_session.scalar(select(func.count(AuditEvent.id))) == before


# ---------------------------------------------------------------------------
# Admin Workbench diagnosis
# ---------------------------------------------------------------------------


def test_admin_diagnosis_shows_the_sequence_and_all_seven_positions(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """Tests 100-106: version, positions, validation, policy, strategy, lineage."""

    _campaign, _company, _contact, membership, _policy, _evidence = scenario
    sequence = build(db_session, scenario)
    response = client.get(f"/admin/campaigns/{membership.campaign_id}/contacts/{membership.id}")
    assert response.status_code == 200
    body = response.text

    assert f"Sequence v{sequence.sequence_version}" in body
    assert "of 7 messages" in body
    for purpose in (
        "initial_outreach",
        "concise_reminder",
        "new_angle",
        "role_relevance",
        "proof_or_outcome",
        "low_friction_resource",
        "close_the_loop",
    ):
        assert purpose in body
    assert "Input digest" in body and sequence.input_digest in body
    assert "Research lineage" in body
    assert "Insights lineage" in body
    assert "Company Intelligence lineage" in body
    assert "Policy and strategy" in body
    assert "planned timing, never a schedule" in body


def test_admin_diagnosis_shows_edit_and_review_history(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """Test 108."""

    _campaign, _company, _contact, membership, _policy, _evidence = scenario
    sequence = build(db_session, scenario)
    rows = sequence_read.message_rows(db_session, sequence=sequence)
    sequence_review.approve_message(db_session, message_version_id=rows[0].version_id)
    sequence_review.edit_message(
        db_session,
        message_version_id=rows[2].version_id,
        subject="Operator wording",
        body="A tighter version of the new-angle message, written by the operator.",
    )
    body = client.get(f"/admin/campaigns/{membership.campaign_id}/contacts/{membership.id}").text

    assert "human_edited" in body
    assert "approved" in body
    assert "by operator" in body


def test_admin_diagnosis_shows_rerun_history_as_separate_sequence_versions(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """Test 107: an explicit regeneration is auditable as its own version."""

    campaign, _company, _contact, membership, _policy, _evidence = scenario
    build(db_session, scenario)
    campaign.primary_cta = "Ask for the preview"
    db_session.flush()
    build(db_session, scenario)

    body = client.get(f"/admin/campaigns/{membership.campaign_id}/contacts/{membership.id}").text
    assert "Sequence v2" in body
    assert "Sequence v1" in body
    assert "superseded" in body


def test_admin_diagnosis_is_truthful_for_a_contact_that_never_had_a_sequence(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """Test 109: legacy memberships stay truthful rather than looking broken."""

    _campaign, _company, _contact, membership, _policy, _evidence = scenario
    body = client.get(f"/admin/campaigns/{membership.campaign_id}/contacts/{membership.id}").text
    assert "No sequence has been generated for this membership" in body
    assert "Sequence v1" not in body


def test_admin_diagnosis_exposes_no_raw_producer_output_or_local_paths(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """Test 111 and section 20: bounded diagnostics, sanitised."""

    _campaign, _company, _contact, membership, _policy, _evidence = scenario
    build(db_session, scenario)
    body = client.get(f"/admin/campaigns/{membership.campaign_id}/contacts/{membership.id}").text

    # No message body reaches the diagnosis page: it diagnoses the generation,
    # not the copy, and the copy is read in the Customer review queue.
    assert not any(text in body for text in BODIES)
    for marker in ("/home/", "/mnt/user-data", "postgresql://", "api_key", "Traceback"):
        assert marker not in body


# ---------------------------------------------------------------------------
# Review without a queue: what the page shows when nothing is waiting
# ---------------------------------------------------------------------------


def test_a_generated_sequence_is_visible_in_the_default_review_view(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """The acceptance test for removing the mandatory backlog.

    The default filter used to be an approval queue. Once generation approves
    its own output that filter is structurally empty, so landing on it would
    have shown an operator an empty page above seven readable messages.
    """

    build(db_session, scenario)
    body = client.get("/app/review").text
    assert body.count("v2-seq-card") == 1
    assert "All sequences" in body
    assert "Waiting for you" not in body.split("v2-rq")[0]


def test_a_sequence_expands_even_when_the_active_filter_excludes_it(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """Following a link to a sequence must show that sequence.

    Expansion used to be resolved by scanning the rows the current filter had
    returned, so asking for a sequence the filter did not include rendered a
    page with no messages on it and no explanation.
    """

    sequence = build(db_session, scenario)
    # "Contains a discard" excludes this sequence: it has none.
    body = client.get(f"/app/review?sview=discarded&sequence={sequence.id}&step=1").text
    assert "v2-seq-card" in body
    assert BODIES[0] in body
    assert "not in the" in body, "the page should say why it is showing an unlisted sequence"
    for label in ("Initial", "F1", "F6"):
        assert f">{label}<" in body


def test_all_seven_bodies_are_readable_without_any_review(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """Requirement: all seven messages available, with no operator action first.

    The requirement never changed; how it is satisfied did. It used to be met
    one ``?step=`` request at a time, and this test walked those seven requests
    asserting each showed its own message. Beta 1 renders all seven at once, so
    the walk is now over the single page.

    Each body appears twice by design -- once in the ``<pre>`` an operator reads
    and copies, once prefilled into that message's edit form -- so this asserts
    presence rather than a count, and asserts the readable copy separately.
    """

    _campaign, _company, contact, membership, _policy, _evidence = scenario
    build(db_session, scenario)
    assert db_session.scalar(select(func.count(EmailSequenceMessageReview.id))) == 0

    body = client.get(f"/app/contacts/{contact.id}?campaign={membership.campaign_id}").text
    for position in range(1, SEQUENCE_LENGTH + 1):
        assert BODIES[position - 1] in body, f"message {position} is not readable"
    # Seven readable bodies, not seven edit boxes.
    assert body.count('class="v2-mail-body" id="seq-body-') == SEQUENCE_LENGTH
    # Readable without any operator action, and said to be so.
    assert body.count("approved by default") >= SEQUENCE_LENGTH


def test_the_contact_page_renders_all_seven_planned_timings(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """The whole ladder, not only its ends."""

    _campaign, _company, contact, membership, _policy, _evidence = scenario
    build(db_session, scenario)
    body = client.get(f"/app/contacts/{contact.id}?campaign={membership.campaign_id}").text
    assert "Day 0 — first message" in body
    for elapsed, delay in ((3, 3), (7, 4), (12, 5), (18, 6), (25, 7), (35, 10)):
        assert f"Day {elapsed} — {delay} days later" in body


def test_the_pages_never_print_the_bare_word_approved_without_saying_whose(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """A default must never read as somebody's judgement.

    Checked on the rendered pages before and after a human decision, so the
    distinction has to survive both states rather than only the empty one.
    """

    _campaign, _company, contact, membership, _policy, _evidence = scenario
    sequence = build(db_session, scenario)
    contact_url = f"/app/contacts/{contact.id}?campaign={membership.campaign_id}&step=1"

    fresh = client.get(contact_url).text
    assert "approved by default" in fresh
    assert "approved by you" not in fresh
    assert "not by anyone" in fresh

    rows = sequence_read.message_rows(db_session, sequence=sequence)
    sequence_review.approve_message(db_session, message_version_id=rows[0].version_id)
    db_session.commit()

    after = client.get(contact_url).text
    assert "approved by you" in after
    assert "approved by default" in after, "the other six are still defaults"
    assert "not by anyone" not in after


def test_the_sequences_placeholder_no_longer_denies_the_engine_exists(
    client: TestClient,
) -> None:
    """The placeholder outlived the thing it was placeholding for."""

    body = client.get("/app/sequences").text
    assert "No sequence engine exists" not in body
    assert "there is no sequence" not in body
    assert "no sending path" in body
