"""Regression coverage for the defects an adversarial review reproduced.

Every test here corresponds to a defect that was *found by attack*, not by
design. They are kept together rather than scattered into the feature suites
because their value is different: each one is a proof that a specific way of
getting this wrong stays fixed, and reading them as a group is the fastest way
to understand what the sequence build got wrong the first time.

The temporary attack tests that produced these findings are not kept. Several of
them relied on fixtures too thin to prove what they claimed -- one "failed to
reproduce" a defect that was real, purely because it never built the execution
state the page needed. Those are rewritten here against real state.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from app.api.deps import get_db
from app.core.config import get_settings
from app.main import create_app
from app.models.audit_event import AuditEvent
from app.models.email_sequence import (
    SEQUENCE_LENGTH,
    EmailSequence,
    EmailSequenceMessageReview,
    EmailSequenceMessageVersion,
)
from app.models.enums import (
    SequenceGenerationStatus,
    SequenceReviewState,
    SequenceStopReason,
    SequenceStopState,
    SequenceValidationStatus,
)
from app.services.sequences import read as sequence_read
from app.services.sequences import review as sequence_review
from app.services.sequences.lineage import (
    MAX_ITEMS,
    MAX_KEYS,
    MAX_STRING_CHARS,
    bounded_lineage,
)
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from tests.test_email_sequence import SUBJECTS, build
from tests.test_email_sequence import scenario as _scenario

scenario = _scenario


def _client(db_session: Session, monkeypatch: pytest.MonkeyPatch, *, sequences: bool) -> TestClient:
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
    with _client(db_session, monkeypatch, sequences=True) as app_client:
        yield app_client
    get_settings.cache_clear()


@pytest.fixture()
def client_off(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    with _client(db_session, monkeypatch, sequences=False) as app_client:
        yield app_client
    get_settings.cache_clear()


# ===========================================================================
# D-1 / D-2 — the UI gate must agree with the generation gate, and every
# declared state must be reachable with its own wording.
# ===========================================================================


def test_an_opted_out_campaign_keeps_its_sequence_but_says_it_is_read_only(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """D-1: the two gates used to disagree, so both outcomes looked current."""

    campaign, _company, contact, membership, _policy, _evidence = scenario
    build(db_session, scenario)
    campaign.cadence_config = {"sequence": {"enabled": False}}
    db_session.flush()

    body = client.get(f"/app/contacts/{contact.id}?campaign={membership.campaign_id}").text

    # Still shown -- the work happened.
    assert "The seven-message sequence" in body
    assert SUBJECTS[0] in body
    # And explained, rather than left to look current.
    assert "Read-only" in body
    assert "no longer configured to generate sequences" in body
    assert "kept read-only" in body


def test_an_opted_out_campaign_refuses_new_review_decisions(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """The refusal lives in the route, not only in the template.

    A page left open across a configuration change will still happily post.
    """

    campaign, _company, _contact, _membership, _policy, _evidence = scenario
    sequence = build(db_session, scenario)
    rows = sequence_read.message_rows(db_session, sequence=sequence)
    campaign.cadence_config = {"sequence": {"enabled": False}}
    db_session.flush()

    response = client.post(
        f"/app/review/sequence/messages/{rows[0].version_id}/approve",
        data={"back": "/app/review"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "err=" in response.headers["location"]
    assert db_session.scalar(select(func.count(EmailSequenceMessageReview.id))) == 0


def test_a_campaign_that_never_opted_in_is_told_so(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """D-2: the campaign_off state is reachable and does not say 'pending'."""

    campaign, _company, contact, membership, _policy, _evidence = scenario
    campaign.cadence_config = {"sequence": {"enabled": False}}
    db_session.flush()

    body = client.get(f"/app/contacts/{contact.id}?campaign={membership.campaign_id}").text
    assert "not set up to generate sequences" in body
    assert "No sequence has been written yet" not in body


def test_an_opted_in_campaign_with_nothing_generated_is_told_to_wait(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """D-2: pending keeps its own wording, and only where it is true."""

    _campaign, _company, contact, membership, _policy, _evidence = scenario
    body = client.get(f"/app/contacts/{contact.id}?campaign={membership.campaign_id}").text
    assert "No sequence has been written yet" in body
    assert "This campaign is opted in" in body


def test_a_refused_generation_says_it_was_refused(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """D-2: the failed state is reachable and never implies work in progress."""

    _campaign, _company, contact, membership, _policy, _evidence = scenario
    sequence = build(db_session, scenario)
    sequence.generation_status = SequenceGenerationStatus.FAILED
    db_session.flush()

    body = client.get(f"/app/contacts/{contact.id}?campaign={membership.campaign_id}").text
    assert "did not produce a usable sequence" in body
    assert "Nothing further will appear here on its own" in body
    assert "When it finishes" not in body


# ===========================================================================
# D-3 — the deployment flag stops generation, not disclosure.
# ===========================================================================


def test_flag_off_keeps_approved_work_visible_and_read_only(
    db_session: Session, client_off: TestClient, scenario: tuple[Any, ...]
) -> None:
    _campaign, _company, contact, membership, _policy, _evidence = scenario
    sequence = build(db_session, scenario)
    rows = sequence_read.message_rows(db_session, sequence=sequence)
    sequence_review.approve_sequence(
        db_session,
        sequence_id=sequence.id,
        expected_version_ids=tuple(row.version_id for row in rows),
    )

    contact_page = client_off.get(
        f"/app/contacts/{contact.id}?campaign={membership.campaign_id}"
    ).text
    assert "The seven-message sequence" in contact_page
    assert "switched off in this environment" in contact_page
    assert "7 approved" in contact_page
    assert "7 approved by you" in contact_page
    assert f"/app/review/sequence/messages/{rows[0].version_id}/approve" not in contact_page

    # The section and its filters stay reachable, and recorded human work is
    # still shown. The default filter is "all", which shows it directly --
    # there is no filter an operator has to find first.
    default_view = client_off.get("/app/review").text
    assert "All sequences" in default_view and "You reviewed these" in default_view
    assert "v2-seq-card" in default_view
    reviewed_view = client_off.get("/app/review?sview=reviewed").text
    assert "v2-seq-card" in reviewed_view
    assert "switched off in this environment" in reviewed_view


def test_flag_off_refuses_new_decisions_but_changes_nothing_recorded(
    db_session: Session, client_off: TestClient, scenario: tuple[Any, ...]
) -> None:
    sequence = build(db_session, scenario)
    rows = sequence_read.message_rows(db_session, sequence=sequence)
    sequence_review.approve_message(db_session, message_version_id=rows[0].version_id)

    response = client_off.post(
        f"/app/review/sequence/messages/{rows[1].version_id}/approve",
        data={"back": "/app/review"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "err=" in response.headers["location"]
    # The existing decision is untouched; the new one was not recorded.
    assert db_session.scalar(select(func.count(EmailSequenceMessageReview.id))) == 1


def test_flag_off_with_nothing_generated_omits_the_section_entirely(
    db_session: Session, client_off: TestClient
) -> None:
    """Nothing to disclose means no permanent 'switched off' banner."""

    body = client_off.get("/app/review").text
    assert "v2-seq-card" not in body
    assert "Sequences are switched off" not in body


# ===========================================================================
# D-5 — actionable position follows the predecessor chain.
# ===========================================================================


def test_a_discarded_initial_leaves_nothing_actionable(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """The defect: follow-up 1 was promoted and would have opened the thread."""

    sequence = build(db_session, scenario)
    rows = sequence_read.message_rows(db_session, sequence=sequence)
    sequence_review.discard_message(db_session, message_version_id=rows[0].version_id)
    sequence_review.approve_message(db_session, message_version_id=rows[1].version_id)
    db_session.refresh(sequence)
    assert sequence.current_actionable_position is None


def test_an_approved_initial_is_actionable_even_when_a_later_message_is_discarded(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    sequence = build(db_session, scenario)
    rows = sequence_read.message_rows(db_session, sequence=sequence)
    sequence_review.approve_message(db_session, message_version_id=rows[0].version_id)
    sequence_review.discard_message(db_session, message_version_id=rows[1].version_id)
    sequence_review.approve_message(db_session, message_version_id=rows[2].version_id)
    db_session.refresh(sequence)
    # Nothing has been sent, so the head is still the only actionable message --
    # and follow-up 2 never steps over the discarded follow-up 1.
    assert sequence.current_actionable_position == 1


def test_editing_a_discarded_initial_makes_the_sequence_actionable_again(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    sequence = build(db_session, scenario)
    rows = sequence_read.message_rows(db_session, sequence=sequence)
    sequence_review.discard_message(db_session, message_version_id=rows[0].version_id)
    db_session.refresh(sequence)
    assert sequence.current_actionable_position is None

    edited = sequence_review.edit_message(
        db_session,
        message_version_id=rows[0].version_id,
        subject="A replacement opening",
        body="The operator rewrote the opening message after discarding the generated one.",
    )
    sequence_review.approve_message(db_session, message_version_id=edited.id)
    db_session.refresh(sequence)
    assert sequence.current_actionable_position == 1


def test_regeneration_after_a_discard_clears_the_actionable_position(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    campaign, _company, _contact, _membership, _policy, _evidence = scenario
    first = build(db_session, scenario)
    rows = sequence_read.message_rows(db_session, sequence=first)
    sequence_review.discard_message(db_session, message_version_id=rows[0].version_id)

    campaign.primary_cta = "A different ask"
    db_session.flush()
    second = build(db_session, scenario)

    # A regenerated sequence carries no decisions at all -- the discard applied
    # to a version that no longer exists -- so every message is approved by
    # default and the chain clears to its head again. Before default approval
    # this asserted `is None`, which measured the absence of review rather than
    # the absence of a blocker.
    assert second.current_actionable_position == 1
    db_session.refresh(first)
    assert first.review_state is SequenceReviewState.SUPERSEDED


def test_a_fully_approved_chain_reports_the_head_as_actionable(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    sequence = build(db_session, scenario)
    rows = sequence_read.message_rows(db_session, sequence=sequence)
    sequence_review.approve_sequence(
        db_session,
        sequence_id=sequence.id,
        expected_version_ids=tuple(row.version_id for row in rows),
    )
    db_session.refresh(sequence)
    assert sequence.current_actionable_position == 1


def test_a_stopped_sequence_has_no_actionable_position(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    sequence = build(db_session, scenario)
    rows = sequence_read.message_rows(db_session, sequence=sequence)
    sequence_review.approve_message(db_session, message_version_id=rows[0].version_id)
    db_session.refresh(sequence)
    assert sequence.current_actionable_position == 1

    sequence.stop_state = SequenceStopState.STOPPED
    sequence.stop_reason = SequenceStopReason.RECIPIENT_REPLY_DETECTED
    db_session.flush()
    aggregate = sequence_review.refresh_aggregate(db_session, sequence=sequence)
    assert aggregate.state is SequenceReviewState.BLOCKED
    assert sequence.current_actionable_position is None


# ===========================================================================
# D-4 — one decision is one audit event.
# ===========================================================================


def _audit_count(db_session: Session, action: str) -> int:
    return int(
        db_session.scalar(select(func.count(AuditEvent.id)).where(AuditEvent.action == action)) or 0
    )


def test_resubmitting_the_same_approval_writes_one_audit_event(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    sequence = build(db_session, scenario)
    rows = sequence_read.message_rows(db_session, sequence=sequence)
    for _ in range(3):
        client.post(
            f"/app/review/sequence/messages/{rows[0].version_id}/approve",
            data={"back": "/app/review", "reason": "double click"},
            follow_redirects=False,
        )
    assert _audit_count(db_session, "email_sequence_message.approved") == 1
    assert db_session.scalar(select(func.count(EmailSequenceMessageReview.id))) == 1


def test_resubmitting_the_same_discard_writes_one_audit_event(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    sequence = build(db_session, scenario)
    rows = sequence_read.message_rows(db_session, sequence=sequence)
    for _ in range(3):
        client.post(
            f"/app/review/sequence/messages/{rows[0].version_id}/discard",
            data={"back": "/app/review", "reason": "not this one"},
            follow_redirects=False,
        )
    assert _audit_count(db_session, "email_sequence_message.discarded") == 1


def test_a_genuinely_changed_decision_is_still_audited(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """Suppressing duplicates must not suppress real changes."""

    sequence = build(db_session, scenario)
    rows = sequence_read.message_rows(db_session, sequence=sequence)
    sequence_review.approve_message(db_session, message_version_id=rows[0].version_id)
    sequence_review.discard_message(db_session, message_version_id=rows[0].version_id)
    sequence_review.approve_message(
        db_session, message_version_id=rows[0].version_id, reason="reconsidered"
    )

    assert _audit_count(db_session, "email_sequence_message.approved") == 2
    assert _audit_count(db_session, "email_sequence_message.discarded") == 1


def test_a_changed_note_on_the_same_decision_is_audited(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    sequence = build(db_session, scenario)
    rows = sequence_read.message_rows(db_session, sequence=sequence)
    sequence_review.approve_message(db_session, message_version_id=rows[0].version_id)
    sequence_review.approve_message(
        db_session, message_version_id=rows[0].version_id, reason="on reflection, yes"
    )
    assert _audit_count(db_session, "email_sequence_message.approved") == 2


# ===========================================================================
# D-6 — the chip and the counts come from one derivation.
# ===========================================================================


def test_a_stale_cached_state_cannot_put_a_half_approved_sequence_in_approved(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """The cache is a filter, never the authority."""

    sequence = build(db_session, scenario)
    rows = sequence_read.message_rows(db_session, sequence=sequence)
    for row in rows:
        sequence_review.approve_message(db_session, message_version_id=row.version_id)

    # Any write that bypasses refresh_aggregate: a crash mid-request, an admin
    # data fix, a restore, or a future service that forgets the call.
    db_session.execute(
        EmailSequenceMessageReview.__table__.delete().where(
            EmailSequenceMessageReview.message_version_id == rows[3].version_id
        )
    )
    db_session.flush()

    # Deleting the row does not unapprove the message -- under default approval
    # a version with no decision is approved -- so what is left stale is the
    # *human* tally, and the card must report that rather than the cache's
    # memory of it.
    reviewed = sequence_read.list_queue(db_session, view=sequence_read.VIEW_REVIEWED)
    assert len(reviewed.rows) == 1
    card = reviewed.rows[0]
    assert card.review_state is SequenceReviewState.APPROVED
    assert card.approved == SEQUENCE_LENGTH
    assert card.human_approved == SEQUENCE_LENGTH - 1
    assert card.unreviewed == 1
    assert card.cache_is_stale is False


def test_the_card_state_and_counts_never_disagree(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    sequence = build(db_session, scenario)
    rows = sequence_read.message_rows(db_session, sequence=sequence)
    sequence_review.approve_sequence(
        db_session,
        sequence_id=sequence.id,
        expected_version_ids=tuple(row.version_id for row in rows),
    )
    card = sequence_read.list_queue(db_session, view=sequence_read.VIEW_REVIEWED).rows[0]
    assert card.review_state is SequenceReviewState.APPROVED
    assert card.approved == card.human_approved == card.message_count == SEQUENCE_LENGTH
    assert card.cache_is_stale is False


def test_a_zero_message_sequence_does_not_derive_as_generated(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """A sequence is exactly seven messages; anything else was never written."""

    _campaign, _company, _contact, membership, _policy, _evidence = scenario
    empty = EmailSequence(
        sequence_key=uuid.uuid4(),
        sequence_version=1,
        campaign_contact_id=membership.id,
        campaign_id=membership.campaign_id,
        contact_id=membership.contact_id,
        input_digest="f" * 64,
        sequence_producer_version="x/v1",
        validation_policy_version="y/v1",
    )
    db_session.add(empty)
    db_session.flush()
    aggregate = sequence_review.refresh_aggregate(db_session, sequence=empty)
    assert aggregate.state is SequenceReviewState.FAILED
    assert empty.current_actionable_position is None


# ===========================================================================
# D-7 — Admin diagnosis query count is bounded by history, not by it.
# ===========================================================================


def test_admin_diagnosis_query_count_does_not_grow_with_regenerations(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, scenario: tuple[Any, ...]
) -> None:
    from app.services.admin_workbench.reader import AdminWorkbenchReader

    campaign, _company, _contact, membership, _policy, _evidence = scenario

    def _measure() -> tuple[int, int]:
        statements: list[str] = []

        def _record(_c: Any, _cur: Any, statement: str, *_a: Any) -> None:
            if statement.lstrip().upper().startswith("SELECT") and "email_sequence" in statement:
                statements.append(statement)

        monkeypatch.setenv("FEATURES__EMAIL_SEQUENCES", "true")
        get_settings.cache_clear()
        reader = AdminWorkbenchReader(db_session, settings=get_settings())
        event.listen(db_session.bind, "before_cursor_execute", _record)
        try:
            view = reader.contact_diagnosis(membership.campaign_id, membership.id)
        finally:
            event.remove(db_session.bind, "before_cursor_execute", _record)
        assert view is not None
        return len(view.sequences), len(statements)

    build(db_session, scenario)
    versions_one, queries_one = _measure()

    for index in range(5):
        campaign.primary_cta = f"CTA revision {index}"
        db_session.flush()
        build(db_session, scenario)
    versions_many, queries_many = _measure()

    assert versions_one == 1
    assert versions_many == 6
    # Constant, not proportional. The exact number is an implementation detail;
    # that it does not move as history grows is the guarantee.
    assert queries_many == queries_one, (
        f"{versions_many} versions cost {queries_many} queries where "
        f"{versions_one} cost {queries_one}"
    )
    assert queries_many <= 6


def test_admin_history_stays_capped_at_ten_versions(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, scenario: tuple[Any, ...]
) -> None:
    from app.services.admin_workbench.reader import AdminWorkbenchReader

    campaign, _company, _contact, membership, _policy, _evidence = scenario
    for index in range(12):
        campaign.primary_cta = f"CTA {index}"
        db_session.flush()
        build(db_session, scenario)

    monkeypatch.setenv("FEATURES__EMAIL_SEQUENCES", "true")
    get_settings.cache_clear()
    view = AdminWorkbenchReader(db_session, settings=get_settings()).contact_diagnosis(
        membership.campaign_id, membership.id
    )
    assert view is not None
    assert len(view.sequences) == 10
    assert view.sequences[0].sequence_version == 12


# ===========================================================================
# D-8 — lineage rendering is bounded and says what it removed.
# ===========================================================================


def test_small_lineage_passes_through_unchanged() -> None:
    original = {"dossier_id": "abc", "dossier_version": 3, "available": True, "ids": ["a", "b"]}
    assert bounded_lineage(original) == original


def test_a_long_string_is_truncated_and_says_so() -> None:
    bounded = bounded_lineage({"claim": "y" * 5_000})
    value = bounded["claim"]
    assert isinstance(value, str)
    assert "[truncated: 5000 characters]" in value
    assert len(value) < MAX_STRING_CHARS + 60


def test_a_wide_object_is_capped_and_says_so() -> None:
    bounded = bounded_lineage({f"key{index}": index for index in range(MAX_KEYS + 25)})
    assert len(bounded) == MAX_KEYS + 1
    assert "further key(s)" in bounded["__truncated__"]


def test_a_long_array_is_capped_and_says_so() -> None:
    bounded = bounded_lineage({"ids": list(range(MAX_ITEMS + 30))})
    items = bounded["ids"]
    assert isinstance(items, list)
    assert len(items) == MAX_ITEMS + 1
    assert "further item(s)" in items[-1]


def test_deep_nesting_is_cut_off_and_says_so() -> None:
    deep: dict[str, Any] = {"leaf": "bottom"}
    for _ in range(20):
        deep = {"down": deep}
    rendered = repr(bounded_lineage(deep))
    assert "nested deeper than" in rendered
    assert "bottom" not in rendered


def test_hostile_html_inside_lineage_survives_as_data_and_is_escaped_on_the_page(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, scenario: tuple[Any, ...]
) -> None:
    """Bounding does not sanitise -- escaping is the template's job, and it works."""

    _campaign, _company, _contact, membership, _policy, _evidence = scenario
    sequence = build(db_session, scenario)
    sequence.research_lineage = {"note": "<script>alert(1)</script>"}
    db_session.flush()

    assert bounded_lineage(sequence.research_lineage) == {"note": "<script>alert(1)</script>"}

    with _client(db_session, monkeypatch, sequences=True) as client:
        body = client.get(
            f"/admin/campaigns/{membership.campaign_id}/contacts/{membership.id}"
        ).text
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_an_oversized_lineage_cannot_produce_a_huge_admin_page(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, scenario: tuple[Any, ...]
) -> None:
    _campaign, _company, _contact, membership, _policy, _evidence = scenario
    sequence = build(db_session, scenario)
    sequence.research_lineage = {"blob": ["y" * 1_000 for _ in range(2_000)]}
    db_session.flush()

    with _client(db_session, monkeypatch, sequences=True) as client:
        response = client.get(f"/admin/campaigns/{membership.campaign_id}/contacts/{membership.id}")
    assert response.status_code == 200
    assert len(response.text) < 200_000, f"admin page grew to {len(response.text)} bytes"


