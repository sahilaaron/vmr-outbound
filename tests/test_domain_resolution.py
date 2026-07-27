"""DAT-017 — automatic company-domain resolution.

The behaviour under test is a trade: fewer operator decisions in exchange for a
policy that must never choose a domain it cannot defend. So these tests are
weighted towards the refusals. Auto-confirmation is covered, but so is every way
the policy is expected to decline — a lone provider result, two sources
disagreeing, an unreachable provider — because a wrong domain is worse than an
unanswered one, and the cases that prove restraint are the ones that would
silently regress.

Provider access is always a stub transport. Nothing here touches the network,
and no test uses a real key or real profile data.
"""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from app.api.deps import get_db
from app.core.config import get_settings
from app.main import create_app
from app.models.company import Company
from app.models.contact import Contact
from app.models.enums import (
    CompanyResolutionOutcome,
    ContactPromotionOutcome,
    DomainResolutionDecision,
    EnrichmentConfirmationSource,
    EnrichmentConfirmationStatus,
    EnrichmentLookupStatus,
    SuppressionReason,
    SuppressionType,
)
from app.models.linkedin_company import LinkedInCompanySnapshot
from app.models.linkedin_profile import LinkedInProfileSnapshot
from app.models.salesnav_enrichment import SalesNavCompanyEnrichment
from app.services.captures import domain_resolution as resolution
from app.services.captures import intake as capture_intake
from app.services.captures import promotion as promo
from app.services.enrichment import domain_policy as policy
from app.services.enrichment import logodev
from app.services.suppressions import add_suppression
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "extensions" / "salesnav-capture" / "docs" / "fixtures"
PROFILE_SUBMISSION = json.loads(
    (FIXTURES / "contact-capture.profile.example.json").read_text("utf-8")
)

LOOPBACK = "http://127.0.0.1:8000"

# The capture fixture is Morgan Vale at Meridian Works.
COMPANY_NAME = "Meridian Works"
COMPANY_LINKEDIN_ID = "meridian-works"
COMPANY_LINKEDIN_URL = "https://www.linkedin.com/company/meridian-works"
DOMAIN = "meridianworks.example"
OTHER_DOMAIN = "meridian-works-group.example"


# --- Provider stubs -----------------------------------------------------------


class CountingTransport:
    """A stub transport that records how many times it was called.

    Provider frugality is a stated requirement, not an implementation detail, so
    the call count is asserted on directly rather than inferred.
    """

    def __init__(self, *brands: dict[str, Any], status: int = 200) -> None:
        self.brands = list(brands)
        self.status = status
        self.calls = 0

    def __call__(self, url: str, headers: Any, timeout: float) -> logodev.RawResponse:
        self.calls += 1
        return logodev.RawResponse(status_code=self.status, body=json.dumps(self.brands))


class FailingTransport:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, url: str, headers: Any, timeout: float) -> logodev.RawResponse:
        self.calls += 1
        raise logodev.TransportError("logo.dev request failed: URLError")


def resolve(
    db: Session,
    snapshot: LinkedInProfileSnapshot,
    transport: Any = None,
    *,
    promote: bool = False,
    **kwargs: Any,
) -> resolution.ResolutionOutcome:
    call = resolution.resolve_and_promote if promote else resolution.resolve_capture
    return call(
        db,
        snapshot=snapshot,
        api_key="test-key-never-real",
        search_url="https://api.logo.dev/search",
        timeout=5.0,
        max_candidates=10,
        actor="test",
        transport=transport,
        **kwargs,
    )


# --- Fixtures -----------------------------------------------------------------


