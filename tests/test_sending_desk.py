"""The inline sending desk: manual email actions, Day 0, and the Today return surface.

The contract these hold:

* Email 1 marked Actioned establishes Day 0; Emails 2-7 are due on whole local
  days from that anchor, never relative to the previous action.
* Copy and Gmail draft never mark anything actioned; only the explicit act does.
* Skip is a deliberate, confirmed act on Emails 2-7; Undo reverses one act and
  keeps the history.
* The desk is a region of Campaign Overview reached by ``?person=`` — never a
  page of its own — and Today brings the user back to it.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from app.api.deps import get_db
from app.core.config import get_settings
from app.main import create_app
from app.models.email_action import SequenceEmailAction
from app.models.email_sequence import EmailSequenceMessageVersion
from app.services import email_progress, today
from app.services.sequences import read as sequence_read
from app.services.sequences import review as sequence_review
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tests.gmail_factory import build_sequence
from tests.test_customer_operating_model import _ready
from tests.test_email_sequence import scenario as _scenario

scenario = _scenario


@pytest.fixture()
def client(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__AGENT_WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__EMAIL_SEQUENCES", "true")
    get_settings.cache_clear()
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as app_client:
        yield app_client
    get_settings.cache_clear()


def _membership(scenario: tuple[Any, ...]) -> Any:
    return scenario[3]


def _campaign_url(scenario: tuple[Any, ...]) -> str:
    return f"/app/campaigns/{_membership(scenario).campaign_id}"


def _desk_url(scenario: tuple[Any, ...], **params: Any) -> str:
    membership = _membership(scenario)
    query = "&".join(f"{key}={value}" for key, value in params.items())
    return f"/app/campaigns/{membership.campaign_id}/desk/{membership.id}/" + query


# ---------------------------------------------------------------------------
# Projection and acts
# ---------------------------------------------------------------------------


def test_a_fresh_ready_person_has_email_one_ready_and_nothing_dated(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    sequence = _ready(db_session, scenario)
    progress = email_progress.progress_for_sequence(db_session, sequence=sequence)

    assert progress.day_zero is None
    assert progress.next_email is not None and progress.next_email.position == 1
    assert progress.next_email.state == email_progress.STATE_READY
    assert [email.state for email in progress.emails[1:]] == [email_progress.STATE_UPCOMING] * 6
    assert all(email.due_on is None for email in progress.emails)
    assert progress.due_now is True
    assert progress.follow_up_due is False
    assert progress.progress_label == "0 of 7 actioned"


def test_marking_email_one_actioned_establishes_day_zero(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    _ready(db_session, scenario)
    membership = _membership(scenario)

    progress = email_progress.mark_actioned(
        db_session, membership_id=membership.id, position=1, actor="alice@example.com"
    )

    assert progress.day_zero == email_progress.local_today()
    assert progress.emails[0].state == email_progress.STATE_ACTIONED
    assert progress.emails[0].acted_by == "alice@example.com"
    assert progress.actioned_count == 1
    assert progress.next_email is not None and progress.next_email.position == 2
    # Follow-ups are dated from Day 0, on the locked ladder.
    assert [email.due_on for email in progress.emails[1:]] == [
        progress.day_zero + timedelta(days=offset) for offset in (3, 7, 12, 18, 25, 35)
    ]
    assert progress.emails[1].state == email_progress.STATE_UPCOMING
    assert progress.due_now is False


def test_follow_ups_are_due_relative_to_day_zero_not_the_previous_action(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    sequence = _ready(db_session, scenario)
    membership = _membership(scenario)
    email_progress.mark_actioned(db_session, membership_id=membership.id, position=1, actor="a")
    day_zero = email_progress.local_today()

    on_day_3 = email_progress.progress_for_sequence(
        db_session, sequence=sequence, today=day_zero + timedelta(days=3)
    )
    assert on_day_3.next_email is not None
    assert on_day_3.next_email.position == 2
    assert on_day_3.next_email.state == email_progress.STATE_DUE
    assert on_day_3.due_label == "Today"

    on_day_5 = email_progress.progress_for_sequence(
        db_session, sequence=sequence, today=day_zero + timedelta(days=5)
    )
    assert on_day_5.next_email is not None
    assert on_day_5.next_email.state == email_progress.STATE_OVERDUE
    assert on_day_5.due_label == "2 days overdue"

    # Acting late does not slide the cadence: Email 3 is still Day 7 from Day 0.
    email_progress.mark_actioned(db_session, membership_id=membership.id, position=2, actor="a")
    later = email_progress.progress_for_sequence(
        db_session, sequence=sequence, today=day_zero + timedelta(days=5)
    )
    assert later.emails[2].due_on == day_zero + timedelta(days=7)
    assert later.emails[2].state == email_progress.STATE_UPCOMING


def test_skip_is_for_follow_ups_only_and_undo_keeps_the_history(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    _ready(db_session, scenario)
    membership = _membership(scenario)

    with pytest.raises(email_progress.EmailActionError):
        email_progress.skip_follow_up(
            db_session, membership_id=membership.id, position=1, actor="a"
        )

    progress = email_progress.skip_follow_up(
        db_session, membership_id=membership.id, position=3, actor="a"
    )
    assert progress.emails[2].state == email_progress.STATE_SKIPPED
    # A skipped email is out of the manual cycle; the next email walks past it.
    email_progress.mark_actioned(db_session, membership_id=membership.id, position=1, actor="a")
    email_progress.mark_actioned(db_session, membership_id=membership.id, position=2, actor="a")
    progress = email_progress.progress_for_sequence(
        db_session,
        sequence=sequence_read.sequence_for_membership(
            db_session, campaign_contact_id=membership.id
        ),
    )
    assert progress.next_email is not None and progress.next_email.position == 4

    progress = email_progress.undo(db_session, membership_id=membership.id, position=3, actor="a")
    assert progress.emails[2].state == email_progress.STATE_UPCOMING
    assert progress.next_email is not None and progress.next_email.position == 3
    # Nothing was deleted: the skip and the undo are both on the ledger.
    assert db_session.scalar(select(func.count(SequenceEmailAction.id))) == 4


def test_double_actioning_and_actioning_a_non_ready_person_are_refused(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    membership = _membership(scenario)
    with pytest.raises(email_progress.EmailActionError):
        email_progress.mark_actioned(db_session, membership_id=membership.id, position=1, actor="a")

    _ready(db_session, scenario)
    email_progress.mark_actioned(db_session, membership_id=membership.id, position=1, actor="a")
    with pytest.raises(email_progress.EmailActionError):
        email_progress.mark_actioned(db_session, membership_id=membership.id, position=1, actor="a")


def test_the_action_records_the_exact_version_and_survives_an_edit(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    from app.services.sequences import review as sequence_review

    sequence = _ready(db_session, scenario)
    membership = _membership(scenario)
    before = sequence_read.message_rows(db_session, sequence=sequence)[0]
    email_progress.mark_actioned(db_session, membership_id=membership.id, position=1, actor="a")
    row = db_session.scalars(select(SequenceEmailAction)).one()
    assert row.message_version_id == before.version_id

    sequence_review.edit_message(
        db_session,
        message_version_id=before.version_id,
        subject="A newer subject",
        body="A newer body that says something else entirely.",
    )
    progress = email_progress.progress_for_sequence(db_session, sequence=sequence)
    assert progress.emails[0].state == email_progress.STATE_ACTIONED
    assert progress.emails[0].stale_version is True


# ---------------------------------------------------------------------------
# Over the wire
# ---------------------------------------------------------------------------


def test_the_ready_table_shows_desk_columns_and_filters(
    client: TestClient, db_session: Session, scenario: tuple[Any, ...]
) -> None:
    _ready(db_session, scenario)
    body = client.get(_campaign_url(scenario)).text
    for label in ("Due now", "All ready", "First email", "Follow-ups", "Actioned"):
        assert label in body
    for column in ("Next email", "Due", "Progress", "Last action"):
        assert column in body
    assert "0 of 7 actioned" in body
    assert "v2-desk" not in body


def test_selecting_a_person_opens_the_workbook_in_place(
    client: TestClient, db_session: Session, scenario: tuple[Any, ...]
) -> None:
    _ready(db_session, scenario)
    membership = _membership(scenario)
    body = client.get(f"{_campaign_url(scenario)}?person={membership.id}").text

    assert 'class="v2-desk"' in body
    assert body.count('class="v2-rail-step') == 7
    for label in ("Email 1", "Day 0", "Email 7", "Day 35"):
        assert label in body
    for action in ("Copy", "Mark actioned", "Why this email?", "History", "Edit"):
        assert action in body
    # No route of its own, and no technical vocabulary.
    assert "Nothing here sends" in body
    for forbidden in ("sequence_id", "version_id=", "policy", "strategy", "Agent"):
        assert forbidden not in body.split("<main")[1].split("Recent activity")[0], forbidden


def test_mark_actioned_over_http_advances_to_the_next_email(
    client: TestClient, db_session: Session, scenario: tuple[Any, ...]
) -> None:
    _ready(db_session, scenario)
    membership = _membership(scenario)
    response = client.post(
        _desk_url(scenario) + "1/actioned", data={"section": "due"}, follow_redirects=False
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert f"person={membership.id}" in location
    assert "email=2" in location
    assert "Day+0" in location

    page = client.get(location.split("#")[0]).text
    assert "1 of 7 actioned" in page
    assert "Undo actioned" not in page  # Email 2 is selected, not Email 1
    email_one = client.get(f"{_campaign_url(scenario)}?person={membership.id}&email=1").text
    assert "Undo actioned" in email_one


def test_skip_needs_the_confirmation_and_email_one_cannot_be_skipped(
    client: TestClient, db_session: Session, scenario: tuple[Any, ...]
) -> None:
    _ready(db_session, scenario)
    refused = client.post(_desk_url(scenario) + "3/skip", data={}, follow_redirects=False)
    assert "err=" in refused.headers["location"]
    assert db_session.scalar(select(func.count(SequenceEmailAction.id))) == 0

    accepted = client.post(
        _desk_url(scenario) + "3/skip", data={"confirm": "1"}, follow_redirects=False
    )
    assert "ok=" in accepted.headers["location"]
    assert db_session.scalar(select(func.count(SequenceEmailAction.id))) == 1

    first = client.post(
        _desk_url(scenario) + "1/skip", data={"confirm": "1"}, follow_redirects=False
    )
    assert "err=" in first.headers["location"]


def test_editing_on_the_desk_writes_a_new_version_and_keeps_history(
    client: TestClient, db_session: Session, scenario: tuple[Any, ...]
) -> None:
    sequence = _ready(db_session, scenario)
    before = sequence_read.message_rows(db_session, sequence=sequence)[0]
    response = client.post(
        _desk_url(scenario) + "1/edit",
        data={
            "version_id": str(before.version_id),
            "subject": "Edited on the desk",
            "body": "New body text long enough.",
        },
        follow_redirects=False,
    )
    assert "ok=" in response.headers["location"]
    after = sequence_read.message_rows(db_session, sequence=sequence)[0]
    assert after.version_id != before.version_id
    assert after.subject == "Edited on the desk"


# ---------------------------------------------------------------------------
# Edit is bound to the person in the path, not to the posted version
# ---------------------------------------------------------------------------
#
# The desk addresses every action as
# ``/app/campaigns/{campaign_id}/desk/{membership_id}/{position}/…`` and posts
# ``version_id`` in the body. The path segments were validated; the body was
# trusted. A form value is chosen by whoever submits it, so trusting it made the
# hidden field the authorization boundary — and a hidden field is not one.
#
# These tests attack the route the way a form value can actually be attacked:
# with a *legitimate, current* version id that simply belongs somewhere else.


def _victim(db: Session) -> Any:
    """A second Campaign, with its own contact and its own live sequence."""

    fixture = build_sequence(db, company_domain="anvil.example", company_name="Anvil Works")
    db.flush()
    return fixture


def _edit(client: TestClient, scenario: tuple[Any, ...], position: int, version_id: Any) -> Any:
    return client.post(
        _desk_url(scenario) + f"{position}/edit",
        data={
            "version_id": str(version_id),
            "subject": "Rewritten by someone else",
            "body": "Body text long enough to pass validation.",
        },
        follow_redirects=False,
    )


def _versions(db: Session) -> int:
    return db.scalar(select(func.count(EmailSequenceMessageVersion.id))) or 0


def test_editing_with_another_campaigns_version_is_refused_and_changes_nothing(
    client: TestClient, db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """The whole point: a real id, current, and not this caller's to touch.

    Campaign B's message 1 is a perfectly valid version — it exists, it is not
    superseded, and its own sequence is live — so every check
    ``sequence_review.edit_message`` performs would have passed. What it cannot
    know is that the caller asked for Campaign A. Without the binding, this post
    supersedes B's text, invalidates any approval on it, and writes an edit
    against a Campaign the caller never opened.
    """

    _ready(db_session, scenario)
    victim = _victim(db_session)
    target = victim.versions[0]
    subject_before, body_before = target.subject, target.body
    count_before = _versions(db_session)

    response = _edit(client, scenario, 1, target.id)

    assert response.status_code == 303
    assert "err=" in response.headers["location"]
    assert "ok=" not in response.headers["location"]
    # Nothing was written: no new version anywhere, and B's text is untouched.
    assert _versions(db_session) == count_before
    db_session.refresh(target)
    assert (target.subject, target.body) == (subject_before, body_before)
    assert target.superseded_at is None


def test_editing_with_another_persons_version_in_the_same_campaign_is_refused(
    client: TestClient, db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """Same Campaign is not the same person.

    A membership check alone would pass this: both people are in Campaigns the
    caller may open. The binding is to the ``CampaignContact`` named in the path,
    which is the only thing that makes "this person's email" mean anything.
    """

    _ready(db_session, scenario)
    membership = _membership(scenario)
    neighbour = build_sequence(db_session, company_domain="forge.example", company_name="Forge Ltd")
    # Move the neighbour's whole sequence into the caller's own Campaign, so the
    # only thing separating the two is the person.
    neighbour.membership.campaign_id = membership.campaign_id
    neighbour.sequence.campaign_id = membership.campaign_id
    db_session.flush()
    target = neighbour.versions[0]
    count_before = _versions(db_session)

    response = _edit(client, scenario, 1, target.id)

    assert "err=" in response.headers["location"]
    assert _versions(db_session) == count_before
    db_session.refresh(target)
    assert target.superseded_at is None


def test_editing_with_the_right_version_at_the_wrong_position_is_refused(
    client: TestClient, db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """The position in the path is part of the selection, so it is part of the check.

    Message 3's version posted to the message-1 URL is the caller's own data, in
    the caller's own Campaign — and still not what the page said they were
    editing. Accepting it would let the redirect land them back on email 1 while
    email 3 quietly changed underneath.
    """

    sequence = _ready(db_session, scenario)
    rows = sequence_read.message_rows(db_session, sequence=sequence)
    third = rows[2]
    count_before = _versions(db_session)

    response = _edit(client, scenario, 1, third.version_id)

    assert "err=" in response.headers["location"]
    assert _versions(db_session) == count_before
    unchanged = sequence_read.message_rows(db_session, sequence=sequence)[2]
    assert unchanged.version_id == third.version_id


def test_editing_a_superseded_version_is_refused(
    client: TestClient, db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """A stale id from a page that was left open, replayed after a newer edit."""

    sequence = _ready(db_session, scenario)
    stale = sequence_read.message_rows(db_session, sequence=sequence)[0].version_id
    sequence_review.edit_message(
        db_session,
        message_version_id=stale,
        subject="The current text",
        body="The current body, long enough.",
        actor="someone@example.com",
    )
    current = sequence_read.message_rows(db_session, sequence=sequence)[0]
    assert current.version_id != stale
    count_before = _versions(db_session)

    response = _edit(client, scenario, 1, stale)

    assert "err=" in response.headers["location"]
    assert _versions(db_session) == count_before
    after = sequence_read.message_rows(db_session, sequence=sequence)[0]
    assert after.version_id == current.version_id
    assert after.subject == "The current text"


def test_a_version_id_that_is_not_a_uuid_is_refused_without_reaching_the_service(
    client: TestClient, db_session: Session, scenario: tuple[Any, ...]
) -> None:
    _ready(db_session, scenario)
    count_before = _versions(db_session)

    response = _edit(client, scenario, 1, "not-a-uuid")

    assert "err=" in response.headers["location"]
    assert _versions(db_session) == count_before


def test_the_legitimate_edit_on_this_persons_own_current_version_still_works(
    client: TestClient, db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """Anti-over-correction: the binding must not refuse the ordinary case.

    Built with a second Campaign present, because a check that accidentally
    keyed on "the only sequence in the database" would pass without it.
    """

    sequence = _ready(db_session, scenario)
    _victim(db_session)
    before = sequence_read.message_rows(db_session, sequence=sequence)[0]

    response = _edit(client, scenario, 1, before.version_id)

    assert "ok=" in response.headers["location"]
    after = sequence_read.message_rows(db_session, sequence=sequence)[0]
    assert after.version_id != before.version_id
    assert after.subject == "Rewritten by someone else"


def test_a_legacy_review_link_lands_inside_the_campaign(
    client: TestClient, db_session: Session, scenario: tuple[Any, ...]
) -> None:
    sequence = _ready(db_session, scenario)
    response = client.get(f"/app/review?sequence={sequence.id}", follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"].startswith("/app/people/")


# ---------------------------------------------------------------------------
# Today
# ---------------------------------------------------------------------------


def test_today_offers_ready_first_emails_and_returns_to_the_campaign(
    client: TestClient, db_session: Session, scenario: tuple[Any, ...]
) -> None:
    _ready(db_session, scenario)
    membership = _membership(scenario)
    body = client.get("/app").text
    assert "Ready for first email" in body
    assert "Open Campaign" in body
    assert f"person={membership.id}" in body
    for forbidden in ("Notifications", "things want you", "Needs you"):
        assert forbidden not in body


def test_today_groups_due_follow_ups_by_campaign(
    db_session: Session, scenario: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready(db_session, scenario)
    membership = _membership(scenario)
    campaign = scenario[0]
    email_progress.mark_actioned(db_session, membership_id=membership.id, position=1, actor="a")
    real_today = email_progress.local_today()
    monkeypatch.setattr(email_progress, "local_today", lambda: real_today + timedelta(days=4))

    view = today.build(db_session, campaigns=[campaign], user_id=None, kb_on=False)
    assert len(view.due) == 1
    card = view.due[0]
    assert card.due == 1
    assert card.overdue == 1
    assert card.next_position == 2
    assert card.open_url.startswith(f"/app/campaigns/{campaign.id}?person={membership.id}")
    assert view.first == []


def test_dismiss_hides_the_card_for_one_user_and_changes_no_email(
    db_session: Session, scenario: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    import uuid

    from app.models.enums import UserRole, UserState
    from app.models.user import User

    _ready(db_session, scenario)
    membership = _membership(scenario)
    campaign = scenario[0]
    user = User(
        id=uuid.uuid4(),
        email="me@example.com",
        email_normalized="me@example.com",
        role=UserRole.USER,
        state=UserState.ACTIVE,
    )
    db_session.add(user)
    db_session.flush()
    email_progress.mark_actioned(db_session, membership_id=membership.id, position=1, actor="a")
    real_today = email_progress.local_today()
    monkeypatch.setattr(email_progress, "local_today", lambda: real_today + timedelta(days=3))

    before = today.build(db_session, campaigns=[campaign], user_id=user.id, kb_on=False)
    assert len(before.due) == 1
    today.dismiss(
        db_session, user_id=user.id, campaign_id=campaign.id, day=email_progress.local_today()
    )

    mine = today.build(db_session, campaigns=[campaign], user_id=user.id, kb_on=False)
    assert mine.due == [] and mine.dismissed == 1
    someone_else = today.build(db_session, campaigns=[campaign], user_id=None, kb_on=False)
    assert len(someone_else.due) == 1
    # Shared state is untouched: the email is still due.
    assert db_session.scalar(select(func.count(SequenceEmailAction.id))) == 1


def test_dismissing_over_http_needs_a_signed_in_account(
    client: TestClient, db_session: Session, scenario: tuple[Any, ...]
) -> None:
    campaign = scenario[0]
    response = client.post(
        "/app/today/dismiss", data={"campaign_id": str(campaign.id)}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "err=" in response.headers["location"]
