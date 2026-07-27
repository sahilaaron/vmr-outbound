"""Downstream gates for company-domain resolution (DAT-017A).

``provisional`` is only a real state if something refuses to treat it as
``confirmed``, so these are the tests that make it real. They cover the rule
twice over: once as a pure function of the state, and once through the service
that would actually act — because a rule only a route enforces is one refactor
away from not being enforced at all.

The stage that matters most here is email discovery. It is the one that spends
money and touches a mail server on the strength of the domain being right, and
it is the one issue #171 names first among the stages a provisional domain must
not open.
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path

import pytest
from app.core.config import get_settings
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_candidate import EmailCandidate
from app.models.enums import DomainResolutionState
from app.models.linkedin_profile import LinkedInProfileSnapshot
from app.services.captures import intake as capture_intake
from app.services.captures import promotion as promo
from app.services.email.candidates import generate_candidates
from app.services.resolution import gates
from app.services.resolution import service as resolution
from app.services.verification import service as verification_service
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tests.test_company_domain_resolution import (
    DOMAIN,
    PROVIDER_SAMPLES,
    access,
    transport_sample,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CAPTURE_FIXTURES = REPO_ROOT / "extensions" / "salesnav-capture" / "docs" / "fixtures"
PROFILE_SUBMISSION = json.loads(
    (CAPTURE_FIXTURES / "contact-capture.profile.example.json").read_text("utf-8")
)
LOOPBACK = "http://127.0.0.1:8000"

#: Everything a provisional domain must NOT open. Listed explicitly rather than
#: derived from the enum, so adding a stage to the enum without deciding what it
#: is allowed to do fails here instead of defaulting to permitted.
FORBIDDEN_FOR_PROVISIONAL = (
    gates.DownstreamStage.FINAL_QUALIFICATION,
    gates.DownstreamStage.PERSONALIZED_DRAFTING,
    gates.DownstreamStage.EMAIL_DISCOVERY,
    gates.DownstreamStage.CAMPAIGN_ELIGIBILITY,
    gates.DownstreamStage.SENDING,
)


def _stage(db: Session) -> LinkedInProfileSnapshot:
    payload = copy.deepcopy(PROFILE_SUBMISSION)
    payload["client_submission_id"] = str(uuid.uuid4())
    for capture in payload["contacts"]:
        capture["client_capture_id"] = str(uuid.uuid4())
    result = capture_intake.stage_contact_captures(db, payload=payload, operator_base_url=LOOPBACK)
    return db.get(  # type: ignore[return-value]
        LinkedInProfileSnapshot, uuid.UUID(str(result.results[0].capture_id))
    )


@pytest.fixture()
def provisional_contact(db_session: Session) -> Contact:
    """A promoted contact whose company domain is provisional."""

    capture = _stage(db_session)
    resolution.resolve(
        db_session, snapshot=capture, access=access(transport_sample("clean_single_match"))
    )
    result = promo.promote(db_session, snapshot=capture, actor="test")
    assert result.contact is not None
    return result.contact


@pytest.fixture()
def confirmed_contact(db_session: Session) -> Contact:
    """A promoted contact whose company domain an operator confirmed."""

    capture = _stage(db_session)
    resolution.resolve(
        db_session, snapshot=capture, access=access(transport_sample("clean_single_match"))
    )
    resolution.correct(db_session, snapshot=capture, domain=DOMAIN, actor="operator")
    result = promo.promote(db_session, snapshot=capture, actor="test")
    assert result.contact is not None
    return result.contact


def _plain_contact(db: Session) -> Contact:
    """A contact whose domain never went through automatic resolution."""

    company = Company(name="Imported Co", domain="imported.example")
    db.add(company)
    db.flush()
    contact = Contact(
        first_name="Dana",
        last_name="Reyes",
        company_name="Imported Co",
        company_domain="imported.example",
        company_id=company.id,
        natural_key="dana|reyes|imported.example",
    )
    db.add(contact)
    db.flush()
    return contact


# --- The rule, as a pure function --------------------------------------------


class TestTheRule:
    def test_a_provisional_domain_opens_research_and_nothing_else(self) -> None:
        research = gates.evaluate_state(
            DomainResolutionState.PROVISIONAL, gates.DownstreamStage.COMPANY_RESEARCH
        )
        assert research.allowed is True

        for stage in FORBIDDEN_FOR_PROVISIONAL:
            decision = gates.evaluate_state(DomainResolutionState.PROVISIONAL, stage)
            assert decision.blocked, f"{stage.value} must not proceed on a provisional domain"
            assert decision.reason and "provisional" in decision.reason

    def test_every_stage_in_the_enum_has_a_decided_answer(self) -> None:
        """No stage may be added without deciding what provisional allows it."""

        decided = {gates.DownstreamStage.COMPANY_RESEARCH, *FORBIDDEN_FOR_PROVISIONAL}
        assert set(gates.DownstreamStage) == decided

    def test_an_unresolved_domain_opens_nothing_at_all(self) -> None:
        for stage in gates.DownstreamStage:
            decision = gates.evaluate_state(DomainResolutionState.UNRESOLVED, stage)
            assert decision.blocked
            assert decision.reason and "unresolved" in decision.reason

    def test_a_confirmed_domain_opens_everything_this_gate_governs(self) -> None:
        for stage in gates.DownstreamStage:
            assert gates.evaluate_state(DomainResolutionState.CONFIRMED, stage).allowed

    def test_no_decision_at_all_is_not_a_restriction(self) -> None:
        """DAT-017A introduced provisional; it did not cast doubt on everything else.

        A domain from a spreadsheet or an operator has no decision row, and must
        behave exactly as it did before this policy existed.
        """

        for stage in gates.DownstreamStage:
            decision = gates.evaluate_state(None, stage)
            assert decision.allowed
            assert decision.state is None


# --- The rule, through the database ------------------------------------------


class TestGatingAContact:
    def test_a_provisional_contact_is_blocked_at_every_forbidden_stage(
        self, db_session: Session, provisional_contact: Contact
    ) -> None:
        assert gates.authorize_contact(
            db_session, contact=provisional_contact, stage=gates.DownstreamStage.COMPANY_RESEARCH
        ).allowed

        for stage in FORBIDDEN_FOR_PROVISIONAL:
            assert gates.authorize_contact(
                db_session, contact=provisional_contact, stage=stage
            ).blocked

    def test_require_raises_for_a_blocked_stage_and_passes_otherwise(
        self, db_session: Session, provisional_contact: Contact
    ) -> None:
        with pytest.raises(gates.DownstreamBlocked):
            gates.require(
                db_session,
                contact=provisional_contact,
                stage=gates.DownstreamStage.EMAIL_DISCOVERY,
            )
        gates.require(
            db_session,
            contact=provisional_contact,
            stage=gates.DownstreamStage.COMPANY_RESEARCH,
        )

    def test_confirming_the_domain_opens_the_stages_that_were_closed(
        self, db_session: Session, confirmed_contact: Contact
    ) -> None:
        for stage in gates.DownstreamStage:
            assert gates.authorize_contact(
                db_session, contact=confirmed_contact, stage=stage
            ).allowed

    def test_a_contact_with_no_resolution_record_is_unaffected(self, db_session: Session) -> None:
        contact = _plain_contact(db_session)
        for stage in gates.DownstreamStage:
            assert gates.authorize_contact(db_session, contact=contact, stage=stage).allowed


# --- Email discovery: the stage that spends money -----------------------------


class TestEmailDiscovery:
    def test_generation_is_refused_for_a_provisional_domain_and_writes_nothing(
        self, db_session: Session, provisional_contact: Contact
    ) -> None:
        result = generate_candidates(db_session, provisional_contact)

        assert result.needs_review is True
        assert result.selected is None
        assert result.candidates == []
        assert result.review_reason and "provisional" in result.review_reason
        assert db_session.scalar(select(func.count()).select_from(EmailCandidate)) == 0

    def test_generation_proceeds_once_the_domain_is_confirmed(
        self, db_session: Session, confirmed_contact: Contact
    ) -> None:
        result = generate_candidates(db_session, confirmed_contact)

        assert result.needs_review is False
        assert result.selected is not None
        assert result.selected.email.endswith(f"@{DOMAIN}")

    def test_verification_is_refused_before_any_provider_call_is_prepared(
        self, db_session: Session, provisional_contact: Contact
    ) -> None:
        """The gate has to bite before the queue, not after.

        ``prepare_and_enqueue_contact`` is the single door to a paid
        MillionVerifier call, and it reaches that door through candidate
        generation — so refusing generation is what stops the spend.
        """

        outcome = verification_service.prepare_and_enqueue_contact(
            db_session, provisional_contact, settings=get_settings()
        )

        assert outcome.needs_review is True
        assert outcome.review_reason and "provisional" in outcome.review_reason
        assert outcome.email is None
        assert db_session.scalar(select(func.count()).select_from(EmailCandidate)) == 0

    def test_an_ordinary_imported_contact_still_generates_candidates(
        self, db_session: Session
    ) -> None:
        """The regression that would matter most: not breaking what already worked."""

        contact = _plain_contact(db_session)
        result = generate_candidates(db_session, contact)

        assert result.needs_review is False
        assert result.selected is not None


# --- Research readiness -------------------------------------------------------


class TestResearchReadiness:
    def test_a_company_with_no_domain_is_not_research_ready(self, db_session: Session) -> None:
        company = Company(name="Nameless", domain=None)
        db_session.add(company)
        db_session.flush()

        readiness = gates.research_readiness(db_session, company_id=company.id, domain=None)
        assert readiness.ready is False
        assert "nothing to research" in readiness.reason

    def test_readiness_reports_provisional_rather_than_a_bare_ready(
        self, db_session: Session, provisional_contact: Contact
    ) -> None:
        assert provisional_contact.company_id is not None
        readiness = gates.research_readiness(
            db_session, company_id=provisional_contact.company_id, domain=DOMAIN
        )

        assert readiness.ready is True
        assert readiness.is_provisional is True
        assert "qualification" in readiness.reason, (
            "the reason must name what research readiness does NOT include"
        )


def test_the_sanitized_provider_fixture_carries_no_real_brand_or_credential() -> None:
    """The fixture is committed evidence; it must stay safe to commit."""

    lowered = (
        (Path(__file__).parent / "fixtures" / "logodev_brand_search_sanitized.json")
        .read_text("utf-8")
        .lower()
    )
    assert "authorization" not in lowered, "no auth header shape belongs in a fixture"
    assert "bearer" not in lowered

    for sample in PROVIDER_SAMPLES.values():
        if not isinstance(sample, dict) or "body" not in sample:
            continue
        for brand in sample["body"]:
            domain = str(brand.get("domain", ""))
            # Either a reserved .example domain, a deliberately invalid string,
            # or one of the real PLATFORM domains the policy must reject — which
            # are public infrastructure names, not customer data.
            assert (
                domain.endswith(".example")
                or " " in domain
                or domain
                in {
                    "linkedin.com",
                    "crunchbase.com",
                    "calderfinch.wixsite.com",
                    "hugedomains.com",
                }
            ), f"unexpected domain in a committed fixture: {domain!r}"
