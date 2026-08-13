"""Copy controls on ``/app/review``, the surface people actually review from.

Copy Subject / Copy Body / Copy Full Email existed on the contact page and not
on the Review queue, so an operator reviewing a sequence had to open a second
page to get the text out. These tests cover the markup contract that closes
that gap, and the three things it was not allowed to disturb on the way.

**One body at a time survives.** The queue is a list. It lists all seven
positions in a selector and renders exactly one body, and that is a decision,
not an oversight — seven full bodies per contact is unusable at any real volume.
Adding copy controls must not have been an excuse to expand the card, so the
one-body rule is asserted here as well as in the sequence suite.

**Copying is not a decision.** The controls sit outside the approve/discard form
and outside the edit ``<details>``, and every one of them is
``type="button"``. A copy control that submits is worse than one that does not
copy: it records a review decision the operator never made.

**Clipboard behaviour is not claimed.** There is no browser in this repository,
so nothing here asserts that ``navigator.clipboard`` was called. What is
asserted is the contract ``sequence.js`` depends on — the buttons, their types,
the ids they name, the live region they report through, and the fact that the
script is reachable at all under a ``script-src 'self'`` policy with no nonce.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest
from app.api.deps import get_db
from app.core.config import get_settings
from app.main import create_app
from app.models.audit_event import AuditEvent
from app.models.email_sequence import SEQUENCE_LENGTH
from app.services.sequences import read as sequence_read
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tests.test_email_sequence import build
from tests.test_email_sequence import scenario as _scenario

scenario = _scenario

#: Every position the selector offers, as the URL names them.
STEPS: tuple[int, ...] = tuple(range(1, SEQUENCE_LENGTH + 1))


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
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def _open(client: TestClient, sequence_id: Any, step: int) -> str:
    response = client.get(f"/app/review?sequence={sequence_id}&step={step}")
    assert response.status_code == 200
    return response.text


# ===========================================================================
# A. The copy markup contract
# ===========================================================================


def test_the_opened_message_carries_three_copy_buttons_naming_its_own_text(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """The ids are keyed on the version id, matching the contact page.

    Two messages rendered on one page must never be able to target each other's
    nodes — the failure would be silent and would put the wrong email in
    somebody's clipboard. The queue renders one message at a time, so the
    strongest available form of that assertion is used here: the opened
    message's ids are present and every other position's ids are absent.
    """

    sequence = build(db_session, scenario)
    rows = sequence_read.message_rows(db_session, sequence=sequence)
    db_session.commit()

    body = _open(client, sequence.id, 3)
    opened = rows[2]
    subject_id = f"seq-subj-{opened.version_id}"
    body_id = f"seq-body-{opened.version_id}"

    assert f'id="{subject_id}"' in body
    assert f'id="{body_id}"' in body
    assert f'data-copy="subject" data-copy-subject="{subject_id}"' in body
    assert f'data-copy="body" data-copy-body="{body_id}"' in body
    assert f'data-copy="full" data-copy-subject="{subject_id}" data-copy-body="{body_id}"' in body
    assert "Copy Subject" in body
    assert "Copy Body" in body
    assert "Copy Full Email" in body

    for other in rows:
        if other.version_id == opened.version_id:
            continue
        assert f"seq-subj-{other.version_id}" not in body
        assert f"seq-body-{other.version_id}" not in body


def test_every_copy_control_on_the_review_page_is_a_plain_button(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """``type="button"`` throughout, because the card contains a form.

    The approve and discard actions are a POST form on this page. A copy control
    that is a submit button — the default for ``<button>`` inside a form — would
    record a review decision every time somebody copied a subject line. An
    anchor would navigate instead. Both are worse than not copying.
    """

    sequence = build(db_session, scenario)
    db_session.commit()
    body = _open(client, sequence.id, 1)

    found = re.findall(r"<(\w+)([^>]*\bdata-copy=[^>]*)>", body)
    # One opened message, three controls — and never twenty-one, which is what
    # an expanded card would produce.
    assert len(found) == 3
    for tag, attributes in found:
        assert tag == "button", f"copy control rendered as <{tag}>"
        assert 'type="button"' in attributes


def test_the_review_page_carries_exactly_one_polite_live_region(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """One region for the page, not one per control.

    ``announce()`` no-ops when it cannot find the region, so without it the
    controls would copy and say nothing to a screen reader. More than one would
    be worse than none: duplicated regions queue duplicate announcements.
    """

    sequence = build(db_session, scenario)
    db_session.commit()
    body = _open(client, sequence.id, 1)

    assert body.count('id="seq-copy-status"') == 1
    assert body.count('aria-live="polite"') == 1
    region = re.search(r"<div[^>]*id=\"seq-copy-status\"[^>]*>", body)
    assert region is not None
    assert 'role="status"' in region.group(0)
    assert 'aria-live="polite"' in region.group(0)


def test_the_review_page_loads_the_shared_script_and_runs_nothing_inline(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """The deployed CSP is ``script-src 'self'`` with no nonce.

    An inline ``<script>`` or an ``onclick=`` would be blocked outright, so the
    controls would render and do nothing in production while passing every test
    that only checked the markup was present. The handler is therefore the same
    external file the contact page loads — one delegated listener, not a second
    copy mechanism to keep in step with the first.
    """

    sequence = build(db_session, scenario)
    db_session.commit()
    body = _open(client, sequence.id, 1)

    assert "sequence.js" in body
    inline = [tag for tag in re.findall(r"<script\b[^>]*>", body) if " src=" not in tag]
    assert inline == [], f"inline script on a nonce-free CSP page: {inline}"
    assert "onclick=" not in body


# ===========================================================================
# B. What the copy controls were not allowed to change
# ===========================================================================


def test_opening_a_card_still_renders_exactly_one_message_body(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """The queue is a list, and this is the rule that keeps it one.

    Expanding the card to all seven would have made the copy controls simpler to
    add and the page unusable at forty contacts. The selector offers all seven
    and one click reaches each; the page renders one.
    """

    sequence = build(db_session, scenario)
    db_session.commit()
    body = _open(client, sequence.id, 4)

    assert body.count('class="v2-mail-body"') == 1


def test_reading_the_review_page_with_the_copy_controls_writes_nothing(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """Copying is not an action, and neither is arriving at the page.

    The controls sit outside the approve form and outside the edit
    ``<details>``, so nothing about rendering them may touch review state or the
    immutable edit lineage. A page load that recorded anything would also make
    every audit count on this queue a lie.
    """

    sequence = build(db_session, scenario)
    db_session.commit()
    before = db_session.scalar(select(func.count(AuditEvent.id)))

    client.get("/app/review")
    for step in STEPS:
        _open(client, sequence.id, step)

    assert db_session.scalar(select(func.count(AuditEvent.id))) == before


# ===========================================================================
# C. All seven positions
# ===========================================================================


def test_the_selector_offers_all_seven_positions_from_an_opened_card(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...]
) -> None:
    """Anti-vacuity for the enumeration below.

    Each step is asserted separately there, which would pass just as happily if
    six of the seven quietly resolved to the initial message. This pins that the
    seven steps are seven different messages before that enumeration runs.
    """

    sequence = build(db_session, scenario)
    rows = sequence_read.message_rows(db_session, sequence=sequence)
    db_session.commit()
    assert len(rows) == SEQUENCE_LENGTH

    body = _open(client, sequence.id, 1)
    for label in ("Initial", "F1", "F2", "F3", "F4", "F5", "F6"):
        assert f">{label}<" in body

    opened = {
        step: re.findall(r"seq-body-([0-9a-f-]{36})", _open(client, sequence.id, step))
        for step in STEPS
    }
    seen = [found[0] for found in opened.values() if found]
    assert len(seen) == SEQUENCE_LENGTH
    assert len(set(seen)) == SEQUENCE_LENGTH, f"steps collapsed onto one message: {opened}"


@pytest.mark.parametrize("step", STEPS)
def test_each_of_the_seven_positions_carries_the_copy_controls_when_opened(
    db_session: Session, client: TestClient, scenario: tuple[Any, ...], step: int
) -> None:
    """Every follow-up is an email somebody has to get out of the page.

    Covering only the initial message would leave the six that are hardest to
    reach — and the ones an operator is most likely to want to paste — untested.
    """

    sequence = build(db_session, scenario)
    rows = sequence_read.message_rows(db_session, sequence=sequence)
    db_session.commit()

    body = _open(client, sequence.id, step)
    row = rows[step - 1]
    subject_id = f"seq-subj-{row.version_id}"
    body_id = f"seq-body-{row.version_id}"

    assert f'data-copy="subject" data-copy-subject="{subject_id}"' in body
    assert f'data-copy="body" data-copy-body="{body_id}"' in body
    assert f'data-copy="full" data-copy-subject="{subject_id}" data-copy-body="{body_id}"' in body
    assert body.count('class="v2-mail-body"') == 1
