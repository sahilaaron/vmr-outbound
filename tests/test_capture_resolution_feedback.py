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
