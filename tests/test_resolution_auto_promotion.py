"""DAT-017A — a resolved decision finishes the job, without two more clicks.

Before this, an operator who watched the policy resolve a capture still had to
press Confirm and then Promote. Neither click carried a judgement: the policy had
already decided, on evidence, and the operator had nothing to add. They were
friction standing between a decided capture and the permanent Company and Contact
it implied.

These tests describe the corrected workflow and, more importantly, its edges:

* both resolved states promote — and ``provisional`` stays ``provisional``;
* unresolved, conflicting, ambiguous and provider-failed captures do **not**
  promote, because those are the cases where the operator's judgement is exactly
  what was missing, and they keep their manual controls;
* what a provisional domain *authorizes* is unchanged — creating a Contact is not
  a promise about the domain, and the service-level gates still refuse email
  discovery, qualification, drafting, campaign eligibility and sending;
* a retry returns the existing outcome rather than a second person;
* a failure part-way leaves no half-built Company/Contact pair.
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
from app.models.company import Company
from app.models.contact import Contact
from app.models.enums import (
    CompanyResolutionOutcome,
    ContactPromotionOutcome,
    DomainResolutionState,
    EnrichmentConfirmationSource,
)
from app.models.linkedin_profile import LinkedInProfileSnapshot
from app.services.captures import intake as capture_intake
from app.services.captures import promotion as promo
from app.services.enrichment import logodev
from app.services.resolution import gates
from app.services.resolution import service as resolution
from sqlalchemy import func, select
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "extensions" / "salesnav-capture" / "docs" / "fixtures"
PROFILE_SUBMISSION = json.loads(
    (FIXTURES / "contact-capture.profile.example.json").read_text("utf-8")
)
PROVIDER_SAMPLES = json.loads(
    (REPO_ROOT / "tests" / "fixtures" / "logodev_brand_search_sanitized.json").read_text("utf-8")
)

LOOPBACK = "http://127.0.0.1:8000"
DOMAIN = "meridianworks.example"


def transport_body(body: list[dict[str, Any]]) -> logodev.Transport:
    def _transport(url: str, headers: Any, timeout: float) -> logodev.RawResponse:
        return logodev.RawResponse(status_code=200, body=json.dumps(body))

    return _transport


def transport_sample(name: str) -> logodev.Transport:
    return transport_body(PROVIDER_SAMPLES[name]["body"])


def transport_failing() -> logodev.Transport:
    def _transport(url: str, headers: Any, timeout: float) -> logodev.RawResponse:
        raise logodev.TransportError("logo.dev request failed: URLError")

    return _transport


def access(transport: logodev.Transport | None) -> resolution.ProviderAccess:
    return resolution.ProviderAccess(
        api_key="test-key-never-real",
        search_url="https://api.logo.dev/search",
        timeout=5.0,
        max_candidates=10,
        transport=transport,
    )


@pytest.fixture()
def enable_capture(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("FEATURES__CONTACT_CAPTURE_INTAKE", "true")
    monkeypatch.setenv("FEATURES__CONTACT_CAPTURE_PROMOTION", "true")
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


def _stage(db: Session) -> LinkedInProfileSnapshot:
    payload = copy.deepcopy(PROFILE_SUBMISSION)
    payload["client_submission_id"] = str(uuid.uuid4())
    for capture in payload["contacts"]:
        capture["client_capture_id"] = str(uuid.uuid4())
    result = capture_intake.stage_contact_captures(db, payload=payload, operator_base_url=LOOPBACK)
    cid = uuid.UUID(str(result.results[0].capture_id))
    snapshot = db.get(LinkedInProfileSnapshot, cid)
    assert snapshot is not None
    return snapshot


@pytest.fixture()
def capture(db_session: Session) -> LinkedInProfileSnapshot:
    return _stage(db_session)


def _contacts(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(Contact)) or 0


def _companies(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(Company)) or 0


# --- The resolved path promotes itself ----------------------------------------


def test_a_provisional_decision_creates_company_and_contact_with_no_clicks(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    """One clean candidate: resolved, promoted, and still honestly provisional."""

    outcome = resolution.resolve(
        db_session, snapshot=capture, access=access(transport_sample("clean_single_match"))
    )

    assert outcome.state is DomainResolutionState.PROVISIONAL
    assert outcome.auto_promoted is True

    result = outcome.promotion_result
    assert result is not None
    assert result.contact_outcome is ContactPromotionOutcome.CONTACT_CREATED
    assert result.company_outcome is CompanyResolutionOutcome.DOMAIN_PROVISIONAL

    contact = outcome.contact
    assert contact is not None
    assert outcome.company is not None
    assert contact.company_id == outcome.company.id
    assert contact.company_domain == DOMAIN


def test_provisional_stays_provisional_after_automatic_promotion(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    """Creating a contact is not a promise about the domain."""

    outcome = resolution.resolve(
        db_session, snapshot=capture, access=access(transport_sample("clean_single_match"))
    )
    db_session.flush()

    assert outcome.decision.state is DomainResolutionState.PROVISIONAL
    assert outcome.decision.policy_version
    assert outcome.decision.reasons

    view = promo.build_view(db_session, capture)
    assert view.promotion.company_outcome is CompanyResolutionOutcome.DOMAIN_PROVISIONAL


def test_a_confirmed_decision_reuses_an_approved_mapping_without_a_provider_call(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    """The second capture of a known company costs nothing and still promotes."""

    resolution.resolve(
        db_session, snapshot=capture, access=access(transport_sample("clean_single_match"))
    )
    promo.confirm_domain(
        db_session,
        snapshot=capture,
        source=EnrichmentConfirmationSource.MANUAL,
        domain=DOMAIN,
        actor="test",
    )
    db_session.flush()

    second = _stage(db_session)
    outcome = resolution.resolve(
        db_session, snapshot=second, access=access(transport_failing()), actor="test"
    )

    # A provider that would raise if called proves no call was made.
    assert outcome.provider_call_made is False
    assert outcome.state is DomainResolutionState.CONFIRMED
    assert outcome.auto_promoted is True
    assert outcome.contact is not None


# --- The exceptions keep their operator ---------------------------------------


@pytest.mark.parametrize(
    "sample",
    ["two_plausible_matches", "platforms_and_directories", "unrelated_top_rank", "empty"],
)
def test_an_unresolved_decision_never_auto_promotes(
    db_session: Session, capture: LinkedInProfileSnapshot, sample: str
) -> None:
    """Ambiguity, ineligibility and emptiness all stay with the operator."""

    outcome = resolution.resolve(
        db_session, snapshot=capture, access=access(transport_sample(sample))
    )

    assert outcome.state is DomainResolutionState.UNRESOLVED
    assert outcome.auto_promoted is False
    assert outcome.promotion_result is None
    assert _contacts(db_session) == 0


def test_a_provider_failure_never_auto_promotes(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    outcome = resolution.resolve(db_session, snapshot=capture, access=access(transport_failing()))

    assert outcome.state is DomainResolutionState.UNRESOLVED
    assert outcome.auto_promoted is False
    assert _contacts(db_session) == 0


def test_the_manual_controls_remain_for_an_unresolved_capture(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    """The Promote button is still there precisely where judgement is needed."""

    resolution.resolve(
        db_session, snapshot=capture, access=access(transport_sample("two_plausible_matches"))
    )
    view = promo.build_view(db_session, capture)

    assert view.promotion.promoted_contact_id is None
    assert view.can_promote is False, "no domain is settled, so promotion is not offered"
    assert view.candidates, "the candidates are still presented for an operator decision"


# --- What provisional still refuses to authorize ------------------------------


def test_automatic_promotion_does_not_unlock_the_downstream_gates(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    """A provisional contact exists; it still cannot be worked."""

    outcome = resolution.resolve(
        db_session, snapshot=capture, access=access(transport_sample("clean_single_match"))
    )
    db_session.flush()
    contact = outcome.contact
    assert contact is not None

    allowed = gates.authorize_contact(
        db_session, contact=contact, stage=gates.DownstreamStage.COMPANY_RESEARCH
    )
    assert allowed.blocked is False

    for stage in (
        gates.DownstreamStage.EMAIL_DISCOVERY,
        gates.DownstreamStage.FINAL_QUALIFICATION,
        gates.DownstreamStage.PERSONALIZED_DRAFTING,
        gates.DownstreamStage.CAMPAIGN_ELIGIBILITY,
        gates.DownstreamStage.SENDING,
    ):
        decision = gates.authorize_contact(db_session, contact=contact, stage=stage)
        assert decision.blocked is True, f"{stage} must stay blocked on a provisional domain"


# --- Idempotency and transactionality -----------------------------------------


def test_re_resolving_returns_the_existing_outcome_and_no_second_contact(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    first = resolution.resolve(
        db_session, snapshot=capture, access=access(transport_sample("clean_single_match"))
    )
    db_session.flush()
    contacts_after_first = _contacts(db_session)
    companies_after_first = _companies(db_session)

    again = resolution.resolve(
        db_session, snapshot=capture, access=access(transport_sample("clean_single_match"))
    )
    db_session.flush()

    assert again.created is False
    assert _contacts(db_session) == contacts_after_first
    assert _companies(db_session) == companies_after_first
    assert first.contact is not None
    # The capture still points at the one contact it made.
    view = promo.build_view(db_session, capture)
    assert view.promotion.promoted_contact_id == first.contact.id


def test_forcing_a_recalculation_creates_no_duplicate_contact_or_company(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    first = resolution.resolve(
        db_session, snapshot=capture, access=access(transport_sample("clean_single_match"))
    )
    db_session.flush()
    assert first.contact is not None

    forced = resolution.resolve(
        db_session,
        snapshot=capture,
        access=access(transport_sample("clean_single_match")),
        force=True,
    )
    db_session.flush()

    assert _contacts(db_session) == 1
    assert forced.promotion_result is not None
    assert forced.promotion_result.contact_outcome is ContactPromotionOutcome.ALREADY_PROMOTED
    assert forced.contact is not None
    assert forced.contact.id == first.contact.id


def test_a_failure_during_automatic_promotion_leaves_no_half_built_pair(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    """Company and Contact are created in one transaction or not at all."""

    before_contacts = _contacts(db_session)
    before_companies = _companies(db_session)

    boom = RuntimeError("promotion failed part-way")

    def _explode(*args: Any, **kwargs: Any) -> None:
        raise boom

    original = promo.promote
    promo.promote = _explode  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError):
            resolution.resolve(
                db_session,
                snapshot=capture,
                access=access(transport_sample("clean_single_match")),
            )
    finally:
        promo.promote = original  # type: ignore[assignment]

    db_session.rollback()

    assert _contacts(db_session) == before_contacts
    assert _companies(db_session) == before_companies


# --- An operator correction keeps its explicit step ---------------------------


def test_an_operator_correction_does_not_silently_promote(
    db_session: Session, capture: LinkedInProfileSnapshot
) -> None:
    """A correction is a deliberate act, not a trigger for a side effect."""

    resolution.resolve(
        db_session, snapshot=capture, access=access(transport_sample("two_plausible_matches"))
    )
    assert _contacts(db_session) == 0

    resolution.correct(
        db_session,
        snapshot=capture,
        domain="chosen-by-hand.example",
        actor="operator",
        note="I checked their website",
    )
    db_session.flush()

    assert _contacts(db_session) == 0, (
        "correcting a decision must not create a contact behind the operator's back"
    )
    view = promo.build_view(db_session, capture)
    assert view.can_promote is True, "the explicit Promote step remains available"


# --- What the operator actually sees ------------------------------------------


def _workbench(db: Session, monkeypatch: pytest.MonkeyPatch) -> Any:
    from app.api.deps import get_db
    from app.main import create_app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__CONTACT_CAPTURE_INTAKE", "true")
    monkeypatch.setenv("FEATURES__CONTACT_CAPTURE_PROMOTION", "true")
    monkeypatch.setenv("FEATURES__COMPANY_DOMAIN_AUTO_RESOLUTION", "true")
    get_settings.cache_clear()
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_db] = _override
    return app, TestClient(app)


def test_a_resolved_capture_page_offers_no_confirm_or_promote(
    db_session: Session, capture: LinkedInProfileSnapshot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two clicks are gone from the page, not merely from the service."""

    resolution.resolve(
        db_session, snapshot=capture, access=access(transport_sample("clean_single_match"))
    )
    db_session.flush()

    app, client = _workbench(db_session, monkeypatch)
    try:
        with client as c:
            body = c.get(f"/contact-captures/{capture.id}").text
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()

    assert "Promote to contact" not in body
    assert f"/contact-captures/{capture.id}/promote" not in body
    assert f"/contact-captures/{capture.id}/company/confirm" not in body
    # ...and it says what it did, truthfully.
    assert "Resolved automatically — provisional." in body
    # The one thing the copy must never do is upgrade the decision.
    assert "Resolved automatically — confirmed." not in body


def test_an_unresolved_capture_page_still_offers_the_manual_controls(
    db_session: Session, capture: LinkedInProfileSnapshot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where judgement is needed, the operator still has their controls."""

    resolution.resolve(
        db_session, snapshot=capture, access=access(transport_sample("two_plausible_matches"))
    )
    db_session.flush()

    app, client = _workbench(db_session, monkeypatch)
    try:
        with client as c:
            body = c.get(f"/contact-captures/{capture.id}").text
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()

    assert "Promote to contact" in body
    assert f"/contact-captures/{capture.id}/company/confirm" in body
