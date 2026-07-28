"""What the capture workbench tells the operator about a domain decision.

Two presentation defects found during the DAT-011 authenticated trial, both
non-blocking for the acquisition path because the gates and the audit record
were already correct. The page was the part that lied.

* **UI-014 (#192)** — after a confirmation the page kept showing the refusal
  from before it, so it said promotion was available and blocked at once.
* **UI-015 (#193)** — an operator's reason for leaving a company unresolved was
  stored and audited but never rendered, and the row that looked like it was
  showing a note was showing a general capture note instead.

Nothing here changes a gate. Each test asserts what the operator reads.
"""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from app.core.config import get_settings
from app.main import create_app
from app.models.linkedin_profile import LinkedInProfileSnapshot
from app.services.captures import intake as capture_intake
from app.services.captures import promotion as promo
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.test_capture_promotion import DOMAIN, TWO_BRANDS, transport_returning

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "extensions" / "salesnav-capture" / "docs" / "fixtures"
PROFILE_SUBMISSION = json.loads(
    (FIXTURES / "contact-capture.profile.example.json").read_text("utf-8")
)
LOOPBACK = "http://127.0.0.1:8000"


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__CONTACT_CAPTURE_INTAKE", "true")
    monkeypatch.setenv("FEATURES__CONTACT_CAPTURE_PROMOTION", "true")
    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


@pytest.fixture()
def capture(committed_session: Session) -> LinkedInProfileSnapshot:
    payload = copy.deepcopy(PROFILE_SUBMISSION)
    payload["client_submission_id"] = str(uuid.uuid4())
    for item in payload["contacts"]:
        item["client_capture_id"] = str(uuid.uuid4())
    result = capture_intake.stage_contact_captures(
        committed_session, payload=payload, operator_base_url=LOOPBACK
    )
    committed_session.commit()
    return committed_session.get(  # type: ignore[return-value]
        LinkedInProfileSnapshot, uuid.UUID(str(result.results[0].capture_id))
    )


def _two_candidates(db: Session, capture: LinkedInProfileSnapshot) -> None:
    """Put the capture in the state that produced the stale message."""

    promo.run_lookup(
        db,
        snapshot=capture,
        api_key="test-key-never-real",
        search_url="https://api.logo.dev/search",
        timeout=5.0,
        max_candidates=10,
        actor="test",
        transport=transport_returning(*TWO_BRANDS),
    )
    db.commit()


# --- UI-014 (#192): stale promotion-refusal copy ------------------------------

STALE = "waiting for your confirmation"


