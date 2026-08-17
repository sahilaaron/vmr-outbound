"""Beta 1 operator UI: Campaign -> Contact -> seven messages, copy, and edit.

Three things these tests exist to protect, none of which the sequence suite
already covers.

**The Contact page is now the whole sequence, not one message at a time.** The
Review queue deliberately renders one body per card because it lists forty
contacts. This page is one contact, and an operator came to read, copy and edit
all seven. If it ever regresses to paging, six of the seven messages become
invisible without any test failing elsewhere.

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
import uuid
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
def client_no_sequences(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    with _client(db_session, monkeypatch, sequences=False) as app_client:
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
    """Campaign -> person and person -> Campaign, both directions."""

    _campaign, _company, contact, membership, _policy, _evidence = scenario
    build(db_session, scenario)
    db_session.commit()

    roster = client.get(f"{_campaign_url(scenario)}/people").text
    assert f'href="/app/people/{contact.id}?campaign={membership.campaign_id}"' in roster

    contact_page = client.get(_contact_url(scenario)).text
    assert (
        f'href="/app/campaigns/{membership.campaign_id}?section=all&person={membership.id}#ready"'
        in contact_page
    )


# ===========================================================================
# B. The Contact page renders the whole sequence
# ===========================================================================


def test_all_seven_messages_render_in_full_on_one_page(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """The core Beta 1 outcome, asserted as seven complete bodies."""

    build(db_session, scenario)
    db_session.commit()
    body = client.get(_contact_url(scenario)).text

    assert body.count("v2-seq-full") >= SEQUENCE_LENGTH
    for index in range(SEQUENCE_LENGTH):
        assert SUBJECTS[index] in body, f"subject {index + 1} missing"
        # The *complete* body, not a truncated excerpt.
        assert BODIES[index] in body, f"body {index + 1} missing or truncated"


def test_the_page_renders_the_documented_cadence(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    build(db_session, scenario)
    db_session.commit()
    body = client.get(_contact_url(scenario)).text
    for day in ELAPSED_DAYS:
        assert f"Day {day} —" in body, f"elapsed day {day} not rendered"


def test_the_page_is_complete_with_zero_review_rows(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """The state every generated sequence is in, and the one Option C governs."""

    build(db_session, scenario)
    db_session.commit()
    assert db_session.scalar(select(func.count(EmailSequenceMessageReview.id))) == 0

    body = client.get(_contact_url(scenario)).text
    assert body.count("approved by default") >= SEQUENCE_LENGTH
    assert "approved by you" not in body
    for pressure in ("Waiting for you", "Needs approval", "Approve before proceeding"):
        assert pressure not in body
    # Every message is still fully readable.
    for index in range(SEQUENCE_LENGTH):
        assert BODIES[index] in body


def test_a_human_confirmation_is_distinct_from_the_default(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    sequence = build(db_session, scenario)
    rows = _rows(db_session, sequence)
    sequence_review.approve_message(db_session, message_version_id=rows[0].version_id)
    db_session.commit()

    body = client.get(_contact_url(scenario)).text
    assert "approved by you" in body
    # The other six are still defaults, and still say so.
    assert body.count("approved by default") >= SEQUENCE_LENGTH - 1


def test_edited_regenerated_and_discarded_are_each_visible(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    sequence = build(db_session, scenario)
    rows = _rows(db_session, sequence)
    sequence_review.edit_message(
        db_session,
        message_version_id=rows[1].version_id,
        subject="Edited subject here",
        body="Edited body here, entirely different from what came before.",
    )
    sequence_review.discard_message(db_session, message_version_id=rows[4].version_id)
    db_session.commit()

    body = client.get(_contact_url(scenario)).text
    assert "human-edited" in body
    assert "discarded" in body
    assert 'data-origin="human_edited"' in body

    # Regenerated is the third origin. A regeneration supersedes the sequence,
    # so it is asserted on the successor's page.
    regenerated = build(db_session, scenario)
    db_session.commit()
    assert regenerated.id != sequence.id
    fresh = client.get(_contact_url(scenario)).text
    assert 'data-origin="regenerated"' in fresh or "regenerated" in fresh


def test_the_page_never_says_approved_means_sent(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    build(db_session, scenario)
    db_session.commit()
    body = client.get(_contact_url(scenario)).text
    assert "no sending path in this build" in body
    # The From line still says no sending account is connected. The wording
    # changed with #267 -- it used to say one *could not* be connected, which
    # stopped being true once a Gmail mailbox could be -- but the claim the test
    # exists to pin is unchanged: this page never implies anything was sent, and
    # with the Gmail feature off (as it is here) no mailbox is connected either.
    assert "no sending account is connected" in body
    assert "Create Gmail drafts" not in body
    for claim in ("has been sent", "will be sent", "ready to send", "scheduled to send"):
        assert claim not in body


# ===========================================================================
# C. The copy markup contract
#
# `sequence.js` reads the rendered nodes named by these attributes. None of
# this proves the clipboard works -- there is no browser here -- it proves the
# contract the script depends on is present and points at the right text.
# ===========================================================================


def _article_for(body: str, version_id: uuid.UUID) -> str:
    """The one message element carrying this exact version id."""

    marker = f'data-version-id="{version_id}"'
    start = body.index(marker)
    opening = body.rindex("<article", 0, start)
    end = body.index("</article>", start)
    return body[opening:end]


def test_each_message_carries_three_copy_buttons_targeting_its_own_text(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    sequence = build(db_session, scenario)
    rows = _rows(db_session, sequence)
    db_session.commit()
    body = client.get(_contact_url(scenario)).text

    for row in rows:
        article = _article_for(body, row.version_id)
        subject_id = f"seq-subj-{row.version_id}"
        body_id = f"seq-body-{row.version_id}"

        # The nodes the script reads exist, and hold this message's own text.
        assert f'id="{subject_id}"' in article
        assert f'id="{body_id}"' in article

        assert f'data-copy="subject" data-copy-subject="{subject_id}"' in article
        assert f'data-copy="body" data-copy-body="{body_id}"' in article
        assert (
            f'data-copy="full" data-copy-subject="{subject_id}" data-copy-body="{body_id}"'
            in article
        )
        assert "Copy Subject" in article
        assert "Copy Body" in article
        assert "Copy Full Email" in article


def test_every_copy_control_is_a_plain_button(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """`type="button"` throughout: focusable and Enter/Space-activated for free.

    A copy control that is an anchor, or a submit button, either navigates or
    posts the edit form. Both are worse than not copying.
    """

    build(db_session, scenario)
    db_session.commit()
    body = client.get(_contact_url(scenario)).text

    controls = re.findall(r"<(\w+)([^>]*\bdata-copy=[^>]*)>", body)
    assert len(controls) == SEQUENCE_LENGTH * 3
    for tag, attributes in controls:
        assert tag == "button", f"copy control rendered as <{tag}>"
        assert 'type="button"' in attributes


def test_the_page_carries_exactly_one_polite_live_region(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """One region for the page, not one per button.

    Seven messages times three buttons is twenty-one controls; a live region
    each would queue twenty-one announcements at a screen reader.
    """

    build(db_session, scenario)
    db_session.commit()
    body = client.get(_contact_url(scenario)).text
    assert body.count('id="seq-copy-status"') == 1
    assert body.count('aria-live="polite"') == 1


def test_each_message_carries_the_identity_a_gmail_draft_would_need(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """Gmail is not built. Naming one exact version later must not need a redesign."""

    _campaign, _company, contact, membership, _policy, _evidence = scenario
    sequence = build(db_session, scenario)
    summary = sequence_read.summary(db_session, sequence=sequence)
    rows = _rows(db_session, sequence)
    db_session.commit()
    body = client.get(_contact_url(scenario)).text

    for row in rows:
        article = _article_for(body, row.version_id)
        for attribute, value in (
            ("data-campaign-id", membership.campaign_id),
            ("data-contact-id", contact.id),
            ("data-campaign-contact-id", summary.campaign_contact_id),
            ("data-sequence-id", summary.sequence_id),
            ("data-sequence-key", summary.sequence_key),
            ("data-message-id", row.message_id),
            ("data-version-id", row.version_id),
        ):
            assert f'{attribute}="{value}"' in article, f"{attribute} missing from message"


def test_the_contact_page_runs_no_inline_script(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """`script-src 'self'` with no nonce: an inline handler would not run."""

    build(db_session, scenario)
    db_session.commit()
    response = client.get(_contact_url(scenario))
    body = response.text

    assert "<script>" not in body
    for handler in ("onclick=", "onsubmit=", "onchange=", "onload=", "javascript:"):
        assert handler not in body
    # The only script on the page is the external, versioned one.
    scripts = re.findall(r"<script\b[^>]*>", body)
    assert len(scripts) == 1
    assert "sequence.js" in scripts[0]
    assert "src=" in scripts[0]


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


def test_the_rendered_page_carries_the_real_hashes(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    from app.web.v2 import routes

    build(db_session, scenario)
    db_session.commit()
    body = client.get(_contact_url(scenario)).text
    assert f"v2.css?v={routes.V2_CSS_VERSION}" in body
    assert f"sequence.js?v={routes.SEQUENCE_JS_VERSION}" in body


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
    sequence = build(
        db_session,
        scenario,
        payload=sequence_payload(bodies=(formula_body,) + BODIES[1:]),
    )
    rows = _rows(db_session, sequence)
    db_session.commit()

    body = client.get(_contact_url(scenario)).text
    article = _article_for(body, rows[0].version_id)

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
    sequence = build(
        db_session,
        scenario,
        payload=sequence_payload(subjects=(subject,) + SUBJECTS[1:], bodies=(body,) + BODIES[1:]),
    )
    rows = _rows(db_session, sequence)
    db_session.commit()

    page = client.get(_contact_url(scenario)).text
    article = _article_for(page, rows[0].version_id)

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
# G. Legacy draft coexistence
# ===========================================================================


def test_a_contact_without_a_sequence_keeps_the_legacy_draft_view(
    db_session: Session, client_no_sequences: TestClient, scenario: tuple[Any, ...]
) -> None:
    """SEQ and DraftVersion coexist. No sequence must not mean no page."""

    _campaign, _company, contact, membership, _policy, _evidence = scenario
    db_session.commit()
    response = client_no_sequences.get(
        f"/app/people/{contact.id}?campaign={membership.campaign_id}"
    )
    assert response.status_code == 200
    body = response.text
    # No sequence, no sequence UI, and no invented one. The page is still the
    # contact's page: the Agent ledger and the legacy draft card remain.
    assert "v2-seq-full" not in body
    assert "Every Agent that touched this contact" in body
    assert "The seven-message sequence" not in body


def test_a_sequence_never_borrows_the_legacy_awaiting_language(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """The legacy predicate means the opposite of Option C.

    `DraftApproval.awaiting_decision` is `decision is None` -- absence means
    *waiting*. Under Option C the same absence means *approved*. A page that
    read the sequence through the legacy predicate would be exactly backwards.
    """

    build(db_session, scenario)
    db_session.commit()
    body = client.get(_contact_url(scenario)).text
    assert "Nobody has approved this version." not in body
    assert "approved by default" in body
