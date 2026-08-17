"""Beta 1 operator UI: Campaign -> person -> the sending desk, copy, and edit.

Three things these tests exist to protect, none of which the sequence suite
already covers.

**Every one of the seven messages is readable, copyable and editable.** The
person page carries no email bodies any more: each Campaign row points into the
inline sending desk on Campaign Overview, which renders one email at a time
(``?person=<membership>&email=<n>``). So where these tests once read seven
bodies off one page they now open all seven emails, and each must be complete
-- a desk that silently dropped one would fail here and nowhere else.

**Copy must give back the exact email.** The imported-value boundary and the
message text pull in opposite directions here, and both directions are asserted:
a formula-shaped *contact name* must be neutralized wherever it is displayed,
and a message *subject or body* must not be, because that text is the email an
operator is about to send. Getting this backwards is silent -- the page still
renders, and the corruption only appears in somebody's inbox.

**Clipboard behaviour is not claimed.** There is no browser harness in this
repository, so nothing here asserts that `navigator.clipboard` was called. What
is asserted is the markup contract `sequence.js` depends on: the buttons, their
types, the id targets they name, and the live region they report through. The
clipboard itself is verified by hand and recorded as such in the handoff.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from app.api.deps import get_db
from app.core.config import get_settings
from app.main import create_app
from app.models.audit_event import AuditEvent
from app.models.campaign import CampaignContact
from app.models.contact import Contact
from app.models.email_sequence import (
    SEQUENCE_LENGTH,
    EmailSequenceMessageReview,
    EmailSequenceMessageVersion,
)
from app.models.enums import SequenceMessageOrigin
from app.models.imported_email import ImportedContactEmail
from app.services.imports import campaign_import
from app.services.personalization import policy as personalization_policy
from app.services.sequences import persistence as sequence_persistence
from app.services.sequences import read as sequence_read
from app.services.sequences import review as sequence_review
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tests import apollo_factory as af
from tests.test_campaign_import_final_review import _live_formulas
from tests.test_customer_operating_model import _ready, _walk_to_personalization
from tests.test_email_sequence import BODIES, SUBJECTS, CountingThinker, build, sequence_payload
from tests.test_email_sequence import scenario as _scenario

scenario = _scenario

#: The ratified ladder. Asserted as the rendered day labels rather than as the
#: constant, so a cadence change that never reaches the page still fails.
ELAPSED_DAYS: tuple[int, ...] = (0, 3, 7, 12, 18, 25, 35)

STATIC = Path(__file__).resolve().parents[1] / "app" / "web" / "static"


def _client(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    *,
    sequences: bool = True,
    imports: bool = False,
) -> TestClient:
    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__AGENT_WORKBENCH", "true")
    if sequences:
        monkeypatch.setenv("FEATURES__EMAIL_SEQUENCES", "true")
    else:
        monkeypatch.delenv("FEATURES__EMAIL_SEQUENCES", raising=False)
    if imports:
        monkeypatch.setenv("FEATURES__CSV_IMPORT", "true")
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
def import_client(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    with _client(db_session, monkeypatch, imports=True) as app_client:
        yield app_client
    get_settings.cache_clear()


def _contact_url(scenario: tuple[Any, ...]) -> str:
    _campaign, _company, contact, membership, _policy, _evidence = scenario
    return f"/app/people/{contact.id}?campaign={membership.campaign_id}"


def _campaign_url(scenario: tuple[Any, ...]) -> str:
    _campaign, _company, _contact, membership, _policy, _evidence = scenario
    return f"/app/campaigns/{membership.campaign_id}"


def _desk_url(scenario: tuple[Any, ...], email: int) -> str:
    """Campaign Overview with the inline sending desk open on one email."""

    _campaign, _company, _contact, membership, _policy, _evidence = scenario
    return f"{_campaign_url(scenario)}?section=all&person={membership.id}&email={email}"


def _ready_with(db: Session, scenario: tuple[Any, ...], **kwargs: Any) -> Any:
    """Build a sequence from a custom payload and walk the person to Ready for Sending."""

    sequence = build(db, scenario, **kwargs)
    _walk_to_personalization(db, scenario[3])
    return sequence


def _doc(body: str) -> str:
    """The one document card the desk renders for the selected email."""

    start = body.index('<article class="v2-doc"')
    end = body.index("</article>", start)
    return body[start:end]


def _rows(db: Session, sequence: Any) -> tuple[sequence_read.MessageRow, ...]:
    return sequence_read.message_rows(db, sequence=sequence)


# ===========================================================================
# A. Roster sequence presence (service projection) and Campaign <-> person links
# ===========================================================================
#
# The Campaign page no longer renders a per-row sequence cell — the workspace
# reports the three preparation outcomes instead — so the roster projection is
# asserted at the service boundary, where the vocabulary is fixed.


def _roster(db: Session, scenario: tuple[Any, ...]) -> sequence_read.SequenceRosterState | None:
    _campaign, _company, _contact, membership, _policy, _evidence = scenario
    return sequence_read.roster_states(db, campaign_id=membership.campaign_id).get(membership.id)


def test_the_roster_projection_has_no_entry_before_a_sequence_is_written(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    assert _roster(db_session, scenario) is None
    assert sequence_read.ROSTER_NO_SEQUENCE == "No sequence yet"


def test_the_roster_projection_reports_a_complete_sequence_as_seven_of_seven(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    build(db_session, scenario)
    state = _roster(db_session, scenario)
    assert state is not None and state.complete
    assert state.label == f"{SEQUENCE_LENGTH} of {SEQUENCE_LENGTH}"
    # Presence, not pressure: the label is a fact, never an instruction.
    for pressure in ("Waiting", "Needs approval", "Approve"):
        assert pressure not in state.label


def test_the_roster_projection_counts_edited_messages(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    sequence = build(db_session, scenario)
    rows = _rows(db_session, sequence)
    sequence_review.edit_message(
        db_session,
        message_version_id=rows[2].version_id,
        subject="I rewrote this subject",
        body="I rewrote this body entirely.",
    )
    state = _roster(db_session, scenario)
    assert state is not None and state.edited == 1


def test_the_roster_projection_reports_a_partial_sequence_as_partial(
    db_session: Session, scenario: tuple[Any, ...]
) -> None:
    """A sequence that lost a message must not read as complete.

    Superseding one current version without writing a replacement is the only
    way to reach this state from the outside, and it is exactly the state a
    naive `count == 7` would mislabel.
    """

    sequence = build(db_session, scenario)
    rows = _rows(db_session, sequence)
    version = db_session.get(EmailSequenceMessageVersion, rows[6].version_id)
    assert version is not None
    version.superseded_at = sequence.created_at
    db_session.flush()

    state = _roster(db_session, scenario)
    assert state is not None and not state.complete
    assert state.label == f"Partial — 6 of {SEQUENCE_LENGTH}"


def test_the_campaign_people_tab_links_to_the_person_and_back(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """Campaign -> person and person -> the desk inside the Campaign, both directions."""

    _campaign, _company, contact, membership, _policy, _evidence = scenario
    _ready(db_session, scenario)
    db_session.commit()

    roster = client.get(f"{_campaign_url(scenario)}/people").text
    assert f'href="/app/people/{contact.id}?campaign={membership.campaign_id}"' in roster

    contact_page = client.get(_contact_url(scenario)).text
    # "Open in Campaign" lands on the desk for this membership; the query
    # ampersand is HTML-escaped inside the attribute, as it should be.
    assert (
        f'href="/app/campaigns/{membership.campaign_id}'
        f'?section=all&amp;person={membership.id}#ready"'
    ) in contact_page
    assert "Open in Campaign" in contact_page


# ===========================================================================
# B. The sending desk renders the whole sequence, one email per request
# ===========================================================================


def test_all_seven_messages_render_in_full_on_the_desk(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """The core Beta 1 outcome, asserted as seven complete bodies.

    The desk shows one email at a time, so this opens all seven and asserts
    each one's own subject and complete body inside its document card.
    """

    _ready(db_session, scenario)
    db_session.commit()

    for position in range(1, SEQUENCE_LENGTH + 1):
        body = client.get(_desk_url(scenario, position)).text
        assert 'class="v2-desk"' in body
        doc = _doc(body)
        assert f'id="desk-subject-{position}"' in doc
        assert f'id="desk-body-{position}"' in doc
        assert SUBJECTS[position - 1] in doc, f"subject {position} missing"
        # The *complete* body, not a truncated excerpt.
        assert BODIES[position - 1] in doc, f"body {position} missing or truncated"


def test_the_desk_renders_the_documented_cadence(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """The seven-step rail carries the ratified ladder as day labels."""

    _ready(db_session, scenario)
    db_session.commit()
    body = client.get(_desk_url(scenario, 1)).text
    assert body.count('class="v2-rail-step') == SEQUENCE_LENGTH
    for position, day in enumerate(ELAPSED_DAYS, start=1):
        assert f"Email {position}" in body
        assert f"Day {day}" in body, f"elapsed day {day} not rendered"


def test_the_desk_is_complete_with_zero_review_rows(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """The state every generated sequence is in: readable with nobody having clicked."""

    _ready(db_session, scenario)
    db_session.commit()
    assert db_session.scalar(select(func.count(EmailSequenceMessageReview.id))) == 0

    for position in range(1, SEQUENCE_LENGTH + 1):
        body = client.get(_desk_url(scenario, position)).text
        for pressure in ("Waiting for you", "Needs approval", "Approve before proceeding"):
            assert pressure not in body
        # Every message is fully readable without any review having happened.
        assert BODIES[position - 1] in _doc(body)


def test_edited_and_regenerated_are_each_visible(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """An operator's edit and a regeneration each show as what they are.

    The desk carries no review decisions, so a discarded message has no
    customer-side rendering any more; the two origins that do are asserted.
    """

    sequence = build(db_session, scenario)
    rows = _rows(db_session, sequence)
    sequence_review.edit_message(
        db_session,
        message_version_id=rows[1].version_id,
        subject="Edited subject here",
        body="Edited body here, entirely different from what came before.",
    )
    _walk_to_personalization(db_session, scenario[3])
    db_session.commit()

    doc = _doc(client.get(_desk_url(scenario, 2)).text)
    assert "Edited by you" in doc
    assert "Edited subject here" in doc
    assert "Human edited" in doc  # the History disclosure names the origin
    assert "Generated" in doc  # ... and keeps the replaced version underneath

    # Regenerated is the other origin. A regeneration supersedes the sequence,
    # so it is asserted on the successor's desk.
    regenerated = build(db_session, scenario)
    db_session.commit()
    assert regenerated.id != sequence.id
    fresh = _doc(client.get(_desk_url(scenario, 1)).text)
    assert "Regenerated" in fresh


def test_the_desk_never_says_anything_was_sent(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """The desk offers Copy, Mark actioned and Edit; it never implies a send.

    With the Gmail feature off (as it is here) no draft action is offered
    either. "Ready to send" is the desk's own vocabulary for Email 1 before Day
    0 and is a state, not a claim about the past, so it is not forbidden.
    """

    _ready(db_session, scenario)
    db_session.commit()
    for position in range(1, SEQUENCE_LENGTH + 1):
        body = client.get(_desk_url(scenario, position)).text
        assert "Nothing here sends" in body
        assert "Create Gmail draft" not in body
        assert "gmail-draft" not in body
        for claim in ("has been sent", "will be sent", "scheduled to send", "was sent"):
            assert claim not in body


# ===========================================================================
# C. The copy markup contract
#
# `sequence.js` reads the rendered nodes named by these attributes. None of
# this proves the clipboard works -- there is no browser here -- it proves the
# contract the script depends on is present and points at the right text.
# ===========================================================================


def test_each_email_carries_one_copy_button_targeting_its_own_text(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """One Copy per email, naming the subject and body nodes of that email only."""

    _ready(db_session, scenario)
    db_session.commit()

    for position in range(1, SEQUENCE_LENGTH + 1):
        doc = _doc(client.get(_desk_url(scenario, position)).text)
        subject_id = f"desk-subject-{position}"
        body_id = f"desk-body-{position}"

        # The nodes the script reads exist, and hold this message's own text.
        assert f'id="{subject_id}"' in doc
        assert f'id="{body_id}"' in doc
        assert SUBJECTS[position - 1] in doc
        assert BODIES[position - 1] in doc

        assert (
            f'data-copy="full" data-copy-subject="{subject_id}" data-copy-body="{body_id}"' in doc
        )
        assert ">Copy<" in doc


def test_every_copy_control_is_a_plain_button(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """`type="button"` throughout: focusable and Enter/Space-activated for free.

    A copy control that is an anchor, or a submit button, either navigates or
    posts the edit form. Both are worse than not copying.
    """

    _ready(db_session, scenario)
    db_session.commit()

    for position in range(1, SEQUENCE_LENGTH + 1):
        body = client.get(_desk_url(scenario, position)).text
        controls = re.findall(r"<(\w+)([^>]*\bdata-copy=[^>]*)>", body)
        assert len(controls) == 1, f"email {position} rendered {len(controls)} copy controls"
        for tag, attributes in controls:
            assert tag == "button", f"copy control rendered as <{tag}>"
            assert 'type="button"' in attributes


def test_the_desk_carries_exactly_one_polite_live_region(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """One region for the page, not one per control."""

    _ready(db_session, scenario)
    db_session.commit()
    for position in range(1, SEQUENCE_LENGTH + 1):
        body = client.get(_desk_url(scenario, position)).text
        assert body.count('id="seq-copy-status"') == 1
        assert body.count('aria-live="polite"') == 1


def test_each_email_carries_the_identity_a_gmail_draft_would_need(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """Every desk action names the Campaign, the membership, the position and the exact version.

    A Gmail draft (when that feature is on) is created for one exact message
    version, through the same shape of route. With Gmail off, as here, the edit
    form is the action that carries that identity, and it must be exact.
    """

    _campaign, _company, _contact, membership, _policy, _evidence = scenario
    sequence = _ready(db_session, scenario)
    rows = _rows(db_session, sequence)
    db_session.commit()

    for row in rows:
        doc = _doc(client.get(_desk_url(scenario, row.position)).text)
        base = f"/app/campaigns/{membership.campaign_id}/desk/{membership.id}/{row.position}"
        assert f'action="{base}/edit"' in doc
        assert f'name="version_id" value="{row.version_id}"' in doc
        # No other message's version leaks into this email's card.
        for other in rows:
            if other.position != row.position:
                assert str(other.version_id) not in doc


def test_the_desk_runs_no_inline_script(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """`script-src 'self'` with no nonce: an inline handler would not run."""

    _ready(db_session, scenario)
    db_session.commit()
    response = client.get(_desk_url(scenario, 1))
    body = response.text

    assert "<script>" not in body
    for handler in ("onclick=", "onsubmit=", "onchange=", "onload=", "javascript:"):
        assert handler not in body
    # The only scripts on the page are the external, versioned ones.
    scripts = re.findall(r"<script\b[^>]*>", body)
    assert len(scripts) == 2
    assert all("src=" in script for script in scripts)
    assert any("sequence.js" in script for script in scripts)
    assert any("desk.js" in script for script in scripts)


# ===========================================================================
# D. Static asset versioning
# ===========================================================================


@pytest.mark.parametrize("asset", ("v2.css", "live.js", "sequence.js"))
def test_every_versioned_asset_token_is_the_real_content_hash(asset: str) -> None:
    """The token must be derived, never written by hand."""

    from app.web.v2 import routes

    expected = sha256((STATIC / asset).read_bytes()).hexdigest()[:12]
    actual = {
        "v2.css": routes.V2_CSS_VERSION,
        "live.js": routes.LIVE_JS_VERSION,
        "sequence.js": routes.SEQUENCE_JS_VERSION,
    }[asset]
    assert actual == expected


def test_the_rendered_desk_carries_the_real_hashes(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    from app.web.v2 import routes, shell

    _ready(db_session, scenario)
    db_session.commit()
    body = client.get(_desk_url(scenario, 1)).text
    assert f"v2.css?v={routes.V2_CSS_VERSION}" in body
    assert f"sequence.js?v={routes.SEQUENCE_JS_VERSION}" in body
    assert f"desk.js?v={shell.DESK_JS_VERSION}" in body
    assert shell.DESK_JS_VERSION == sha256((STATIC / "desk.js").read_bytes()).hexdigest()[:12]


def test_sequence_pages_are_never_cached(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    build(db_session, scenario)
    db_session.commit()
    for url in (_contact_url(scenario), _campaign_url(scenario)):
        response = client.get(url)
        assert response.headers["cache-control"] == "no-store", url
        assert "script-src 'self'" in response.headers["content-security-policy"], url


# ===========================================================================
# E. Editing, through the Beta 1 page
# ===========================================================================


def test_saving_an_edit_writes_the_next_version_and_no_review_row(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """Every clause of the Beta 1 edit contract, over HTTP."""

    sequence = build(db_session, scenario)
    rows = _rows(db_session, sequence)
    target = rows[3]
    db_session.commit()

    response = client.post(
        f"/app/review/sequence/messages/{target.version_id}/edit",
        data={
            "subject": "A subject I wrote myself",
            "body": "A body I wrote myself, replacing what the Agent produced.",
            "back": _contact_url(scenario) + "#message-4",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert "err=" not in location
    # The flash must survive the fragment rather than land inside it.
    assert "ok=" in location.split("#")[0]
    assert location.endswith("#message-4")

    db_session.expire_all()
    fresh = _rows(db_session, sequence)
    edited = next(row for row in fresh if row.message_id == target.message_id)

    assert edited.message_version == target.message_version + 1
    assert edited.version_id != target.version_id
    assert edited.origin is SequenceMessageOrigin.HUMAN_EDITED
    assert edited.source_version_id == target.version_id
    assert edited.subject == "A subject I wrote myself"

    # Version N is kept, and kept intact.
    previous = db_session.get(EmailSequenceMessageVersion, target.version_id)
    assert previous is not None
    assert previous.subject == SUBJECTS[3]
    assert previous.body == BODIES[3]
    assert previous.superseded_at is not None

    # Option C: an edit is not a review, and must not manufacture one.
    assert db_session.scalar(select(func.count(EmailSequenceMessageReview.id))) == 0
    actions = list(db_session.scalars(select(AuditEvent.action)).all())
    assert "email_sequence_message.edited" in actions
    assert not any(action.endswith(".approved") for action in actions)

    # And the edited message is still approved -- by default, by nobody.
    assert edited.approved is True
    assert edited.decision_origin == "default"


def test_a_hostile_origin_cannot_edit_through_the_contact_page(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    sequence = build(db_session, scenario)
    rows = _rows(db_session, sequence)
    db_session.commit()

    response = client.post(
        f"/app/review/sequence/messages/{rows[0].version_id}/edit",
        data={"subject": "Injected", "body": "From elsewhere.", "back": _contact_url(scenario)},
        headers={"Origin": "https://evil.example"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "err=" in response.headers["location"]

    db_session.expire_all()
    assert (
        db_session.scalar(
            select(func.count(EmailSequenceMessageVersion.id)).where(
                EmailSequenceMessageVersion.message_id == rows[0].message_id
            )
        )
        == 1
    )


def test_a_null_origin_edit_from_our_own_page_still_works(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """`Referrer-Policy: no-referrer` makes our own form posts carry `null`."""

    sequence = build(db_session, scenario)
    rows = _rows(db_session, sequence)
    db_session.commit()

    response = client.post(
        f"/app/review/sequence/messages/{rows[0].version_id}/edit",
        data={
            "subject": "Rewritten by me",
            "body": "My own words here.",
            "back": _contact_url(scenario),
        },
        headers={"Origin": "null"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "err=" not in response.headers["location"]

    db_session.expire_all()
    assert (
        db_session.scalar(
            select(func.count(EmailSequenceMessageVersion.id)).where(
                EmailSequenceMessageVersion.message_id == rows[0].message_id
            )
        )
        == 2
    )


def test_the_edit_form_is_prefilled_with_the_exact_stored_text(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """The prefill used to be neutralized, which persisted the projection.

    Opening the editor on a message whose body legitimately begins with "-" and
    saving would write "'-..." into the next version -- a corruption of the
    email introduced by the act of looking at it.
    """

    # Long enough to clear the generator's own length validation, so what is
    # under test is the projection and not the word count.
    formula_body = "- " + BODIES[0]
    _ready_with(
        db_session,
        scenario,
        payload=sequence_payload(bodies=(formula_body,) + BODIES[1:]),
    )
    db_session.commit()

    article = _doc(client.get(_desk_url(scenario, 1)).text)

    opening = formula_body[:24]
    assert opening in article, "the dashed opening must survive to the page"
    assert f"&#39;{opening}" not in article, "the display must not apostrophe-prefix an email"
    assert f"'{opening}" not in article

    # And the prefill an operator would save carries the same exact text.
    textarea = article[article.index("<textarea") : article.index("</textarea>")]
    assert opening in textarea
    assert "&#39;" not in textarea


# ===========================================================================
# F. The imported-value boundary, in both directions
# ===========================================================================


@pytest.fixture()
def hostile_import(db_session: Session) -> Any:
    """An imported contact whose every visible identity field opens a formula."""

    def _build(prefix: str) -> dict[str, Any]:
        marker = f"{prefix}cmd|"
        # Execution stays off. A *running* sequence campaign whose skippable
        # Agents are disabled now refuses new enrolments outright, because the
        # walk would step every imported contact past Research into a terminal
        # SKIPPED. These tests are about the display boundary, not the pipeline:
        # nothing below asserts on execution, and the sequence they render is
        # generated and persisted directly rather than earned by a walk. So the
        # campaign is left in the state the import is actually allowed to
        # happen in, and the seven-message opt-in — which the contact page does
        # read — is kept.
        campaign = af.make_campaign(db_session, execution=False)
        campaign.cadence_config = {"sequence": {"enabled": True}}
        campaign.name = f"{marker}campaign"
        db_session.flush()
        campaign_import.confirm(
            db_session,
            campaign_id=campaign.id,
            content=af.csv_bytes(
                [
                    af.row(
                        **{
                            "First Name": f"{marker}first",
                            "Last Name": f"{marker}last",
                            "Title": f"{marker}title",
                            "Company Name": f"{marker}company",
                            "Company Name for Emails": f"{marker}coemails",
                        }
                    )
                ]
            ),
            filename="hostile.csv",
        )
        contact = db_session.scalars(select(Contact)).one()
        membership = db_session.scalars(
            select(CampaignContact).where(CampaignContact.campaign_id == campaign.id)
        ).one()
        policy = personalization_policy.ensure_initial_policy(db_session, actor="test")
        from app.services.personalization import sequence as sequence_generation

        generated = sequence_generation.generate_sequence(
            db_session,
            membership=membership,
            policy=policy,
            thinker=CountingThinker(sequence_payload()),
        )
        sequence_persistence.persist_sequence(
            db_session, membership=membership, contact=contact, generated=generated
        )
        db_session.commit()
        return {"campaign": campaign, "contact": contact, "membership": membership}

    return _build


@pytest.mark.parametrize("prefix", ("=", "+", "-", "@"))
def test_the_beta_surfaces_render_no_live_formula_from_imported_identity(
    import_client: TestClient, hostile_import: Any, prefix: str
) -> None:
    """The Campaign roster was the gap. It is on the sweep now."""

    state = hostile_import(prefix)
    needle = f"{prefix}cmd|"
    urls = {
        "campaign_roster": f"/app/campaigns/{state['campaign'].id}/people",
        "contact_sequence": (f"/app/people/{state['contact'].id}?campaign={state['campaign'].id}"),
    }
    for surface, url in urls.items():
        body = import_client.get(url).text
        assert not _live_formulas(body, needle), f"{surface} rendered a live formula"


@pytest.mark.parametrize("prefix", ("=", "+", "-", "@"))
def test_the_roster_sweep_would_fail_without_the_boundary(
    import_client: TestClient,
    hostile_import: Any,
    disabled_display_boundary: Any,
    prefix: str,
) -> None:
    """The mutation proof for the surfaces this build added.

    Without it the sweep above could pass because the hostile value never
    reached the page -- which is exactly how the roster gap survived until now.
    """

    state = hostile_import(prefix)
    needle = f"{prefix}cmd|"
    unprotected = []
    for surface, url in (
        ("campaign_roster", f"/app/campaigns/{state['campaign'].id}/people"),
        (
            "contact_sequence",
            f"/app/people/{state['contact'].id}?campaign={state['campaign'].id}",
        ),
    ):
        if not _live_formulas(import_client.get(url).text, needle):
            unprotected.append(surface)
    assert not unprotected, (
        "with the boundary disabled these surfaces still showed no live formula, "
        f"so their absence assertion proves nothing: {unprotected}"
    )


@pytest.fixture()
def disabled_display_boundary(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Turn the projection into a pass-through, underneath the Jinja filter.

    Patching `env.filters` is not enough: `_sequence.html` is pulled in with
    `{% import %}`, and Jinja binds a macro module's filters at
    module-construction time and caches that module for the life of the
    process. `display.safe_text` resolves `neutralize_formula` from its own
    module globals on every call, so replacing that reaches the boundary
    whichever binding a cached macro is holding.
    """

    from app.services.imports import display

    def _passthrough(value: str | None) -> str | None:
        return value

    monkeypatch.setattr(display, "neutralize_formula", _passthrough)
    return _passthrough


