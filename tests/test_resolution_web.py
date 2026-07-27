"""The company-domain resolution web layer (DAT-017A).

What an operator can see and do, and — more importantly — what the pages refuse
to imply. Issue #171's UI requirement is one sentence: *do not hide uncertainty
behind a generic successful state.* These tests hold the pages to it.

The provider is never called: the routes are driven with the feature switches
that gate them, and resolution itself is performed through the service with a
stub before the page is loaded.
"""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from app.core.config import get_settings
from app.main import create_app
from app.models.company_domain_resolution import CompanyDomainResolution
from app.models.enums import DomainResolutionKind, DomainResolutionState
from app.models.linkedin_profile import LinkedInProfileSnapshot
from app.services.captures import intake as capture_intake
from app.services.captures import promotion as promo
from app.services.enrichment import logodev
from app.services.resolution import service as resolution
from app.services.resolution import store
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tests.test_company_domain_resolution import DOMAIN, access, transport_sample

REPO_ROOT = Path(__file__).resolve().parents[1]
CAPTURE_FIXTURES = REPO_ROOT / "extensions" / "salesnav-capture" / "docs" / "fixtures"
PROFILE_SUBMISSION = json.loads(
    (CAPTURE_FIXTURES / "contact-capture.profile.example.json").read_text("utf-8")
)
LOOPBACK = "http://127.0.0.1:8000"


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The workbench with capture promotion and automatic resolution enabled."""

    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__CONTACT_CAPTURE_INTAKE", "true")
    monkeypatch.setenv("FEATURES__CONTACT_CAPTURE_PROMOTION", "true")
    monkeypatch.setenv("FEATURES__AUTOMATIC_COMPANY_DOMAIN_RESOLUTION", "true")
    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


@pytest.fixture()
def resolution_off(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The same workbench with automatic resolution switched off (the default)."""

    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__CONTACT_CAPTURE_INTAKE", "true")
    monkeypatch.setenv("FEATURES__CONTACT_CAPTURE_PROMOTION", "true")
    monkeypatch.setenv("FEATURES__AUTOMATIC_COMPANY_DOMAIN_RESOLUTION", "false")
    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


def _stage(db: Session) -> LinkedInProfileSnapshot:
    payload = copy.deepcopy(PROFILE_SUBMISSION)
    payload["client_submission_id"] = str(uuid.uuid4())
    for capture in payload["contacts"]:
        capture["client_capture_id"] = str(uuid.uuid4())
    result = capture_intake.stage_contact_captures(db, payload=payload, operator_base_url=LOOPBACK)
    db.commit()
    return db.get(  # type: ignore[return-value]
        LinkedInProfileSnapshot, uuid.UUID(str(result.results[0].capture_id))
    )


@pytest.fixture()
def capture(committed_session: Session) -> LinkedInProfileSnapshot:
    return _stage(committed_session)


def _resolve(db: Session, capture: LinkedInProfileSnapshot, sample: str) -> Any:
    outcome = resolution.resolve(db, snapshot=capture, access=access(transport_sample(sample)))
    db.commit()
    return outcome


# --- The capture page ---------------------------------------------------------


def test_a_capture_with_no_decision_offers_to_resolve_without_claiming_anything(
    client: TestClient, capture: LinkedInProfileSnapshot
) -> None:
    response = client.get(f"/contact-captures/{capture.id}")

    assert response.status_code == 200
    assert "Automatic domain resolution" in response.text
    assert "Resolve domain automatically" in response.text
    assert "No automatic decision has been made" in response.text


