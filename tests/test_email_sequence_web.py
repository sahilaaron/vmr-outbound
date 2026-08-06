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
    assert "0 approved" in body
    assert f"{SEQUENCE_LENGTH} waiting" in body
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
    """Test 83: pending is named as pending."""

    body = client.get("/app/review").text
    assert "No sequence has been written yet" in body
    assert "all seven messages as one unit" in body


def test_the_review_page_shows_no_sequence_section_when_the_feature_is_off(
    db_session: Session, client_without_sequences: TestClient, scenario: tuple[Any, ...]
) -> None:
    """Tests 83, 96, 115: off is genuinely off, and legacy drafts still render."""

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
    assert "Sequences" not in body.split("<main")[1].split("</main")[0].split("<nav")[0]
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
    assert "waiting for you" in body
    assert "planned timing, not a schedule" in body


def test_the_contact_page_table_carries_no_bodies_until_a_row_is_opened(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """Tests 91, 99."""

    _campaign, _company, contact, membership, _policy, _evidence = scenario
    build(db_session, scenario)
    closed = client.get(f"/app/contacts/{contact.id}?campaign={membership.campaign_id}").text
    assert not any(text in closed for text in BODIES)

    opened = client.get(f"/app/contacts/{contact.id}?campaign={membership.campaign_id}&step=4").text
    shown = [text for text in BODIES if text in opened]
    assert shown == [BODIES[3]]


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


def test_the_contact_page_is_truthful_when_the_feature_is_off(
    db_session: Session, client_without_sequences: TestClient, scenario: tuple[Any, ...]
) -> None:
    """Test 96."""

    _campaign, _company, contact, membership, _policy, _evidence = scenario
    build(db_session, scenario)
    body = client_without_sequences.get(
        f"/app/contacts/{contact.id}?campaign={membership.campaign_id}"
    ).text
    assert "The seven-message sequence" not in body


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