def test_the_message_text_itself_is_never_neutralized(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """The other direction, and the one that would corrupt a real email.

    A subject or body is not an imported cell; it is the message. Prefixing it
    with an apostrophe changes what the operator reads, what they copy, and
    what would be sent.
    """

    subject = "=SUM(A1:A2) is what the analyst asked about"
    # Prefixed onto a body that already clears validation, so the assertion is
    # about the projection rather than about message length.
    body = "+1 on the approach you described. " + BODIES[0]
    sequence = _ready_with(
        db_session,
        scenario,
        payload=sequence_payload(subjects=(subject,) + SUBJECTS[1:], bodies=(body,) + BODIES[1:]),
    )
    rows = _rows(db_session, sequence)
    db_session.commit()

    article = _doc(client.get(_desk_url(scenario, 1)).text)

    # Present exactly, and not apostrophe-prefixed.
    assert "=SUM(A1:A2) is what the analyst asked about" in article
    assert "+1 on the approach you described." in article
    assert "&#39;=SUM(A1:A2)" not in article
    assert "&#39;+1 on the approach" not in article

    # And the stored text is untouched either way.
    stored = db_session.get(EmailSequenceMessageVersion, rows[0].version_id)
    assert stored is not None
    assert stored.subject == subject
    assert stored.body == body


@pytest.mark.parametrize("prefix", ("=", "+", "-", "@"))
def test_imported_evidence_is_kept_verbatim_however_it_is_displayed(
    import_client: TestClient, db_session: Session, hostile_import: Any, prefix: str
) -> None:
    """Neutralization is a projection. The record of what the file said is not."""

    hostile_import(prefix)
    stored = db_session.scalars(select(ImportedContactEmail)).all()
    assert stored, "the import must have recorded its own evidence"
    for row in stored:
        assert not (row.raw_email or "").startswith("'")


# ===========================================================================
# G. No legacy awaiting language
# ===========================================================================


def test_a_sequence_never_borrows_the_legacy_awaiting_language(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """The legacy predicate means the opposite of Option C.

    `DraftApproval.awaiting_decision` is `decision is None` -- absence means
    *waiting*. Under Option C the same absence means *ready*. A desk that read
    the sequence through the legacy predicate would be exactly backwards: with
    no review row anywhere, Email 1 is Ready and nothing is waiting on anyone.
    """

    _ready(db_session, scenario)
    db_session.commit()
    assert db_session.scalar(select(func.count(EmailSequenceMessageReview.id))) == 0
    body = client.get(_desk_url(scenario, 1)).text
    assert "Nobody has approved this version." not in body
    assert "Awaiting" not in body
    assert "Email 1 ready to send" in body