# ===========================================================================
# Smaller fixes: view mismatch, same-origin, oversized submissions.
# ===========================================================================


def test_the_sequence_filter_is_independent_of_the_draft_filter(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """Choosing 'Discarded' for drafts used to silently reinterpret the sequence filter."""

    sequence = build(db_session, scenario)
    rows = sequence_read.message_rows(db_session, sequence=sequence)
    sequence_review.approve_sequence(
        db_session,
        sequence_id=sequence.id,
        expected_version_ids=tuple(row.version_id for row in rows),
    )

    # The draft view says "discarded"; the sequence section has its own filter
    # and must not reinterpret it. The sequence here has no discarded message,
    # so the sequence "discarded" filter is empty while "reviewed" is not.
    discarded = client.get("/app/review?view=discarded&sview=discarded").text
    assert "v2-seq-card" not in discarded

    reviewed = client.get("/app/review?view=discarded&sview=reviewed").text
    assert "v2-seq-card" in reviewed


def test_a_cross_site_submission_is_refused(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    sequence = build(db_session, scenario)
    rows = sequence_read.message_rows(db_session, sequence=sequence)
    response = client.post(
        f"/app/review/sequence/messages/{rows[0].version_id}/edit",
        data={"subject": "Injected", "body": "Written from another site.", "back": "/app/review"},
        headers={"Sec-Fetch-Site": "cross-site"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "err=" in response.headers["location"]
    assert (
        db_session.scalar(
            select(func.count(EmailSequenceMessageVersion.id)).where(
                EmailSequenceMessageVersion.message_id == rows[0].message_id
            )
        )
        == 1
    ), "no new version may be written by a cross-site request"


def test_a_same_origin_submission_is_allowed(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    sequence = build(db_session, scenario)
    rows = sequence_read.message_rows(db_session, sequence=sequence)
    response = client.post(
        f"/app/review/sequence/messages/{rows[0].version_id}/edit",
        data={
            "subject": "Reworded",
            "body": "The operator rewrote this from the app.",
            "back": "/app/review",
        },
        headers={"Sec-Fetch-Site": "same-origin"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "ok=" in response.headers["location"]


def test_an_oversized_submission_is_refused_before_it_is_parsed(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    sequence = build(db_session, scenario)
    rows = sequence_read.message_rows(db_session, sequence=sequence)
    response = client.post(
        f"/app/review/sequence/messages/{rows[0].version_id}/edit",
        data={"subject": "Big", "body": "x" * 400_000, "back": "/app/review"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "err=" in response.headers["location"]
    assert (
        db_session.scalar(
            select(func.count(EmailSequenceMessageVersion.id)).where(
                EmailSequenceMessageVersion.message_id == rows[0].message_id
            )
        )
        == 1
    )


def test_message_count_records_what_was_generated(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """The column is a historical fact, kept distinct from the live tally."""

    sequence = build(db_session, scenario)
    assert sequence.message_count == SEQUENCE_LENGTH
    summary = sequence_read.summary(db_session, sequence=sequence)
    assert summary.message_count == SEQUENCE_LENGTH

    rows = sequence_read.message_rows(db_session, sequence=sequence)
    sequence_review.discard_message(db_session, message_version_id=rows[0].version_id)
    db_session.refresh(sequence)
    # Discarding does not change what was generated.
    assert sequence.message_count == SEQUENCE_LENGTH


def test_supersession_timestamps_are_orderable_within_one_transaction(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """clock_timestamp(), not now(): now() is constant for a whole transaction."""

    campaign, _company, _contact, _membership, _policy, _evidence = scenario
    first = build(db_session, scenario)
    rows = sequence_read.message_rows(db_session, sequence=first)
    sequence_review.edit_message(
        db_session,
        message_version_id=rows[0].version_id,
        subject="Edited once",
        body="An operator rewrote the opening message before regenerating.",
    )
    campaign.primary_cta = "Changed"
    db_session.flush()
    build(db_session, scenario)

    stamps = list(
        db_session.scalars(
            select(EmailSequenceMessageVersion.superseded_at)
            .where(EmailSequenceMessageVersion.superseded_at.is_not(None))
            .order_by(EmailSequenceMessageVersion.superseded_at)
        ).all()
    )
    assert len(stamps) >= 2
    assert len(set(stamps)) > 1, "supersessions in one transaction must remain orderable"


def test_validation_failure_derives_as_failed_not_as_awaiting(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    sequence = build(db_session, scenario)
    sequence.validation_status = SequenceValidationStatus.FAILED
    db_session.flush()
    aggregate = sequence_review.refresh_aggregate(db_session, sequence=sequence)
    assert aggregate.state is SequenceReviewState.FAILED
    # A failed sequence is not offered by any narrowing filter. It stays
    # visible under "all", because a failure an operator cannot find is worse
    # than one they can.
    assert sequence_read.list_queue(db_session, view=sequence_read.VIEW_REVIEWED).rows == ()
    assert sequence_read.list_queue(db_session, view=sequence_read.VIEW_EDITED).rows == ()
    assert sequence_read.list_queue(db_session, view=sequence_read.VIEW_DISCARDED).rows == ()
    assert len(sequence_read.list_queue(db_session, view=sequence_read.VIEW_ALL).rows) == 1