@pytest.fixture()
def enable_capture(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("FEATURES__CONTACT_CAPTURE_INTAKE", "true")
    monkeypatch.setenv("FEATURES__CONTACT_CAPTURE_PROMOTION", "true")
    monkeypatch.setenv("FEATURES__AUTOMATIC_DOMAIN_RESOLUTION", "true")
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


def _stage(db: Session, submission: dict[str, Any]) -> list[LinkedInProfileSnapshot]:
    payload = copy.deepcopy(submission)
    payload["client_submission_id"] = str(uuid.uuid4())
    for capture in payload["contacts"]:
        capture["client_capture_id"] = str(uuid.uuid4())
    result = capture_intake.stage_contact_captures(db, payload=payload, operator_base_url=LOOPBACK)
    ids = [uuid.UUID(str(r.capture_id)) for r in result.results]
    return [db.get(LinkedInProfileSnapshot, cid) for cid in ids]  # type: ignore[misc]


@pytest.fixture()
def capture(db_session: Session) -> LinkedInProfileSnapshot:
    return _stage(db_session, PROFILE_SUBMISSION)[0]


def _company_page(
    db: Session,
    *,
    domain: str = DOMAIN,
    linkedin_id: str | None = COMPANY_LINKEDIN_ID,
    normalized_url: str | None = COMPANY_LINKEDIN_URL,
    name: str = COMPANY_NAME,
) -> LinkedInCompanySnapshot:
    """An operator-captured LinkedIn company page carrying a website domain.

    This is the independent evidence DAT-017 introduces. It is seeded directly
    rather than through the intake so a test can control precisely which join
    key is available.
    """

    snapshot = LinkedInCompanySnapshot(
        client_capture_id=str(uuid.uuid4()),
        content_hash=uuid.uuid4().hex,
        schema_version="linkedin-company-capture/1.0.0",
        source="chrome-extension:linkedin-company-capture",
        source_url=normalized_url,
        normalized_company_url=normalized_url,
        company_linkedin_id=linkedin_id,
        website_domain=domain,
        captured_at=datetime.now(UTC),
        extraction_status="ok",
        payload={},
        company_fields={"name": name, "website": f"https://{domain}"},
    )
    db.add(snapshot)
    db.flush()
    return snapshot


def _prior_confirmation(db: Session, *, domain: str, linkedin_id: str | None = None) -> None:
    """An earlier operator confirmation for the same company, on another capture."""

    other = _stage(db, PROFILE_SUBMISSION)[0]
    promo.ensure_records(db, other)
    record = promo.get_enrichment(db, other.id)
    assert record is not None
    record.company_linkedin_id = linkedin_id
    promo.confirm_domain(
        db,
        snapshot=other,
        source=EnrichmentConfirmationSource.MANUAL,
        domain=domain,
        actor="operator",
    )
    # Promote it out of the way so it cannot be confused with the capture under
    # test when a query looks for pending work.
    promo.promote(db, snapshot=other, actor="operator")


def _record(db: Session, snapshot: LinkedInProfileSnapshot) -> SalesNavCompanyEnrichment:
    record = promo.get_enrichment(db, snapshot.id)
    assert record is not None
    return record


# =============================================================================
# 1. One strongly corroborated candidate auto-confirms and promotes
# =============================================================================


def test_a_corroborated_candidate_auto_confirms_and_promotes(
    db_session: Session, capture: LinkedInProfileSnapshot, enable_capture: None
) -> None:
    """Two independent sources naming the same domain is enough.

    The provider suggested it and an operator-captured company page for the same
    LinkedIn company id already showed it. Asking a human to retype what two
    independent sources agree on is friction, not judgement.
    """

    _company_page(db_session)
    transport = CountingTransport({"domain": DOMAIN, "name": COMPANY_NAME})

    outcome = resolve(db_session, capture, transport, promote=True)

    assert outcome.decision is not None
    assert outcome.decision.decision is DomainResolutionDecision.AUTO_CONFIRMED
    assert outcome.decision.domain == DOMAIN
    assert outcome.applied is True
    assert outcome.promoted is True

    record = _record(db_session, capture)
    assert record.confirmation_status is EnrichmentConfirmationStatus.CONFIRMED
    assert record.confirmed_domain == DOMAIN
    assert record.confirmation_source is EnrichmentConfirmationSource.AUTOMATIC_POLICY

    assert outcome.promotion is not None
    assert outcome.promotion.company_outcome is CompanyResolutionOutcome.DOMAIN_AUTO_CONFIRMED
    assert outcome.promotion_result is not None
    assert outcome.promotion_result.contact_outcome is ContactPromotionOutcome.CONTACT_CREATED

    contact = db_session.get(Contact, outcome.promotion.promoted_contact_id)
    assert contact is not None
    assert contact.company_domain == DOMAIN


def test_an_identity_matched_company_page_resolves_without_the_provider(
    db_session: Session, capture: LinkedInProfileSnapshot, enable_capture: None
) -> None:
    """First-party evidence under an exact identity join stands on its own.

    The company page is not a provider guess; it is what the operator saw on
    LinkedIn's page for this exact company id. Spending a provider call to
    confirm it would be spending money to learn nothing.
    """

    _company_page(db_session)
    transport = CountingTransport({"domain": OTHER_DOMAIN, "name": "Something Else"})

    outcome = resolve(db_session, capture, transport, promote=True)

    assert outcome.decision is not None
    assert outcome.decision.decision is DomainResolutionDecision.AUTO_CONFIRMED
    assert outcome.decision.domain == DOMAIN
    assert policy.Reason.COMPANY_PAGE_IDENTITY_MATCH in outcome.decision.reasons
    assert transport.calls == 0, "an already-answered question must not be asked again"
    assert outcome.provider_called is False
    assert outcome.promoted is True


# =============================================================================
# 2. One weak, uncorroborated candidate enters review
# =============================================================================


def test_a_lone_provider_candidate_is_not_trusted(
    db_session: Session, capture: LinkedInProfileSnapshot, enable_capture: None
) -> None:
    """Being the only result is not evidence of being the right result.

    This is the case the whole policy is shaped around: a single plausible
    domain, nothing to check it against. It goes to review with a recommendation
    an operator can accept in one click — but nobody's mail is sent to it first.
    """

    transport = CountingTransport({"domain": DOMAIN, "name": COMPANY_NAME})

    outcome = resolve(db_session, capture, transport, promote=True)

    assert outcome.decision is not None
    assert outcome.decision.decision is DomainResolutionDecision.REVIEW_REQUIRED
    assert outcome.decision.domain is None
    assert policy.Reason.PROVIDER_CANDIDATE_UNCORROBORATED in outcome.decision.reasons
    assert outcome.applied is False
    assert outcome.promoted is False

    record = _record(db_session, capture)
    assert record.confirmation_status is EnrichmentConfirmationStatus.UNCONFIRMED
    assert record.confirmed_domain is None
    # The recommendation is surfaced but never applied.
    assert record.resolution_recommendation == DOMAIN


def test_an_exact_name_match_alone_does_not_auto_confirm(
    db_session: Session, capture: LinkedInProfileSnapshot, enable_capture: None
) -> None:
    """Name agreement is circular corroboration and must not decide.

    The provider was asked using the company name, so a candidate whose brand
    name and domain label both echo that name has told us nothing we did not
    already put in. It is recorded, and it is not enough.
    """

    transport = CountingTransport({"domain": "meridianworks.com", "name": COMPANY_NAME})

    outcome = resolve(db_session, capture, transport)

    assert outcome.decision is not None
    assert outcome.decision.decision is DomainResolutionDecision.REVIEW_REQUIRED
    assert outcome.applied is False


# =============================================================================
# 3. Several candidates, one decisively corroborated
# =============================================================================


def test_one_corroborated_candidate_among_several_auto_confirms(
    db_session: Session, capture: LinkedInProfileSnapshot, enable_capture: None
) -> None:
    """A crowded candidate list is not ambiguity when one candidate is checkable."""

    _company_page(db_session, domain=OTHER_DOMAIN)
    transport = CountingTransport(
        {"domain": DOMAIN, "name": COMPANY_NAME},
        {"domain": OTHER_DOMAIN, "name": "Meridian Works Group"},
        {"domain": "meridianworks.co", "name": "Meridian"},
    )

    outcome = resolve(db_session, capture, transport, promote=True)

    assert outcome.decision is not None
    assert outcome.decision.decision is DomainResolutionDecision.AUTO_CONFIRMED
    assert outcome.decision.domain == OTHER_DOMAIN, (
        "the corroborated candidate wins, not the top-ranked one"
    )
    assert outcome.promoted is True


# =============================================================================
# 4. Several plausible candidates remain unresolved
# =============================================================================


def test_several_uncorroborated_candidates_stay_unresolved(
    db_session: Session, capture: LinkedInProfileSnapshot, enable_capture: None
) -> None:
    transport = CountingTransport(
        {"domain": DOMAIN, "name": COMPANY_NAME},
        {"domain": OTHER_DOMAIN, "name": "Meridian Works Group"},
        {"domain": "meridianworks.co", "name": "Meridian"},
    )

    outcome = resolve(db_session, capture, transport, promote=True)

    assert outcome.decision is not None
    assert outcome.decision.decision is DomainResolutionDecision.REVIEW_REQUIRED
    assert policy.Reason.PROVIDER_MULTIPLE_CANDIDATES in outcome.decision.reasons
    assert outcome.applied is False
    assert outcome.promoted is False


# =============================================================================
# 5. A compatible prior mapping is reused, with zero provider calls
# =============================================================================


def test_a_prior_mapping_is_reused_without_touching_the_provider(
    db_session: Session, capture: LinkedInProfileSnapshot, enable_capture: None
) -> None:
    """An operator decision, once made, should not have to be made again.

    Replaying it is also the cheapest possible resolution: the question is
    answered before the provider is consulted, so a company costs at most one
    lookup no matter how many people are captured from it.
    """

    _prior_confirmation(db_session, domain=DOMAIN)
    transport = CountingTransport({"domain": OTHER_DOMAIN, "name": "Anything"})

    outcome = resolve(db_session, capture, transport, promote=True)

    assert outcome.decision is not None
    assert outcome.decision.decision is DomainResolutionDecision.PRIOR_MAPPING_REUSED
    assert outcome.decision.domain == DOMAIN
    assert transport.calls == 0
    assert outcome.provider_called is False

    record = _record(db_session, capture)
    assert record.confirmation_source is EnrichmentConfirmationSource.PRIOR_MAPPING
    assert record.lookup_attempts == 0
    assert outcome.promotion is not None
    assert outcome.promotion.company_outcome is CompanyResolutionOutcome.EXISTING_COMPANY_RESOLVED


# =============================================================================
# 6. Conflicting prior mappings produce review, never a selection
# =============================================================================


def test_conflicting_prior_mappings_go_to_review(
    db_session: Session, capture: LinkedInProfileSnapshot, enable_capture: None
) -> None:
    """Two earlier decisions disagreeing is a question, not a tie to break."""

    _prior_confirmation(db_session, domain=DOMAIN)
    _prior_confirmation(db_session, domain=OTHER_DOMAIN)

    transport = CountingTransport({"domain": DOMAIN, "name": COMPANY_NAME})
    outcome = resolve(db_session, capture, transport, promote=True)

    assert outcome.decision is not None
    assert outcome.decision.decision is DomainResolutionDecision.CONFLICT
    assert outcome.decision.domain is None
    assert policy.Reason.PRIOR_MAPPING_CONFLICT in outcome.decision.reasons
    assert outcome.applied is False
    assert outcome.promoted is False


def test_a_company_page_contradicting_a_prior_mapping_is_a_conflict(
    db_session: Session, capture: LinkedInProfileSnapshot, enable_capture: None
) -> None:
    """Authoritative sources disagreeing is checked before anything is selected.

    Preferring one source by rule would resolve the case quietly and wrongly
    half the time. The disagreement is the finding.
    """

    _prior_confirmation(db_session, domain=DOMAIN)
    _company_page(db_session, domain=OTHER_DOMAIN)

    outcome = resolve(db_session, capture, CountingTransport())

    assert outcome.decision is not None
    assert outcome.decision.decision is DomainResolutionDecision.CONFLICT
    assert policy.Reason.AUTHORITATIVE_CONFLICT in outcome.decision.reasons
    assert outcome.applied is False


# =============================================================================
# 7. Provider unavailable produces no fabricated domain
# =============================================================================


def test_an_unreachable_provider_never_produces_a_domain(
    db_session: Session, capture: LinkedInProfileSnapshot, enable_capture: None
) -> None:
    transport = FailingTransport()

    outcome = resolve(db_session, capture, transport, promote=True)

    assert outcome.decision is not None
    assert outcome.decision.decision is DomainResolutionDecision.PROVIDER_UNAVAILABLE
    assert outcome.decision.domain is None
    assert outcome.applied is False
    assert outcome.promoted is False

    record = _record(db_session, capture)
    assert record.confirmed_domain is None
    assert record.lookup_status is EnrichmentLookupStatus.API_UNAVAILABLE


def test_a_provider_with_no_matches_is_distinct_from_an_unreachable_one(
    db_session: Session, capture: LinkedInProfileSnapshot, enable_capture: None
) -> None:
    """ "Nothing exists" and "we could not ask" call for different next actions."""

    outcome = resolve(db_session, capture, CountingTransport(), promote=True)

    assert outcome.decision is not None
    assert outcome.decision.decision is DomainResolutionDecision.NO_CREDIBLE_CANDIDATE
    assert outcome.decision.domain is None
    assert outcome.applied is False


# =============================================================================
# 8. Retry is idempotent
# =============================================================================


def test_retrying_resolution_and_promotion_changes_nothing(
    db_session: Session, capture: LinkedInProfileSnapshot, enable_capture: None
) -> None:
    """Running it three times must cost one lookup and create one of everything."""

    _company_page(db_session)
    transport = CountingTransport({"domain": DOMAIN, "name": COMPANY_NAME})

    first = resolve(db_session, capture, transport, promote=True)
    assert first.promoted is True
    contact_id = first.promotion.promoted_contact_id if first.promotion else None

    second = resolve(db_session, capture, transport, promote=True)
    third = resolve(db_session, capture, transport, promote=True)

    for repeat in (second, third):
        assert repeat.applied is False, "an existing decision is not re-made"
        assert repeat.provider_called is False
        assert repeat.promotion is not None
        assert repeat.promotion.promoted_contact_id == contact_id
        assert repeat.promotion_result is not None
        assert repeat.promotion_result.contact_outcome is ContactPromotionOutcome.ALREADY_PROMOTED

    assert db_session.scalar(select(Contact).where(Contact.id == contact_id)) is not None
    assert len(db_session.scalars(select(Contact)).all()) == 1
    assert len(db_session.scalars(select(Company)).all()) == 1
    assert len(db_session.scalars(select(SalesNavCompanyEnrichment)).all()) == 1
    assert len(resolution.pending_reviews(db_session)) == 0


def test_retrying_an_unresolved_capture_does_not_duplicate_the_review_item(
    db_session: Session, capture: LinkedInProfileSnapshot, enable_capture: None
) -> None:
    transport = CountingTransport({"domain": DOMAIN, "name": COMPANY_NAME})

    resolve(db_session, capture, transport)
    resolve(db_session, capture, transport)

    items = resolution.pending_reviews(db_session)
    assert len(items) == 1
    assert items[0].capture_id == capture.id
    assert transport.calls == 1, "the lookup is not repeated on a retry"


# =============================================================================
# 9. Suppression and identity ambiguity still block promotion
# =============================================================================


def test_a_suppressed_domain_blocks_an_automatically_resolved_promotion(
    db_session: Session, capture: LinkedInProfileSnapshot, enable_capture: None
) -> None:
    """Automation decides the domain. It does not decide who may be contacted."""

    add_suppression(
        db_session,
        suppression_type=SuppressionType.DOMAIN,
        value=DOMAIN,
        reason=SuppressionReason.COMPETITOR,
        actor="operator",
    )
    _company_page(db_session)

    outcome = resolve(db_session, capture, CountingTransport(), promote=True)

    assert outcome.decision is not None
    assert outcome.decision.decision is DomainResolutionDecision.AUTO_CONFIRMED
    assert outcome.promoted is False
    assert outcome.promotion_result is not None
    assert outcome.promotion_result.contact_outcome is ContactPromotionOutcome.SUPPRESSED
    assert db_session.scalars(select(Contact)).all() == []


def test_an_ambiguous_identity_blocks_an_automatically_resolved_promotion(
    db_session: Session, capture: LinkedInProfileSnapshot, enable_capture: None
) -> None:
    """Two contacts already claim this profile URL; the policy cannot pick one."""

    url = capture.normalized_profile_url
    assert url is not None
    for suffix in ("a", "b"):
        db_session.add(
            Contact(
                first_name="Morgan",
                last_name="Vale",
                company_name=COMPANY_NAME,
                company_domain=DOMAIN,
                linkedin_url=url,
                natural_key=f"morgan|vale|{suffix}.example",
            )
        )
    db_session.flush()
    _company_page(db_session)

    outcome = resolve(db_session, capture, CountingTransport(), promote=True)

    assert outcome.applied is True
    assert outcome.promoted is False
    assert outcome.promotion_result is not None
    assert (
        outcome.promotion_result.contact_outcome
        is ContactPromotionOutcome.CONTACT_IDENTITY_AMBIGUOUS
    )


# =============================================================================
# 10. Policy version, evidence and reasons are preserved
# =============================================================================


def test_every_decision_records_its_policy_version_evidence_and_reasons(
    db_session: Session, capture: LinkedInProfileSnapshot, enable_capture: None
) -> None:
    """A decision nobody can re-explain later is not an explainable decision."""

    _company_page(db_session)
    transport = CountingTransport({"domain": DOMAIN, "name": COMPANY_NAME})

    resolve(db_session, capture, transport)
    record = _record(db_session, capture)

    assert record.resolution_policy_version == policy.POLICY_VERSION
    assert record.resolution_decision is DomainResolutionDecision.AUTO_CONFIRMED
    assert record.resolution_reasons
    assert record.resolved_at is not None

    evidence = record.resolution_evidence or []
    assert evidence, "the evidence that produced the decision must be stored"
    axes = {item["axis"] for item in evidence}
    assert policy.EvidenceAxis.COMPANY_PAGE in axes
    for item in evidence:
        assert set(item) >= {"domain", "axis", "identity_matched", "notes", "detail"}


def test_a_review_decision_is_recorded_as_durably_as_an_automatic_one(
    db_session: Session, capture: LinkedInProfileSnapshot, enable_capture: None
) -> None:
    """The Review Queue (#172) consumes these rows, so they cannot be thin."""

    transport = CountingTransport(
        {"domain": DOMAIN, "name": COMPANY_NAME},
        {"domain": OTHER_DOMAIN, "name": "Meridian Works Group"},
    )
    resolve(db_session, capture, transport)

    items = resolution.pending_reviews(db_session)
    assert len(items) == 1
    item = items[0]
    assert item.subject_type == "company_domain"
    assert item.blocked_action == "contact_promotion"
    assert item.decision is DomainResolutionDecision.REVIEW_REQUIRED
    assert item.reason_codes
    assert item.evidence
    assert item.recommendation in {DOMAIN, OTHER_DOMAIN}
    assert item.policy_version == policy.POLICY_VERSION
    assert item.reusable is True
    assert item.as_dict()["subject_id"] == str(item.subject_id)


# =============================================================================
# 11. Automatic promotion cannot duplicate a Contact or a Company
# =============================================================================


def test_an_existing_company_is_reused_not_duplicated(
    db_session: Session, capture: LinkedInProfileSnapshot, enable_capture: None
) -> None:
    existing = Company(name=COMPANY_NAME, domain=DOMAIN)
    db_session.add(existing)
    db_session.flush()

    _company_page(db_session)
    resolve(db_session, capture, CountingTransport(), promote=True)

    companies = db_session.scalars(select(Company)).all()
    assert len(companies) == 1
    assert companies[0].id == existing.id


def test_an_existing_contact_is_linked_not_duplicated(
    db_session: Session, capture: LinkedInProfileSnapshot, enable_capture: None
) -> None:
    url = capture.normalized_profile_url
    existing = Contact(
        first_name="Morgan",
        last_name="Vale",
        company_name=COMPANY_NAME,
        company_domain=DOMAIN,
        linkedin_url=url,
        natural_key=f"morgan|vale|{DOMAIN}",
    )
    db_session.add(existing)
    db_session.flush()

    _company_page(db_session)
    outcome = resolve(db_session, capture, CountingTransport(), promote=True)

    assert outcome.promotion_result is not None
    assert (
        outcome.promotion_result.contact_outcome
        is ContactPromotionOutcome.CONTACT_EXACT_MATCH_LINKED
    )
    assert len(db_session.scalars(select(Contact)).all()) == 1
    assert outcome.promotion is not None
    assert outcome.promotion.promoted_contact_id == existing.id


# =============================================================================
# 12. Existing manual behaviour and historical DAT-014 records stay compatible
# =============================================================================


def test_an_operator_decision_is_never_overwritten(
    db_session: Session, capture: LinkedInProfileSnapshot, enable_capture: None
) -> None:
    """Automation fills gaps. It does not overrule the person it works for."""

    promo.ensure_records(db_session, capture)
    promo.confirm_domain(
        db_session,
        snapshot=capture,
        source=EnrichmentConfirmationSource.MANUAL,
        domain=OTHER_DOMAIN,
        actor="operator",
    )
    _company_page(db_session, domain=DOMAIN)

    outcome = resolve(db_session, capture, CountingTransport(), promote=True)

    assert outcome.applied is False
    assert outcome.skipped_reason == "already decided by an operator"
    record = _record(db_session, capture)
    assert record.confirmed_domain == OTHER_DOMAIN
    assert record.confirmation_source is EnrichmentConfirmationSource.MANUAL


def test_an_operator_marked_unresolved_capture_is_left_alone(
    db_session: Session, capture: LinkedInProfileSnapshot, enable_capture: None
) -> None:
    promo.ensure_records(db_session, capture)
    promo.confirm_domain(
        db_session,
        snapshot=capture,
        source=EnrichmentConfirmationSource.UNRESOLVED,
        domain=None,
        actor="operator",
    )
    _company_page(db_session)

    outcome = resolve(db_session, capture, CountingTransport(), promote=True)

    assert outcome.applied is False
    assert outcome.promoted is False
    record = _record(db_session, capture)
    assert record.confirmation_status is EnrichmentConfirmationStatus.UNRESOLVED


def test_a_historical_record_with_no_policy_decision_still_promotes(
    db_session: Session, capture: LinkedInProfileSnapshot, enable_capture: None
) -> None:
    """Rows written before DAT-017 have a NULL decision and must keep working.

    This is the compatibility guarantee that lets the migration be additive: the
    policy columns describe a conclusion, they do not gate the path.
    """

    promo.ensure_records(db_session, capture)
    record = _record(db_session, capture)
    assert record.resolution_decision is None

    promo.confirm_domain(
        db_session,
        snapshot=capture,
        source=EnrichmentConfirmationSource.MANUAL,
        domain=DOMAIN,
        actor="operator",
    )
    result = promo.promote(db_session, snapshot=capture, actor="operator")

    assert result.promoted is True
    assert result.company_outcome is CompanyResolutionOutcome.DOMAIN_CANDIDATE_CONFIRMED
    assert record.resolution_decision is None


def test_correcting_an_automatic_domain_is_recorded_and_counted(
    db_session: Session, capture: LinkedInProfileSnapshot, enable_capture: None
) -> None:
    """The correction rate is the number that keeps the automatic rate honest."""

    _company_page(db_session)
    resolve(db_session, capture, CountingTransport())

    record = _record(db_session, capture)
    assert record.confirmation_source is EnrichmentConfirmationSource.AUTOMATIC_POLICY

    promo.confirm_domain(
        db_session,
        snapshot=capture,
        source=EnrichmentConfirmationSource.MANUAL,
        domain=OTHER_DOMAIN,
        actor="operator",
    )

    assert record.confirmed_domain == OTHER_DOMAIN
    assert record.resolution_corrected_from == DOMAIN
    assert record.resolution_corrected_at is not None

    stats = resolution.metrics(db_session)
    assert stats.automatic == 1
    assert stats.corrections == 1
    assert stats.correction_rate == 1.0


def test_reaffirming_the_same_automatic_domain_is_not_a_correction(
    db_session: Session, capture: LinkedInProfileSnapshot, enable_capture: None
) -> None:
    """Agreement is not a correction; counting it would slander the policy."""

    _company_page(db_session)
    resolve(db_session, capture, CountingTransport())

    promo.confirm_domain(
        db_session,
        snapshot=capture,
        source=EnrichmentConfirmationSource.MANUAL,
        domain=DOMAIN,
        actor="operator",
    )

    record = _record(db_session, capture)
    assert record.resolution_corrected_at is None
    assert resolution.metrics(db_session).corrections == 0


# =============================================================================
# Metrics
# =============================================================================


def test_metrics_report_automatic_review_and_provider_figures(
    db_session: Session, enable_capture: None
) -> None:
    # The review case is resolved first, while no company page exists to
    # corroborate anything — two provider candidates and nothing to check them
    # against.
    review_capture = _stage(db_session, PROFILE_SUBMISSION)[0]
    resolve(
        db_session,
        review_capture,
        CountingTransport(
            {"domain": "unrelated-a.example", "name": "Unrelated A"},
            {"domain": "unrelated-b.example", "name": "Unrelated B"},
        ),
    )

    # Then the company page arrives, and the next capture resolves itself.
    auto_capture = _stage(db_session, PROFILE_SUBMISSION)[0]
    _company_page(db_session)
    resolve(db_session, auto_capture, CountingTransport())

    stats = resolution.metrics(db_session)

    assert stats.decided == 2
    assert stats.automatic == 1
    assert stats.review == 1
    assert stats.automatic_rate == 0.5
    assert stats.review_rate == 0.5
    assert stats.provider_calls == 1, "only the review case needed the provider"
    assert stats.records_with_provider_call == 1
    assert stats.by_decision[DomainResolutionDecision.AUTO_CONFIRMED.value] == 1
    assert stats.as_dict()["automatic_rate"] == 0.5


# =============================================================================
# The feature gate
# =============================================================================


def test_nothing_happens_while_automatic_resolution_is_disabled(
    db_session: Session,
    capture: LinkedInProfileSnapshot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off means untouched, not "the same path with a smaller policy"."""

    monkeypatch.setenv("FEATURES__CONTACT_CAPTURE_INTAKE", "true")
    monkeypatch.setenv("FEATURES__CONTACT_CAPTURE_PROMOTION", "true")
    monkeypatch.setenv("FEATURES__AUTOMATIC_DOMAIN_RESOLUTION", "false")
    get_settings.cache_clear()
    try:
        _company_page(db_session)
        transport = CountingTransport({"domain": DOMAIN, "name": COMPANY_NAME})

        outcome = resolve(db_session, capture, transport, promote=True)

        assert outcome.decision is None
        assert outcome.applied is False
        assert outcome.promoted is False
        assert outcome.skipped_reason == "automatic domain resolution is disabled"
        assert transport.calls == 0
        assert db_session.scalars(select(SalesNavCompanyEnrichment)).all() == []
        assert db_session.scalars(select(Contact)).all() == []
    finally:
        get_settings.cache_clear()


# =============================================================================
# The workbench route
# =============================================================================


@pytest.fixture()
def workbench(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__CONTACT_CAPTURE_INTAKE", "true")
    monkeypatch.setenv("FEATURES__CONTACT_CAPTURE_PROMOTION", "true")
    monkeypatch.setenv("FEATURES__AUTOMATIC_DOMAIN_RESOLUTION", "true")
    get_settings.cache_clear()
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_the_resolve_route_resolves_and_promotes(
    db_session: Session, workbench: TestClient
) -> None:
    """The operator-facing entry point, with no provider key configured.

    Worth covering precisely because the provider is absent: the captured
    company page is enough on its own, which is the case that makes automation
    cheap as well as safe.
    """

    capture_row = _stage(db_session, PROFILE_SUBMISSION)[0]
    _company_page(db_session)

    response = workbench.post(f"/contact-captures/{capture_row.id}/resolve", follow_redirects=False)

    assert response.status_code in (302, 303)
    assert "contact+promoted" in response.headers["location"].replace("%20", "+")

    record = _record(db_session, capture_row)
    assert record.confirmation_source is EnrichmentConfirmationSource.AUTOMATIC_POLICY
    assert record.confirmed_domain == DOMAIN


def test_the_resolve_route_reports_an_unresolved_case_without_applying_it(
    db_session: Session, workbench: TestClient
) -> None:
    capture_row = _stage(db_session, PROFILE_SUBMISSION)[0]

    response = workbench.post(f"/contact-captures/{capture_row.id}/resolve", follow_redirects=False)

    assert response.status_code in (302, 303)
    assert "err=" in response.headers["location"]
    record = _record(db_session, capture_row)
    assert record.confirmation_status is EnrichmentConfirmationStatus.UNCONFIRMED
    assert record.confirmed_domain is None


# =============================================================================
# Policy unit tests — the decision table, without a database
# =============================================================================


def _input(*evidence: policy.DomainEvidence, status: str = "OK") -> policy.ResolutionInput:
    return policy.ResolutionInput(
        company_key="acme",
        company_name="Acme",
        company_linkedin_id="acme",
        lookup_status=EnrichmentLookupStatus[status],
        evidence=evidence,
    )


def test_policy_never_counts_two_notes_on_one_axis_as_corroboration() -> None:
    """Both name signals on a single provider candidate is still one axis."""

    decision = policy.decide(
        _input(
            policy.DomainEvidence(
                domain="acme.com",
                axis=policy.EvidenceAxis.PROVIDER_CANDIDATE,
                notes=(policy.Reason.BRAND_NAME_AGREEMENT, policy.Reason.DOMAIN_LABEL_AGREEMENT),
            )
        )
    )
    assert decision.decision is DomainResolutionDecision.REVIEW_REQUIRED


def test_policy_is_deterministic_for_the_same_evidence() -> None:
    evidence = (
        policy.DomainEvidence(domain="acme.com", axis=policy.EvidenceAxis.PROVIDER_CANDIDATE),
        policy.DomainEvidence(
            domain="acme.com", axis=policy.EvidenceAxis.COMPANY_PAGE, identity_matched=True
        ),
    )
    first = policy.decide(_input(*evidence))
    second = policy.decide(_input(*reversed(evidence)))
    assert first.decision is second.decision
    assert first.domain == second.domain == "acme.com"


def test_brand_and_label_agreement_tolerate_legal_suffixes() -> None:
    assert policy.brand_name_agrees("Acme Ltd", "Acme")
    assert policy.domain_label_agrees("Acme Holdings", "acme.com")
    assert not policy.brand_name_agrees("Acme", "Acme Systems")
    assert not policy.domain_label_agrees("Acme", "acmecorp-shop.com")
