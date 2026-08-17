"""The seven-message sequence on the sending desk and in Admin diagnosis, over HTTP.

Two things these tests exist to protect.

**The write routes record exact versions.** Approve, bulk approve and edit
name the immutable message version they act on, and a stale submission is
refused rather than quietly applied to text nobody read.

**The desk shows exactly this person's emails, escaped, in full.** The person
page carries no email bodies: each Campaign row points into the inline sending
desk on Campaign Overview (``?person=<membership>&email=<n>``), which renders
one email at a time. A person who is not Ready for Sending has no desk at all,
and never sees another person's messages.

The global Emails/Review page no longer exists, and the person page no longer
carries the per-state sequence notices, so the queue and notice tests that
lived here are gone.
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
from tests.test_customer_operating_model import _ready
from tests.test_email_sequence import BODIES, SUBJECTS, build
from tests.test_email_sequence import scenario as _scenario

scenario = _scenario


def _desk_url(scenario: tuple[Any, ...], email: int) -> str:
    """Campaign Overview with the inline sending desk open on one email."""

    _campaign, _company, _contact, membership, _policy, _evidence = scenario
    return (
        f"/app/campaigns/{membership.campaign_id}?section=all&person={membership.id}&email={email}"
    )


def _doc(body: str) -> str:
    """The one document card the desk renders for the selected email."""

    start = body.index('<article class="v2-doc"')
    end = body.index("</article>", start)
    return body[start:end]


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


# ---------------------------------------------------------------------------
# Review queue
# ---------------------------------------------------------------------------


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
    """Test 66. No page renders this form any more; the route still names every version."""

    sequence = build(db_session, scenario)
    rows = sequence_read.message_rows(db_session, sequence=sequence)
    _campaign, _company, contact, membership, _policy, _evidence = scenario

    response = client.post(
        f"/app/review/sequence/{sequence.id}/approve",
        data={
            "version_ids": ",".join(str(row.version_id) for row in rows),
            "back": f"/app/people/{contact.id}?campaign={membership.campaign_id}",
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
        # ...and that fallback itself resolves inside the product.
        follow = client.get("/app/review", follow_redirects=False)
        assert follow.status_code == 308
        assert follow.headers["location"].startswith("/app/campaigns")


def test_a_failed_generation_is_not_described_as_still_running(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """D-2: a sequence that failed validation is not a package anybody can act on.

    The person page used to carry a per-state notice; now the only place emails
    are read is the sending desk, and a failed sequence must not open one. The
    person is not Ready for Sending, the person page offers no way into the
    desk, and asking Campaign Overview for the desk renders none.
    """

    from app.models.enums import SequenceValidationStatus

    _campaign, _company, contact, membership, _policy, _evidence = scenario
    sequence = _ready(db_session, scenario)
    sequence.validation_status = SequenceValidationStatus.FAILED
    db_session.flush()

    person = client.get(f"/app/people/{contact.id}?campaign={membership.campaign_id}").text
    assert "Open in Campaign" not in person
    assert SUBJECTS[0] not in person

    overview = client.get(_desk_url(scenario, 1)).text
    assert 'class="v2-desk"' not in overview
    assert SUBJECTS[0] not in overview
    assert BODIES[0] not in overview


# ---------------------------------------------------------------------------
# Contact page
# ---------------------------------------------------------------------------


def test_user_content_is_escaped_on_the_desk(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """Test 86. Message text is rendered raw (never neutralized) but always escaped."""

    sequence = _ready(db_session, scenario)
    rows = sequence_read.message_rows(db_session, sequence=sequence)
    sequence_review.edit_message(
        db_session,
        message_version_id=rows[0].version_id,
        subject="<script>alert('subject')</script>",
        body="<img src=x onerror=alert('body')> and some ordinary words after it.",
    )
    body = client.get(_desk_url(scenario, 1)).text

    assert "<script>alert('subject')</script>" not in body
    assert "&lt;script&gt;" in body
    assert "<img src=x onerror" not in body
    # Escaped in the readable copy and in the edit prefill alike.
    doc = _doc(body)
    assert doc.count("&lt;script&gt;alert(&#39;subject&#39;)&lt;/script&gt;") >= 1
    assert "&lt;img src=x onerror=alert(&#39;body&#39;)&gt;" in doc


def test_a_legacy_emails_link_resolves_to_the_person_page(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """A bookmarked `/app/review?sequence=` still lands on those emails."""

    _campaign, _company, contact, membership, _policy, _evidence = scenario
    sequence = build(db_session, scenario)
    response = client.get(f"/app/review?sequence={sequence.id}", follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"] == (
        f"/app/people/{contact.id}?campaign={membership.campaign_id}#emails"
    )


def test_the_desk_renders_every_body_one_email_at_a_time(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """The desk contract: one email per request, and every one of the seven complete.

    Each request renders exactly one readable body and one edit prefill; the
    other six bodies are not on that page, so a body that leaked from another
    position would be caught here.
    """

    _ready(db_session, scenario)
    for position in range(1, SEQUENCE_LENGTH + 1):
        body = client.get(_desk_url(scenario, position)).text
        assert body.count('class="v2-doc-body"') == 1
        doc = _doc(body)
        assert BODIES[position - 1] in doc, f"message {position} is not readable in full"
        assert SUBJECTS[position - 1] in doc
        for other in range(SEQUENCE_LENGTH):
            if other != position - 1:
                assert BODIES[other] not in body, f"body {other + 1} leaked onto email {position}"


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
    response = client.get(f"/app/people/{contact.id}?campaign={membership.campaign_id}")
    assert response.status_code == 200
    # No fabricated follow-ups appear for it.
    assert "F6" not in response.text


def test_an_unknown_contact_still_404s(db_session: Session, client: TestClient) -> None:
    """Test 98: adding a section did not weaken the not-found path."""

    assert client.get(f"/app/people/{uuid.uuid4()}").status_code == 404
    assert client.get("/app/people/not-a-uuid").status_code == 404


def test_a_contact_in_a_campaign_without_a_sequence_sees_no_other_contacts_sequence(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """Test 98/85: the desk is scoped to this membership, not to the campaign."""

    campaign, company, _contact, _membership, _policy, _evidence = scenario
    _ready(db_session, scenario)

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

    body = client.get(f"/app/people/{other.id}?campaign={campaign.id}").text
    assert "Open in Campaign" not in body
    assert SUBJECTS[0] not in body

    # Asking the Campaign for the bystander's desk opens nothing, and certainly
    # not the other person's emails.
    overview = client.get(
        f"/app/campaigns/{campaign.id}?section=all&person={membership.id}&email=1"
    ).text
    assert 'class="v2-desk"' not in overview
    assert SUBJECTS[0] not in overview
    assert BODIES[0] not in overview


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

    client.get(f"/app/review?sequence={sequence.id}")
    client.get(f"/app/people/{contact.id}?campaign={membership.campaign_id}&step=6")

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


def test_all_seven_bodies_are_readable_without_any_review(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """Requirement: all seven messages available, with no operator action first.

    The requirement never changed; how it is satisfied did. The desk renders
    one email per request, so this walks the seven and asserts each shows its
    own message with no review row anywhere.

    Each body appears twice by design -- once in the ``<pre>`` an operator reads
    and copies, once prefilled into that message's edit form -- so this asserts
    presence rather than a count, and asserts the readable copy separately.
    """

    _ready(db_session, scenario)
    assert db_session.scalar(select(func.count(EmailSequenceMessageReview.id))) == 0

    for position in range(1, SEQUENCE_LENGTH + 1):
        body = client.get(_desk_url(scenario, position)).text
        assert BODIES[position - 1] in body, f"message {position} is not readable"
        # One readable body, not an edit box standing in for it.
        assert body.count(f'class="v2-doc-body" id="desk-body-{position}"') == 1
        # And nothing on the desk asks for an approval before reading it.
        for pressure in ("Needs approval", "Approve before", "Waiting for you"):
            assert pressure not in body


def test_the_desk_renders_all_seven_planned_timings(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """The whole ladder, not only its ends: the rail names every day."""

    _ready(db_session, scenario)
    body = client.get(_desk_url(scenario, 1)).text
    assert body.count('class="v2-rail-step') == SEQUENCE_LENGTH
    for position, elapsed in enumerate((0, 3, 7, 12, 18, 25, 35), start=1):
        assert f"Email {position}" in body
        assert f"Day {elapsed}" in body
    # And the selected email's card says which day it is.
    for position, elapsed in enumerate((0, 3, 7, 12, 18, 25, 35), start=1):
        doc = _doc(client.get(_desk_url(scenario, position)).text)
        assert f'<span class="v2-doc-k">Email {position}</span>' in doc
        assert f"Day {elapsed}" in doc
