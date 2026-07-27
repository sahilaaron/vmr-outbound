"""Automatic company-domain resolution tests (DAT-017A).

Two halves, matching the code:

* the **policy** is pure, so its tests are pure — evidence in, decision out, no
  database and no provider;
* the **service** runs against live Postgres and a stubbed provider, because
  what it has to prove is about persistence: that a decision is stored with
  enough evidence to explain it, that retries and recalculation write nothing
  new, that a correction supersedes rather than erases, and that the permanent
  Company and Contact end up linked exactly once.

The provider is always stubbed. No test needs an API key, and none can spend a
lookup.

The product guarantees under test are the ones issue #171 names: a domain is
never fabricated; provider rank alone never confirms; confirmed comes only from
evidence already on record; ambiguous, conflicting, invalid, unsuitable, missing
and failed cases all stay unresolved and say why; every decision keeps its
candidates, reasons, warnings, policy version and whether it spent a provider
call; and a provisional domain opens company research and nothing else.
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
from app.models.company_domain_resolution import CompanyDomainResolution
from app.models.contact import Contact
from app.models.enums import (
    CompanyResolutionOutcome,
    ContactPromotionOutcome,
    DomainResolutionKind,
    DomainResolutionState,
    EnrichmentConfirmationSource,
    EnrichmentConfirmationStatus,
    EnrichmentLookupStatus,
)
from app.models.linkedin_profile import LinkedInProfileSnapshot
from app.models.salesnav_enrichment import SalesNavCompanyEnrichment
from app.services.captures import intake as capture_intake
from app.services.captures import promotion as promo
from app.services.enrichment import logodev
from app.services.resolution import gates, policy, store
from app.services.resolution import service as resolution
from sqlalchemy import func, select
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parents[1]
CAPTURE_FIXTURES = REPO_ROOT / "extensions" / "salesnav-capture" / "docs" / "fixtures"
PROFILE_SUBMISSION = json.loads(
    (CAPTURE_FIXTURES / "contact-capture.profile.example.json").read_text("utf-8")
)

#: Sanitized provider responses. Kept in a fixture file rather than inline so the
#: policy is proved against realistic provider output — including the awkward
#: shapes — instead of against dicts written to match what it expects.
PROVIDER_SAMPLES: dict[str, Any] = json.loads(
    (Path(__file__).parent / "fixtures" / "logodev_brand_search_sanitized.json").read_text("utf-8")
)

LOOPBACK = "http://127.0.0.1:8000"
COMPANY = "Meridian Works"
DOMAIN = "meridianworks.example"


# --- Provider stubs -----------------------------------------------------------


def transport_body(body: list[dict[str, Any]]) -> logodev.Transport:
    def _transport(url: str, headers: Any, timeout: float) -> logodev.RawResponse:
        assert "Authorization" in headers, "the client must authenticate"
        return logodev.RawResponse(status_code=200, body=json.dumps(body))

    return _transport


def transport_sample(name: str) -> logodev.Transport:
    """A stub answering with one sanitized sample from the fixture file."""

    return transport_body(PROVIDER_SAMPLES[name]["body"])


def transport_failing() -> logodev.Transport:
    def _transport(url: str, headers: Any, timeout: float) -> logodev.RawResponse:
        raise logodev.TransportError("logo.dev request failed: URLError")

    return _transport


class CountingTransport:
    """A stub that records how many times the provider was actually called.

    "We do not re-buy what we already know" is a claim about spending, so it is
    tested by counting calls rather than by inspecting a flag the code sets for
    itself.
    """

    def __init__(self, body: list[dict[str, Any]]) -> None:
        self.body = body
        self.calls = 0

    def __call__(self, url: str, headers: Any, timeout: float) -> logodev.RawResponse:
        self.calls += 1
        return logodev.RawResponse(status_code=200, body=json.dumps(self.body))


def access(transport: logodev.Transport | None) -> resolution.ProviderAccess:
    return resolution.ProviderAccess(
        api_key="test-key-never-real",
        search_url="https://api.logo.dev/search",
        timeout=5.0,
        max_candidates=10,
        transport=transport,
    )


NO_PROVIDER = resolution.ProviderAccess()


# --- Fixtures -----------------------------------------------------------------


@pytest.fixture()
def enable_resolution(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("FEATURES__CONTACT_CAPTURE_INTAKE", "true")
    monkeypatch.setenv("FEATURES__CONTACT_CAPTURE_PROMOTION", "true")
    monkeypatch.setenv("FEATURES__AUTOMATIC_COMPANY_DOMAIN_RESOLUTION", "true")
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
    """One unmatched profile capture: Morgan Vale at Meridian Works."""

    return _stage(db_session, PROFILE_SUBMISSION)[0]


@pytest.fixture()
def second_capture(db_session: Session) -> LinkedInProfileSnapshot:
    """A second, independent capture of the same employer."""

    return _stage(db_session, PROFILE_SUBMISSION)[0]


def _company(
    db: Session, *, name: str, domain: str | None, linkedin_id: str | None = None
) -> Company:
    company = Company(name=name, domain=domain, linkedin_company_id=linkedin_id)
    db.add(company)
    db.flush()
    return company


def _approved_mapping(
    db: Session, *, key: str, name: str, domain: str
) -> SalesNavCompanyEnrichment:
    """A company an operator already confirmed elsewhere — an approved mapping."""

    record = SalesNavCompanyEnrichment(
        capture_id=_stage(db, PROFILE_SUBMISSION)[0].id,
        company_key=key,
        company_name=name,
        row_count=1,
        lookup_status=EnrichmentLookupStatus.OK,
        confirmation_status=EnrichmentConfirmationStatus.CONFIRMED,
        confirmed_domain=domain,
        confirmation_source=EnrichmentConfirmationSource.CANDIDATE,
        confirmed_by="operator",
        lookup_attempts=1,
    )
    db.add(record)
    db.flush()
    return record


def evidence(**kwargs: Any) -> policy.ResolutionEvidence:
    """Policy evidence with sensible defaults for the fields a test ignores."""

    base: dict[str, Any] = {
        "company_name": COMPANY,
        "normalized_company_name": policy.normalize_company_name(COMPANY),
    }
    base.update(kwargs)
    return policy.ResolutionEvidence(**base)


def candidates_from(name: str) -> tuple[dict[str, Any], ...]:
    """A stored candidate set as DAT-010 would have written it, from a sample."""

    return tuple(
        {"domain": brand["domain"], "name": brand.get("name"), "rank": index, "confidence": None}
        for index, brand in enumerate(PROVIDER_SAMPLES[name]["body"], start=1)
    )


# =============================================================================
# The policy — pure, no database
# =============================================================================


class TestNormalization:
    def test_a_company_name_folds_past_case_punctuation_and_legal_form(self) -> None:
        folded = {
            policy.normalize_company_name(value)
            for value in (
                "Meridian Works",
                "meridian  works",
                "Meridian Works, Inc.",
                "MERIDIAN-WORKS",
            )
        }
        assert folded == {"meridianworks"}, "these all name the same organisation"

    def test_a_name_that_is_only_a_legal_form_survives(self) -> None:
        """Stripping suffixes must never strip a name down to nothing.

        A company genuinely called "Limited" would otherwise fold to the empty
        string, which the policy reads as "no company name" — turning a real
        employer into a capture with nothing to resolve.
        """

        assert policy.normalize_company_name("Limited") == "limited"

    def test_a_registrable_label_ignores_the_public_suffix(self) -> None:
        assert policy.registrable_label("meridianworks.example") == "meridianworks"
        assert policy.registrable_label("meridian-works.co.uk") == "meridianworks"

    @pytest.mark.parametrize(
        ("domain", "expected"),
        [
            ("linkedin.com", policy.REJECTED_SOCIAL_DOMAIN),
            ("crunchbase.com", policy.REJECTED_DIRECTORY_DOMAIN),
            ("amazon.com", policy.REJECTED_MARKETPLACE_DOMAIN),
            ("gmail.com", policy.REJECTED_GENERIC_PLATFORM_DOMAIN),
            ("hugedomains.com", policy.REJECTED_PARKED_DOMAIN),
            # A subdomain of a platform is the platform, not a company.
            ("meridianworks.wixsite.com", policy.REJECTED_GENERIC_PLATFORM_DOMAIN),
            ("meridianworks.example", None),
        ],
    )
    def test_unsuitable_domains_are_named_with_their_reason(
        self, domain: str, expected: str | None
    ) -> None:
        assert policy.unsuitable_reason(domain) == expected


class TestEstablishedEvidence:
    def test_one_approved_mapping_confirms_without_consulting_the_provider(self) -> None:
        decision = policy.evaluate(
            evidence(
                approved_mapping_domains=frozenset({DOMAIN}),
                candidates=candidates_from("clean_single_match"),
            )
        )
        assert decision.state is DomainResolutionState.CONFIRMED
        assert decision.selected_domain == DOMAIN
        assert policy.REASON_REUSED_APPROVED_MAPPING in decision.reasons
        assert decision.candidates == (), "the provider was never consulted, so nothing was judged"

    def test_two_approved_mappings_disagreeing_stays_unresolved(self) -> None:
        decision = policy.evaluate(
            evidence(approved_mapping_domains=frozenset({DOMAIN, "meridian.example"}))
        )
        assert decision.state is DomainResolutionState.UNRESOLVED
        assert decision.selected_domain is None
        assert policy.REASON_CONFLICTING_APPROVED_MAPPINGS in decision.reasons

    def test_an_existing_company_with_the_same_normalized_name_confirms(self) -> None:
        decision = policy.evaluate(
            evidence(
                existing_companies=(
                    policy.ExistingCompanyMatch(
                        company_id=uuid.uuid4(),
                        name="Meridian Works Inc.",
                        domain=DOMAIN,
                        matched_on="normalized_name",
                    ),
                )
            )
        )
        assert decision.state is DomainResolutionState.CONFIRMED
        assert decision.selected_domain == DOMAIN
        assert policy.REASON_MATCHED_EXISTING_COMPANY_NAME in decision.reasons

    def test_a_linkedin_identifier_match_is_reported_as_its_own_reason(self) -> None:
        decision = policy.evaluate(
            evidence(
                linkedin_company_id="meridian-works",
                existing_companies=(
                    policy.ExistingCompanyMatch(
                        company_id=uuid.uuid4(),
                        name="Meridian Works",
                        domain=DOMAIN,
                        matched_on="linkedin_company_id",
                    ),
                ),
            )
        )
        assert policy.REASON_MATCHED_EXISTING_COMPANY_LINKEDIN in decision.reasons

    def test_two_existing_companies_with_different_domains_stay_unresolved(self) -> None:
        decision = policy.evaluate(
            evidence(
                existing_companies=(
                    policy.ExistingCompanyMatch(
                        company_id=uuid.uuid4(),
                        name="Meridian Works",
                        domain=DOMAIN,
                        matched_on="normalized_name",
                    ),
                    policy.ExistingCompanyMatch(
                        company_id=uuid.uuid4(),
                        name="Meridian Works",
                        domain="meridian.example",
                        matched_on="normalized_name",
                    ),
                )
            )
        )
        assert decision.state is DomainResolutionState.UNRESOLVED
        assert policy.REASON_CONFLICTING_EXISTING_COMPANIES in decision.reasons

    def test_a_mapping_contradicting_an_existing_company_stays_unresolved(self) -> None:
        """Two settled sources disagreeing is the worst case to guess in."""

        decision = policy.evaluate(
            evidence(
                approved_mapping_domains=frozenset({DOMAIN}),
                existing_companies=(
                    policy.ExistingCompanyMatch(
                        company_id=uuid.uuid4(),
                        name="Meridian Works",
                        domain="meridian.example",
                        matched_on="normalized_name",
                    ),
                ),
            )
        )
        assert decision.state is DomainResolutionState.UNRESOLVED
        assert policy.REASON_MAPPING_CONFLICTS_WITH_COMPANY in decision.reasons

    def test_no_company_name_resolves_to_nothing(self) -> None:
        decision = policy.evaluate(
            policy.ResolutionEvidence(company_name=None, normalized_company_name="")
        )
        assert decision.state is DomainResolutionState.UNRESOLVED
        assert policy.REASON_NO_COMPANY_NAME in decision.reasons

    def test_established_evidence_returns_none_when_only_the_provider_can_help(self) -> None:
        """The signal that authorizes spending a provider call, tested directly."""

        assert policy.evaluate_established_evidence(evidence()) is None
        assert (
            policy.evaluate_established_evidence(
                evidence(approved_mapping_domains=frozenset({DOMAIN}))
            )
            is not None
        ), "an approved mapping settles it, so no call is warranted"


class TestProviderCandidates:
    def test_a_single_clean_candidate_is_provisional_and_never_confirmed(self) -> None:
        decision = policy.evaluate(
            evidence(
                candidates=candidates_from("clean_single_match"),
                lookup_status=EnrichmentLookupStatus.OK,
                provider="logo.dev",
            )
        )
        assert decision.state is DomainResolutionState.PROVISIONAL
        assert decision.selected_domain == DOMAIN
        assert decision.provider_rank == 1
        assert policy.REASON_SINGLE_ALIGNED_CANDIDATE in decision.reasons
        assert policy.WARNING_PROVISIONAL_LIMITS in decision.warnings

    def test_provider_rank_alone_never_produces_confirmed(self) -> None:
        """The rule issue #171 states twice, tested as a property of the policy.

        Rank 1 is given to a candidate that does NOT align, and to one that does.
        Neither reaches confirmed: the aligned one is provisional, and the
        unaligned one selects nothing at all.
        """

        aligned = policy.evaluate(
            evidence(
                candidates=candidates_from("clean_single_match"),
                lookup_status=EnrichmentLookupStatus.OK,
            )
        )
        assert aligned.state is DomainResolutionState.PROVISIONAL
        assert policy.REASON_RANK_IS_NOT_CONFIRMATION in aligned.reasons

        top_ranked_but_wrong = policy.evaluate(
            policy.ResolutionEvidence(
                company_name="Harbourline Freight",
                normalized_company_name=policy.normalize_company_name("Harbourline Freight"),
                candidates=candidates_from("unrelated_top_rank"),
                lookup_status=EnrichmentLookupStatus.OK,
            )
        )
        assert top_ranked_but_wrong.state is DomainResolutionState.UNRESOLVED
        assert top_ranked_but_wrong.selected_domain is None
        assert policy.REASON_NO_ALIGNED_CANDIDATE in top_ranked_but_wrong.reasons

    def test_two_equally_plausible_candidates_stay_unresolved(self) -> None:
        decision = policy.evaluate(
            policy.ResolutionEvidence(
                company_name="Northwind Labs",
                normalized_company_name=policy.normalize_company_name("Northwind Labs"),
                candidates=candidates_from("two_plausible_matches"),
                lookup_status=EnrichmentLookupStatus.OK,
            )
        )
        assert decision.state is DomainResolutionState.UNRESOLVED
        assert policy.REASON_MULTIPLE_ALIGNED_CANDIDATES in decision.reasons
        assert len(decision.candidates) == 2, "both are kept with their evaluation"

    def test_platform_and_directory_candidates_are_all_rejected_with_reasons(self) -> None:
        decision = policy.evaluate(
            policy.ResolutionEvidence(
                company_name="Calder & Finch",
                normalized_company_name=policy.normalize_company_name("Calder & Finch"),
                candidates=candidates_from("platforms_and_directories"),
                lookup_status=EnrichmentLookupStatus.OK,
            )
        )
        assert decision.state is DomainResolutionState.UNRESOLVED
        assert decision.selected_domain is None
        reasons = {c.rejection_reason for c in decision.candidates}
        assert reasons == {
            policy.REJECTED_SOCIAL_DOMAIN,
            policy.REJECTED_DIRECTORY_DOMAIN,
            policy.REJECTED_GENERIC_PLATFORM_DOMAIN,
            policy.REJECTED_PARKED_DOMAIN,
        }
        assert policy.WARNING_CANDIDATES_REJECTED in decision.warnings

    def test_an_invalid_domain_is_rejected_rather_than_repaired(self) -> None:
        decision = policy.evaluate(
            policy.ResolutionEvidence(
                company_name="Ashgrove Systems",
                normalized_company_name=policy.normalize_company_name("Ashgrove Systems"),
                candidates=candidates_from("malformed_entry"),
                lookup_status=EnrichmentLookupStatus.OK,
            )
        )
        assert decision.state is DomainResolutionState.UNRESOLVED
        assert decision.candidates[0].rejection_reason == policy.REJECTED_INVALID_DOMAIN
        assert decision.candidates[0].eligible is False

    def test_an_empty_provider_answer_is_distinct_from_a_failed_one(self) -> None:
        searched = policy.evaluate(
            evidence(candidates=(), lookup_status=EnrichmentLookupStatus.NO_MATCH)
        )
        assert policy.REASON_PROVIDER_NO_CANDIDATES in searched.reasons

        failed = policy.evaluate(
            evidence(candidates=(), lookup_status=EnrichmentLookupStatus.API_UNAVAILABLE)
        )
        assert policy.REASON_PROVIDER_UNAVAILABLE in failed.reasons

        never_asked = policy.evaluate(
            evidence(candidates=(), lookup_status=EnrichmentLookupStatus.NOT_STARTED)
        )
        assert policy.REASON_PROVIDER_LOOKUP_NOT_RUN in never_asked.reasons

        assert {searched.state, failed.state, never_asked.state} == {
            DomainResolutionState.UNRESOLVED
        }

    def test_every_reason_and_warning_code_has_operator_facing_words(self) -> None:
        """A stored code an operator cannot read is a decision they cannot check."""

        reason_codes = {
            value
            for name, value in vars(policy).items()
            if name.startswith("REASON_") and isinstance(value, str)
        }
        warning_codes = {
            value
            for name, value in vars(policy).items()
            if name.startswith("WARNING_") and isinstance(value, str)
        }
        assert reason_codes <= set(policy.REASON_TEXT)
        assert warning_codes <= set(policy.WARNING_TEXT)

    def test_an_unknown_code_is_shown_rather_than_dropped(self) -> None:
        """A decision from a policy version this build lacks stays visible."""

        assert policy.explain(["from_a_future_policy"], table=policy.REASON_TEXT) == [
            "from_a_future_policy"
        ]


# =============================================================================
# The service — live Postgres, stubbed provider
# =============================================================================


class TestResolvingACapture:
    def test_a_clean_candidate_resolves_to_provisional_and_builds_the_company(
        self, db_session: Session, capture: LinkedInProfileSnapshot
    ) -> None:
        outcome = resolution.resolve(
            db_session, snapshot=capture, access=access(transport_sample("clean_single_match"))
        )

        assert outcome.state is DomainResolutionState.PROVISIONAL
        assert outcome.selected_domain == DOMAIN
        assert outcome.provider_call_made is True
        assert outcome.company is not None and outcome.company.domain == DOMAIN

        decision = store.current_decision(db_session, capture.id)
        assert decision is not None
        assert decision.decision_number == 1
        assert decision.decision_kind is DomainResolutionKind.AUTOMATIC
        assert decision.policy_version == policy.POLICY_VERSION
        assert decision.company_name_original == COMPANY
        assert decision.company_name_normalized == "meridianworks"
        assert decision.provider == "logo.dev"
        assert decision.provider_rank == 1
        assert decision.enrichment_id is not None, "the candidate evidence stays linked"
        assert decision.resolved_company_id == outcome.company.id
        assert decision.candidates, "the candidate set considered is preserved"

    def test_a_provisional_decision_never_becomes_an_approved_mapping(
        self, db_session: Session, capture: LinkedInProfileSnapshot
    ) -> None:
        """The laundering path, closed explicitly.

        If a provisional decision wrote a confirmation onto the DAT-010 record,
        the next capture at the same company would read it back as an approved
        mapping and confirm from evidence nobody ever confirmed.
        """

        resolution.resolve(
            db_session, snapshot=capture, access=access(transport_sample("clean_single_match"))
        )
        record = promo.get_enrichment(db_session, capture.id)
        assert record is not None
        assert record.confirmation_status is EnrichmentConfirmationStatus.UNCONFIRMED
        assert record.confirmed_domain is None

        assert (
            promo.prior_confirmed_domains(
                db_session,
                company_key_value=record.company_key,
                company_linkedin_id=record.company_linkedin_id,
                exclude_record_id=record.id,
            )
            == set()
        )

    def test_a_provisional_company_cannot_promote_a_later_capture_to_confirmed(
        self,
        db_session: Session,
        capture: LinkedInProfileSnapshot,
        second_capture: LinkedInProfileSnapshot,
    ) -> None:
        """The longer laundering route, closed.

        A provisional decision creates a permanent Company so research can start.
        Without a guard, the next capture at the same employer would find that
        Company, read "an existing company already has this domain" as settled
        evidence, and confirm — the original guess citing itself back.
        """

        first = resolution.resolve(
            db_session, snapshot=capture, access=access(transport_sample("clean_single_match"))
        )
        assert first.state is DomainResolutionState.PROVISIONAL
        assert first.company is not None

        second = resolution.resolve(
            db_session,
            snapshot=second_capture,
            access=access(transport_sample("clean_single_match")),
        )

        assert second.state is DomainResolutionState.PROVISIONAL, (
            "a company standing on a guess cannot settle another capture"
        )
        assert second.company is not None and second.company.id == first.company.id, (
            "it is still the same company — reused, not duplicated"
        )

        # Once an operator confirms it, the same evidence legitimately settles.
        resolution.correct(db_session, snapshot=capture, domain=DOMAIN, actor="operator")
        third = resolution.resolve(
            db_session, snapshot=second_capture, access=NO_PROVIDER, force=True
        )
        assert third.state is DomainResolutionState.CONFIRMED

    def test_an_approved_mapping_is_reused_without_another_provider_call(
        self, db_session: Session, capture: LinkedInProfileSnapshot
    ) -> None:
        _approved_mapping(
            db_session, key=promo.company_hints(capture).key, name=COMPANY, domain=DOMAIN
        )
        counting = CountingTransport(PROVIDER_SAMPLES["clean_single_match"]["body"])

        outcome = resolution.resolve(db_session, snapshot=capture, access=access(counting))

        assert outcome.state is DomainResolutionState.CONFIRMED
        assert outcome.selected_domain == DOMAIN
        assert counting.calls == 0, "an approved mapping answers the question already"
        assert outcome.provider_call_made is False
        assert outcome.decision.provider_call_made is False
        assert policy.REASON_REUSED_APPROVED_MAPPING in [str(r) for r in outcome.decision.reasons]

    def test_an_existing_compatible_company_is_reused_without_a_provider_call(
        self, db_session: Session, capture: LinkedInProfileSnapshot
    ) -> None:
        existing = _company(db_session, name="Meridian Works, Inc.", domain=DOMAIN)
        counting = CountingTransport(PROVIDER_SAMPLES["clean_single_match"]["body"])

        outcome = resolution.resolve(db_session, snapshot=capture, access=access(counting))

        assert outcome.state is DomainResolutionState.CONFIRMED
        assert counting.calls == 0
        assert outcome.company is not None and outcome.company.id == existing.id, (
            "the existing company is reused, not duplicated"
        )
        assert db_session.scalar(select(func.count()).select_from(Company)) == 1

    def test_a_confirmed_decision_records_its_source_on_the_candidate_store(
        self, db_session: Session, capture: LinkedInProfileSnapshot
    ) -> None:
        _company(db_session, name=COMPANY, domain=DOMAIN)
        resolution.resolve(db_session, snapshot=capture, access=NO_PROVIDER)

        record = promo.get_enrichment(db_session, capture.id)
        assert record is not None
        assert record.confirmation_status is EnrichmentConfirmationStatus.CONFIRMED
        assert record.confirmation_source is EnrichmentConfirmationSource.AUTOMATIC_POLICY

    def test_a_company_with_no_domain_does_not_confirm_anything(
        self, db_session: Session, capture: LinkedInProfileSnapshot
    ) -> None:
        """Matching a name proves nothing about which domain is right."""

        _company(db_session, name=COMPANY, domain=None)
        outcome = resolution.resolve(db_session, snapshot=capture, access=NO_PROVIDER)

        assert outcome.state is DomainResolutionState.UNRESOLVED
        assert policy.REASON_PROVIDER_LOOKUP_NOT_RUN in [str(r) for r in outcome.decision.reasons]

    def test_two_existing_companies_sharing_the_name_leave_it_unresolved(
        self, db_session: Session, capture: LinkedInProfileSnapshot
    ) -> None:
        _company(db_session, name="Meridian Works", domain=DOMAIN)
        _company(db_session, name="Meridian Works Ltd", domain="meridian.example")

        outcome = resolution.resolve(db_session, snapshot=capture, access=NO_PROVIDER)

        assert outcome.state is DomainResolutionState.UNRESOLVED
        assert outcome.selected_domain is None
        assert outcome.company is None

    def test_a_provider_failure_stays_unresolved_and_says_so(
        self, db_session: Session, capture: LinkedInProfileSnapshot
    ) -> None:
        outcome = resolution.resolve(
            db_session, snapshot=capture, access=access(transport_failing())
        )

        assert outcome.state is DomainResolutionState.UNRESOLVED
        assert outcome.selected_domain is None
        assert policy.REASON_PROVIDER_UNAVAILABLE in [str(r) for r in outcome.decision.reasons]
        assert outcome.provider_call_made is True, "the attempt is recorded truthfully"

    def test_no_provider_key_decides_from_stored_evidence_and_reports_it(
        self, db_session: Session, capture: LinkedInProfileSnapshot
    ) -> None:
        outcome = resolution.resolve(db_session, snapshot=capture, access=NO_PROVIDER)

        assert outcome.state is DomainResolutionState.UNRESOLVED
        assert outcome.provider_call_made is False
        assert policy.REASON_PROVIDER_LOOKUP_NOT_RUN in [str(r) for r in outcome.decision.reasons]

    def test_unsuitable_candidates_leave_the_capture_unresolved_with_the_evidence_kept(
        self, db_session: Session, capture: LinkedInProfileSnapshot
    ) -> None:
        outcome = resolution.resolve(
            db_session,
            snapshot=capture,
            access=access(transport_sample("platforms_and_directories")),
        )

        assert outcome.state is DomainResolutionState.UNRESOLVED
        assert outcome.selected_domain is None
        stored = {c["domain"]: c for c in outcome.decision.candidates or []}
        assert "linkedin.com" in stored
        assert stored["linkedin.com"]["rejection_reason"] == policy.REJECTED_SOCIAL_DOMAIN
        assert db_session.scalar(select(func.count()).select_from(Company)) == 0, (
            "an unresolved decision creates no company"
        )


class TestIdempotence:
    def test_a_retry_returns_the_existing_decision_and_spends_nothing(
        self, db_session: Session, capture: LinkedInProfileSnapshot
    ) -> None:
        counting = CountingTransport(PROVIDER_SAMPLES["clean_single_match"]["body"])
        first = resolution.resolve(db_session, snapshot=capture, access=access(counting))
        assert counting.calls == 1

        second = resolution.resolve(db_session, snapshot=capture, access=access(counting))

        assert second.created is False
        assert second.decision.id == first.decision.id
        assert counting.calls == 1, "a retry must not re-buy a lookup"

    def test_recalculation_over_unchanged_evidence_writes_no_second_decision(
        self, db_session: Session, capture: LinkedInProfileSnapshot
    ) -> None:
        counting = CountingTransport(PROVIDER_SAMPLES["clean_single_match"]["body"])
        first = resolution.resolve(db_session, snapshot=capture, access=access(counting))

        again = resolution.resolve(
            db_session, snapshot=capture, access=access(counting), force=True
        )

        assert again.created is False
        assert again.decision.id == first.decision.id
        assert again.decision.decided_at == first.decision.decided_at, (
            "an unchanged decision must not look newer than the evidence behind it"
        )
        assert counting.calls == 1, "the stored candidates are reused"
        assert db_session.scalar(select(func.count()).select_from(CompanyDomainResolution)) == 1

    def test_recalculation_after_the_evidence_changes_supersedes_rather_than_edits(
        self, db_session: Session, capture: LinkedInProfileSnapshot
    ) -> None:
        first = resolution.resolve(
            db_session, snapshot=capture, access=access(transport_sample("clean_single_match"))
        )
        assert first.state is DomainResolutionState.PROVISIONAL

        # An operator confirms the same company elsewhere: the evidence is now
        # established, and the same domain becomes confirmed for a better reason.
        _approved_mapping(
            db_session, key=promo.company_hints(capture).key, name=COMPANY, domain=DOMAIN
        )
        second = resolution.resolve(db_session, snapshot=capture, access=NO_PROVIDER, force=True)

        assert second.created is True
        assert second.state is DomainResolutionState.CONFIRMED
        assert second.decision.decision_number == 2

        history = store.decision_history(db_session, capture.id)
        assert [d.decision_number for d in history] == [2, 1]
        assert history[1].is_current is False
        assert history[1].superseded_at is not None
        assert history[1].state is DomainResolutionState.PROVISIONAL, (
            "the earlier decision keeps saying what it said"
        )

    def test_repeated_resolution_never_duplicates_a_company(
        self,
        db_session: Session,
        capture: LinkedInProfileSnapshot,
        second_capture: LinkedInProfileSnapshot,
    ) -> None:
        for snapshot in (capture, second_capture):
            resolution.resolve(
                db_session, snapshot=snapshot, access=access(transport_sample("clean_single_match"))
            )
        resolution.resolve(
            db_session,
            snapshot=capture,
            access=access(transport_sample("clean_single_match")),
            force=True,
        )

        assert db_session.scalar(select(func.count()).select_from(Company)) == 1

    def test_only_one_decision_can_be_current_for_a_capture(
        self, db_session: Session, capture: LinkedInProfileSnapshot
    ) -> None:
        resolution.resolve(
            db_session, snapshot=capture, access=access(transport_sample("clean_single_match"))
        )
        resolution.correct(
            db_session, snapshot=capture, domain="corrected.example", actor="operator"
        )

        current = db_session.scalars(
            select(CompanyDomainResolution).where(
                CompanyDomainResolution.capture_id == capture.id,
                CompanyDomainResolution.is_current.is_(True),
            )
        ).all()
        assert len(current) == 1


class TestPromotion:
    def test_a_provisional_capture_promotes_and_links_the_contact_by_company_id(
        self, db_session: Session, capture: LinkedInProfileSnapshot
    ) -> None:
        outcome = resolution.resolve(
            db_session, snapshot=capture, access=access(transport_sample("clean_single_match"))
        )
        assert outcome.state is DomainResolutionState.PROVISIONAL

        result = promo.promote(db_session, snapshot=capture, actor="test")

        assert result.contact_outcome is ContactPromotionOutcome.CONTACT_CREATED
        assert result.company_outcome is CompanyResolutionOutcome.DOMAIN_PROVISIONAL
        assert result.contact is not None
        assert result.contact.company_id == outcome.company.id, (
            "the permanent edge is what DAT-017A required, not the domain string alone"
        )
        assert result.contact.company_domain == DOMAIN

    def test_promotion_is_still_idempotent_and_creates_no_second_contact(
        self, db_session: Session, capture: LinkedInProfileSnapshot
    ) -> None:
        resolution.resolve(
            db_session, snapshot=capture, access=access(transport_sample("clean_single_match"))
        )
        first = promo.promote(db_session, snapshot=capture, actor="test")
        again = promo.promote(db_session, snapshot=capture, actor="test")

        assert again.contact_outcome is ContactPromotionOutcome.ALREADY_PROMOTED
        assert again.contact is not None and first.contact is not None
        assert again.contact.id == first.contact.id
        assert db_session.scalar(select(func.count()).select_from(Contact)) == 1
        assert db_session.scalar(select(func.count()).select_from(Company)) == 1

    def test_rebuilding_the_view_does_not_undo_a_provisional_resolution(
        self, db_session: Session, capture: LinkedInProfileSnapshot
    ) -> None:
        """The regression this would otherwise hit on every page load.

        A provisional decision writes no confirmation to the candidate store, so
        a view rebuild that only looked there would report the capture back as
        "awaiting your confirmation" and quietly disable the promote button.
        """

        resolution.resolve(
            db_session, snapshot=capture, access=access(transport_sample("clean_single_match"))
        )
        view = promo.build_view(db_session, capture)

        assert view.promotion.company_outcome is CompanyResolutionOutcome.DOMAIN_PROVISIONAL
        assert view.promotion.resolved_domain == DOMAIN
        assert view.can_promote is True

    def test_an_unresolved_capture_still_cannot_be_promoted(
        self, db_session: Session, capture: LinkedInProfileSnapshot
    ) -> None:
        resolution.resolve(
            db_session, snapshot=capture, access=access(transport_sample("unrelated_top_rank"))
        )
        result = promo.promote(db_session, snapshot=capture, actor="test")

        assert result.contact is None
        assert result.contact_outcome is ContactPromotionOutcome.PROMOTION_BLOCKED
        assert db_session.scalar(select(func.count()).select_from(Contact)) == 0


class TestOperatorCorrection:
    def test_a_correction_supersedes_the_earlier_decision_and_keeps_its_evidence(
        self, db_session: Session, capture: LinkedInProfileSnapshot
    ) -> None:
        first = resolution.resolve(
            db_session, snapshot=capture, access=access(transport_sample("clean_single_match"))
        )
        original_candidates = copy.deepcopy(first.decision.candidates)

        corrected = resolution.correct(
            db_session,
            snapshot=capture,
            domain="Meridian-Group.example",
            actor="operator",
            note="the provider matched the wrong Meridian",
        )

        assert corrected.state is DomainResolutionState.CONFIRMED
        assert corrected.selected_domain == "meridian-group.example", "normalized on the way in"
        assert corrected.decision.decision_kind is DomainResolutionKind.OPERATOR_CORRECTION
        assert corrected.decision.correction_note == "the provider matched the wrong Meridian"

        history = store.decision_history(db_session, capture.id)
        assert len(history) == 2, "nothing was deleted"
        superseded = history[1]
        assert superseded.state is DomainResolutionState.PROVISIONAL
        assert superseded.selected_domain == DOMAIN
        assert superseded.candidates == original_candidates
        assert superseded.superseded_at is not None
        assert superseded.is_current is False

    def test_a_correction_to_unresolved_is_recorded_as_a_decision(
        self, db_session: Session, capture: LinkedInProfileSnapshot
    ) -> None:
        resolution.resolve(
            db_session, snapshot=capture, access=access(transport_sample("clean_single_match"))
        )
        corrected = resolution.correct(
            db_session, snapshot=capture, domain=None, actor="operator", note="not this company"
        )

        assert corrected.state is DomainResolutionState.UNRESOLVED
        assert corrected.selected_domain is None
        assert policy.REASON_OPERATOR_MARKED_UNRESOLVED in [
            str(r) for r in corrected.decision.reasons
        ]

    def test_a_correction_repoints_an_already_promoted_contact_without_merging(
        self, db_session: Session, capture: LinkedInProfileSnapshot
    ) -> None:
        resolution.resolve(
            db_session, snapshot=capture, access=access(transport_sample("clean_single_match"))
        )
        result = promo.promote(db_session, snapshot=capture, actor="test")
        assert result.contact is not None
        original_company_id = result.contact.company_id

        corrected = resolution.correct(
            db_session, snapshot=capture, domain="meridian-group.example", actor="operator"
        )

        db_session.refresh(result.contact)
        assert corrected.company is not None
        assert result.contact.company_id == corrected.company.id
        assert corrected.company.id != original_company_id
        assert db_session.scalar(select(func.count()).select_from(Company)) == 2, (
            "both company rows survive — a re-link is not a merge"
        )
        assert result.contact.company_domain == DOMAIN, (
            "the captured domain is evidence and dedup input; the disagreement is "
            "surfaced as a company conflict rather than rewritten away"
        )
        assert policy.WARNING_CORRECTED_DOMAIN_DIFFERS in [
            str(w) for w in corrected.decision.warnings or []
        ]

    def test_recalculation_refuses_to_run_over_an_operator_decision(
        self, db_session: Session, capture: LinkedInProfileSnapshot
    ) -> None:
        resolution.resolve(
            db_session, snapshot=capture, access=access(transport_sample("clean_single_match"))
        )
        resolution.correct(db_session, snapshot=capture, domain="chosen.example", actor="operator")

        with pytest.raises(resolution.ResolutionError):
            resolution.resolve(db_session, snapshot=capture, access=NO_PROVIDER, force=True)

        current = store.current_decision(db_session, capture.id)
        assert current is not None
        assert current.selected_domain == "chosen.example"

    def test_an_invalid_corrected_domain_changes_nothing(
        self, db_session: Session, capture: LinkedInProfileSnapshot
    ) -> None:
        resolution.resolve(
            db_session, snapshot=capture, access=access(transport_sample("clean_single_match"))
        )
        with pytest.raises(resolution.ResolutionError):
            resolution.correct(db_session, snapshot=capture, domain="not a domain", actor="op")

        assert db_session.scalar(select(func.count()).select_from(CompanyDomainResolution)) == 1


class TestResearchReadiness:
    def test_a_provisional_company_is_research_ready_and_says_it_is_provisional(
        self, db_session: Session, capture: LinkedInProfileSnapshot
    ) -> None:
        outcome = resolution.resolve(
            db_session, snapshot=capture, access=access(transport_sample("clean_single_match"))
        )
        assert outcome.company is not None

        readiness = gates.research_readiness(
            db_session, company_id=outcome.company.id, domain=outcome.company.domain
        )
        assert readiness.ready is True
        assert readiness.is_provisional is True
        assert "provisional" in readiness.reason

    def test_a_company_with_no_resolution_record_is_not_reported_as_uncertain(
        self, db_session: Session
    ) -> None:
        company = _company(db_session, name="Imported Co", domain="imported.example")

        readiness = gates.research_readiness(
            db_session, company_id=company.id, domain=company.domain
        )
        assert readiness.ready is True
        assert readiness.state is None
        assert store.company_state(db_session, company.id) is None