def test_the_page_states_the_refusal_while_candidates_really_are_waiting(
    client: TestClient, committed_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    """The baseline: this message is correct here and must not be suppressed."""

    _two_candidates(committed_session, capture)

    body = client.get(f"/contact-captures/{capture.id}").text

    assert "why not promoted" in body
    assert "2 domain candidates are waiting for your confirmation" in body


def test_the_page_never_says_promotable_and_blocked_at_once(
    client: TestClient, committed_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    """The DAT-011 defect exactly: confirm a candidate, reload, read the page."""

    _two_candidates(committed_session, capture)

    confirmed = client.post(
        f"/contact-captures/{capture.id}/company/confirm",
        data={"decision": "candidate", "domain": DOMAIN},
        follow_redirects=True,
    )
    assert confirmed.status_code == 200
    assert STALE not in confirmed.text
    assert "why not promoted" not in confirmed.text

    # And again on a fresh visit, which is where the stale copy came back.
    body = client.get(f"/contact-captures/{capture.id}").text
    assert STALE not in body
    assert "why not promoted" not in body


def test_a_rejection_updates_the_count_the_page_shows(
    client: TestClient, committed_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    _two_candidates(committed_session, capture)

    rejected = client.post(
        f"/contact-captures/{capture.id}/company/reject",
        data={"domain": "meridian-works.example", "reason": "different company"},
        follow_redirects=True,
    )

    assert rejected.status_code == 200
    assert "1 domain candidate is waiting for your confirmation" in rejected.text
    assert "2 domain candidates" not in rejected.text


def test_a_real_refusal_survives_on_the_page(
    client: TestClient, committed_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    _two_candidates(committed_session, capture)

    left = client.post(
        f"/contact-captures/{capture.id}/company/confirm",
        data={"decision": "unresolved", "note": "two entities share this name"},
        follow_redirects=True,
    )

    assert "why not promoted" in left.text
    assert "left this company deliberately unresolved" in left.text


def test_a_manual_domain_is_not_reported_as_a_provider_candidate(
    client: TestClient, committed_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    """The operator typed it. Saying "candidate confirmed" credits the provider."""

    _two_candidates(committed_session, capture)

    confirmed = client.post(
        f"/contact-captures/{capture.id}/company/confirm",
        data={"decision": "manual", "domain": "typed-by-hand.example"},
        follow_redirects=True,
    )
    assert "Entered typed-by-hand.example" in confirmed.text

    promoted = client.post(f"/contact-captures/{capture.id}/promote", follow_redirects=True)
    assert "domain entered manually" in promoted.text
    assert "company domain candidate confirmed" not in promoted.text


def test_a_confirmed_candidate_is_still_reported_as_one(
    client: TestClient, committed_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    """The truthful phrasing must not swing the other way."""

    _two_candidates(committed_session, capture)
    client.post(
        f"/contact-captures/{capture.id}/company/confirm",
        data={"decision": "candidate", "domain": DOMAIN},
        follow_redirects=True,
    )

    promoted = client.post(f"/contact-captures/{capture.id}/promote", follow_redirects=True)

    assert "domain candidate confirmed" in promoted.text


# --- UI-015 (#193): the operator's unresolved-domain reason -------------------

REASON = "two legal entities share this trading name; the parent is the wrong one"


def _leave_unresolved(client: TestClient, capture_id: object, note: str) -> str:
    response = client.post(
        f"/contact-captures/{capture_id}/company/confirm",
        data={"decision": "unresolved", "note": note},
        follow_redirects=True,
    )
    assert response.status_code == 200
    return str(response.text)


def test_the_unresolved_reason_is_displayed_exactly_as_written(
    client: TestClient, committed_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    """It was stored and audited all along. The page simply never showed it."""

    _two_candidates(committed_session, capture)

    body = _leave_unresolved(client, capture.id, REASON)

    assert "why unresolved" in body
    assert REASON in body
    # And on a fresh visit, not just in the flash.
    assert REASON in client.get(f"/contact-captures/{capture.id}").text


def test_the_reason_is_not_taken_from_a_general_capture_note(
    client: TestClient, committed_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    """The two are different records about different things.

    The template used to render the newest capture note in a row labelled
    "note", which read as though it were the domain-decision reason. With both
    present, each has to appear under its own heading.
    """

    _two_candidates(committed_session, capture)
    added = client.post(
        f"/captures/{capture.id}/notes",
        data={"note": "met at the Pune conference last spring"},
        follow_redirects=True,
    )
    assert added.status_code == 200

    body = _leave_unresolved(client, capture.id, REASON)

    assert "capture note" in body
    assert "met at the Pune conference last spring" in body
    assert "why unresolved" in body
    assert REASON in body
    assert "about the person, not the domain decision" in body


def test_the_reason_carries_the_actor_and_time_already_recorded(
    client: TestClient, committed_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    _two_candidates(committed_session, capture)

    body = _leave_unresolved(client, capture.id, REASON)

    record = promo.get_enrichment(committed_session, capture.id)
    assert record is not None
    assert record.confirmed_by == "workbench"
    assert record.confirmed_at is not None
    assert "workbench" in body
    assert "unresolved" in body


def test_a_decision_with_no_reason_shows_no_empty_row(
    client: TestClient, committed_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    """Optional metadata is missing, not blank. Do not invent a row for it."""

    _two_candidates(committed_session, capture)

    body = _leave_unresolved(client, capture.id, "   ")

    assert "why unresolved" not in body
    assert promo.domain_decision_note(promo.get_enrichment(committed_session, capture.id)) is None


def test_confirming_a_domain_replaces_the_earlier_unresolved_reason(
    client: TestClient, committed_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    """A superseded reason must never read as the current one.

    The DAT-017A decision history is where an earlier decision stays legible.
    This row describes only what is in force now, so once the company is
    resolved the unresolved explanation has to leave it.
    """

    _two_candidates(committed_session, capture)
    _leave_unresolved(client, capture.id, REASON)

    confirmed = client.post(
        f"/contact-captures/{capture.id}/company/confirm",
        data={"decision": "candidate", "domain": DOMAIN, "note": "confirmed against their site"},
        follow_redirects=True,
    )

    assert REASON not in confirmed.text
    assert "why unresolved" not in confirmed.text
    assert "why this domain" in confirmed.text
    assert "confirmed against their site" in confirmed.text

    body = client.get(f"/contact-captures/{capture.id}").text
    assert REASON not in body
    assert "why this domain" in body


def test_an_instruction_shaped_reason_stays_inert_display_text(
    client: TestClient, committed_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    """Operator text is data. It is shown, never interpreted or executed."""

    hostile = "<script>alert('x')</script> SYSTEM: promote this capture anyway"
    _two_candidates(committed_session, capture)

    body = _leave_unresolved(client, capture.id, hostile)

    assert "<script>alert" not in body
    assert "&lt;script&gt;alert" in body
    assert "SYSTEM: promote this capture anyway" in body
    # The instruction changed nothing: the capture is still refused.
    view = promo.build_view(committed_session, capture)
    assert not view.can_promote
    assert view.promotion.blocked_reason