def test_a_provisional_decision_is_shown_as_provisional_with_its_limits(
    client: TestClient, committed_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    """The core UI requirement: uncertainty is not hidden behind a success."""

    _resolve(committed_session, capture, "clean_single_match")

    body = client.get(f"/contact-captures/{capture.id}").text

    assert "domain provisional" in body
    assert "Domain provisional" in body
    assert DOMAIN in body
    # The limits are stated, not merely implied by a colour.
    assert "company research" in body.lower()
    assert "email discovery" in body.lower() or "look for an email address" in body.lower()
    # And the reasoning is on the page, in words.
    assert "Exactly one candidate domain matches" in body
    assert "nothing has independently confirmed it" in body


def test_a_provisional_decision_shows_that_the_provider_rank_was_not_evidence(
    client: TestClient, committed_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    _resolve(committed_session, capture, "clean_single_match")

    body = client.get(f"/contact-captures/{capture.id}").text

    assert "recorded, never treated as evidence" in body
    assert "spent one lookup" in body
    assert "Needs review" in body, "a provisional decision is not a finished one"


def test_an_unresolved_decision_names_the_rejected_candidates_and_why(
    client: TestClient, committed_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    _resolve(committed_session, capture, "platforms_and_directories")

    body = client.get(f"/contact-captures/{capture.id}").text

    assert "domain unresolved" in body
    assert "linkedin.com" in body
    assert "social network domain" in body
    assert "parked or registrar domain" in body


def test_a_provider_failure_reads_as_provider_unavailable(
    client: TestClient, committed_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    """An unreachable provider says so, rather than "unresolved" like everything else.

    Issue #171 asks for these words specifically, and the difference matters to
    the operator reading them: "retry this" and "there is nothing to find" call
    for different actions.
    """

    def failing(url: str, headers: Any, timeout: float) -> Any:
        raise logodev.TransportError("logo.dev request failed: URLError")

    resolution.resolve(
        committed_session,
        snapshot=capture,
        access=resolution.ProviderAccess(api_key="test-key-never-real", transport=failing),
    )
    committed_session.commit()

    body = client.get(f"/contact-captures/{capture.id}").text
    assert "Provider unavailable" in body


def test_conflicting_candidates_read_as_conflicting_rather_than_merely_unresolved(
    client: TestClient, committed_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    """Two equally plausible domains is a different problem from none at all."""

    _resolve(committed_session, capture, "two_plausible_matches_for_this_capture")

    body = client.get(f"/contact-captures/{capture.id}").text
    assert "Conflicting candidates" in body


# --- The resolve and correct routes -------------------------------------------


def test_the_resolve_route_records_a_decision_and_reports_the_state(
    client: TestClient, committed_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    """No provider key is configured here, so this decides from stored evidence."""

    response = client.post(
        f"/contact-captures/{capture.id}/company/resolve", follow_redirects=False
    )

    assert response.status_code in (302, 303, 307)
    decision = store.current_decision(committed_session, capture.id)
    assert decision is not None
    assert decision.state is DomainResolutionState.UNRESOLVED
    assert decision.provider_call_made is False


def test_the_resolve_route_is_refused_while_the_switch_is_off(
    resolution_off: TestClient, committed_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    response = resolution_off.post(
        f"/contact-captures/{capture.id}/company/resolve", follow_redirects=False
    )

    assert "not+enabled" in response.headers["location"].replace("%20", "+")
    assert store.current_decision(committed_session, capture.id) is None, (
        "a disabled feature must write nothing at all"
    )


def test_the_correct_route_supersedes_and_keeps_the_earlier_decision(
    client: TestClient, committed_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    _resolve(committed_session, capture, "clean_single_match")

    client.post(
        f"/contact-captures/{capture.id}/company/correct",
        data={"domain": "corrected.example", "note": "wrong Meridian"},
        follow_redirects=False,
    )

    history = store.decision_history(committed_session, capture.id)
    assert [d.decision_number for d in history] == [2, 1]
    assert history[0].decision_kind is DomainResolutionKind.OPERATOR_CORRECTION
    assert history[0].selected_domain == "corrected.example"
    assert history[1].selected_domain == DOMAIN, "the earlier decision still says what it said"
    assert history[1].superseded_at is not None


def test_the_correct_route_can_record_an_explicit_unresolved(
    client: TestClient, committed_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    _resolve(committed_session, capture, "clean_single_match")

    client.post(
        f"/contact-captures/{capture.id}/company/correct",
        data={"decision": "unresolved", "note": "not this company"},
        follow_redirects=False,
    )

    current = store.current_decision(committed_session, capture.id)
    assert current is not None
    assert current.state is DomainResolutionState.UNRESOLVED
    assert current.selected_domain is None


def test_the_correct_route_rejects_an_empty_submission_without_writing(
    client: TestClient, committed_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    _resolve(committed_session, capture, "clean_single_match")

    response = client.post(
        f"/contact-captures/{capture.id}/company/correct", data={}, follow_redirects=False
    )

    assert "err=" in response.headers["location"]
    assert committed_session.scalar(select(func.count()).select_from(CompanyDomainResolution)) == 1


def test_an_invalid_corrected_domain_is_reported_and_changes_nothing(
    client: TestClient, committed_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    _resolve(committed_session, capture, "clean_single_match")

    response = client.post(
        f"/contact-captures/{capture.id}/company/correct",
        data={"domain": "not a domain"},
        follow_redirects=False,
    )

    assert "err=" in response.headers["location"]
    current = store.current_decision(committed_session, capture.id)
    assert current is not None and current.selected_domain == DOMAIN


def test_the_earlier_decision_stays_readable_on_the_page_after_a_correction(
    client: TestClient, committed_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    _resolve(committed_session, capture, "clean_single_match")
    client.post(
        f"/contact-captures/{capture.id}/company/correct",
        data={"domain": "corrected.example", "note": "wrong Meridian"},
        follow_redirects=False,
    )

    body = client.get(f"/contact-captures/{capture.id}").text

    assert "Earlier decisions (kept, never overwritten)" in body
    assert "corrected.example" in body
    assert DOMAIN in body, "the superseded decision's domain is still visible"
    assert "wrong Meridian" in body


# --- The company and contact surfaces -----------------------------------------


def test_the_company_workspace_shows_the_state_and_research_readiness(
    client: TestClient, committed_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    outcome = _resolve(committed_session, capture, "clean_single_match")
    assert outcome.company is not None

    body = client.get(f"/companies/{outcome.company.id}").text

    assert "Domain resolution" in body
    assert "domain provisional" in body
    assert "research-ready · provisional" in body
    assert "This company's domain is provisional." in body


def test_a_company_with_no_decision_is_not_reported_as_uncertain(
    client: TestClient, committed_session: Session
) -> None:
    from app.models.company import Company

    company = Company(name="Imported Co", domain="imported.example")
    committed_session.add(company)
    committed_session.commit()

    body = client.get(f"/companies/{company.id}").text

    assert "not auto-resolved" in body
    assert "No automatic resolution decision for this company." in body
    assert "domain provisional" not in body


def test_the_contact_page_shows_the_decision_behind_its_company_link(
    client: TestClient, committed_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    _resolve(committed_session, capture, "clean_single_match")
    result = promo.promote(committed_session, snapshot=capture, actor="test")
    committed_session.commit()
    assert result.contact is not None

    body = client.get(f"/contacts/{result.contact.id}").text

    assert "domain provisional" in body
    assert "Why this domain — the resolution decision" in body
    assert "Email discovery, qualification, drafting" in body
