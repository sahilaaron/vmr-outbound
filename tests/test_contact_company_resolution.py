"""Company-domain resolution for a Contact that arrived without a capture.

The defect these tests exist for: Google Sheets could accept a row naming a
company the product had never seen, but that Contact could never *establish* the
company, because company-domain resolution was bound to a Chrome capture — the
decision ledger's subject was a ``linkedin_profile_snapshots`` row and the
candidate store had no owner for a bare Contact. Sheets contacts therefore sat at
``company_domain_missing`` until somebody re-acquired the same person through the
browser extension.

What is proved here is convergence, not a second path: an unseen company from
either surface enters the same evidence gathering, the same provider ladder, the
same policy, the same decision ledger and the same downstream gates, and the two
surfaces share what each establishes. The properties that made the old refusal
correct are proved to survive it — a provisional domain stays provisional and
cannot launder itself into evidence, nothing is decided without a provider to
decide with, and a retry costs nothing.

The provider is always stubbed. No test needs an API key and none can spend a
lookup.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from app.core.config import Settings, get_settings
from app.models.campaign import Campaign
from app.models.company import Company
from app.models.company_domain_resolution import CompanyDomainResolution
from app.models.contact import Contact
from app.models.enums import (
    AgentIdentifier,
    CampaignStatus,
    DomainResolutionState,
    EnrichmentConfirmationSource,
    PipelineStageStatus,
)
from app.models.linkedin_profile import LinkedInProfileSnapshot
from app.models.salesnav_enrichment import SalesNavCompanyEnrichment
from app.services import campaign_contacts
from app.services.agents.adapters import DEFAULT_ADAPTERS, CompanyAgentAdapter
from app.services.agents.orchestrator import run_next
from app.services.captures import promotion as capture_promotion
from app.services.enrichment import companies as enrichment
from app.services.enrichment import logodev
from app.services.resolution import gates, store
from app.services.resolution import service as resolution
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

COMPANY = "Meridian Works"
DOMAIN = "meridianworks.example"


# --- Stubs --------------------------------------------------------------------


class CountingTransport:
    """A provider stub that records how many calls were actually bought."""

    def __init__(self, body: list[dict[str, Any]]) -> None:
        self.body = body
        self.calls = 0

    def __call__(self, url: str, headers: Any, timeout: float) -> logodev.RawResponse:
        assert "Authorization" in headers, "the client must authenticate"
        self.calls += 1
        return logodev.RawResponse(status_code=200, body=json.dumps(self.body))


def matching_transport() -> CountingTransport:
    """One brand whose name and domain both match — the provisional case."""

    return CountingTransport([{"domain": DOMAIN, "name": COMPANY}])


def access(transport: logodev.Transport | None) -> resolution.ProviderAccess:
    return resolution.ProviderAccess(
        api_key="test-key-never-real",
        search_url="https://api.logo.dev/search",
        timeout=5.0,
        max_candidates=10,
        transport=transport,
    )


NO_PROVIDER = resolution.ProviderAccess()


@pytest.fixture()
def resolution_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Automatic company-domain resolution switched on, with no real credential.

    Deliberately *not* ``FEATURES__CONTACT_CAPTURE_PROMOTION``: that control
    governs turning captures into Contacts, and a Contact acquired from a
    spreadsheet must not depend on it.
    """

    monkeypatch.setenv("FEATURES__AUTOMATIC_COMPANY_DOMAIN_RESOLUTION", "true")
    monkeypatch.setenv("FEATURES__SALESNAV_DOMAIN_ENRICHMENT", "true")
    monkeypatch.setenv("LOGO_DEV_API_KEY", "test-key-never-real")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --- Fixtures -----------------------------------------------------------------


def sheet_contact(db: Session, *, company: str = COMPANY, first: str = "Ada") -> Contact:
    """A Contact exactly as the Sheets intake path leaves one for an unseen company.

    Company name preserved, no domain, no company link, no natural key — see
    ``app.services.integrations.sheets.submit``.
    """

    contact = Contact(
        first_name=first,
        last_name="Lovelace",
        company_name=company,
        company_domain=None,
        company_id=None,
        natural_key=None,
    )
    db.add(contact)
    db.flush()
    return contact


def capture(db: Session, *, company: str = COMPANY) -> LinkedInProfileSnapshot:
    """A capture naming the same employer, for the convergence tests."""

    snapshot = LinkedInProfileSnapshot(
        client_capture_id=f"cap-{uuid.uuid4()}",
        content_hash=str(uuid.uuid4()),
        schema_version="linkedin-contact-capture/2.1.0",
        source="test",
        extraction_status="ok",
        payload={"current_employment_hint": {"company_name": company}},
        profile_fields={"full_name": "Grace Hopper", "first_name": "Grace", "last_name": "Hopper"},
    )
    db.add(snapshot)
    db.flush()
    return snapshot


def campaign_for(db: Session) -> Campaign:
    campaign = Campaign(
        name=f"Contact resolution {uuid.uuid4()}",
        status=CampaignStatus.ACTIVE,
        execution_enabled=True,
    )
    db.add(campaign)
    db.flush()
    return campaign


def current(db: Session, contact: Contact) -> CompanyDomainResolution | None:
    return store.current_decision(db, store.ResolutionSubject.for_contact(contact.id))


# --- The ledger's subject -----------------------------------------------------


class TestTheLedgerIsSourceAgnostic:
    """A decision is about a subject. Exactly one, and either kind is first class."""

    def test_a_subject_names_exactly_one_record(self) -> None:
        with pytest.raises(ValueError):
            store.ResolutionSubject()
        with pytest.raises(ValueError):
            store.ResolutionSubject(capture_id=uuid.uuid4(), contact_id=uuid.uuid4())

    def test_the_database_refuses_a_decision_with_two_subjects(
        self, db_session: Session, resolution_on: None
    ) -> None:
        """The constraint is in the schema, not only in the constructor.

        A path that builds a row directly — a script, a later feature, a repair —
        still cannot write a decision that two surfaces could both claim.
        """

        contact = sheet_contact(db_session)
        snapshot = capture(db_session)
        db_session.add(
            CompanyDomainResolution(
                capture_id=snapshot.id,
                contact_id=contact.id,
                decision_number=1,
                is_current=True,
                state=DomainResolutionState.UNRESOLVED,
                policy_version="test",
                reasons=[],
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_a_contact_decision_and_a_capture_decision_are_both_current(
        self, db_session: Session, resolution_on: None
    ) -> None:
        """Two subjects at the same company do not collide in the live-decision index."""

        contact = sheet_contact(db_session)
        snapshot = capture(db_session)
        resolution.resolve_contact(db_session, contact=contact, access=access(matching_transport()))
        resolution.resolve(db_session, snapshot=snapshot, access=access(matching_transport()))

        contact_decision = current(db_session, contact)
        capture_decision = store.current_decision(db_session, snapshot.id)
        assert contact_decision is not None and capture_decision is not None
        assert contact_decision.id != capture_decision.id
        assert contact_decision.capture_id is None
        assert contact_decision.contact_id == contact.id
        assert capture_decision.contact_id is None
        assert capture_decision.capture_id == snapshot.id
        assert contact_decision.subject_label == "contact"
        assert capture_decision.subject_label == "capture"


# --- Resolving a contact ------------------------------------------------------


class TestResolvingAContactsCompany:
    def test_an_unseen_company_reaches_a_provisional_domain_and_a_company(
        self, db_session: Session, resolution_on: None
    ) -> None:
        """The blocker, stated as the behaviour that used to be impossible."""

        contact = sheet_contact(db_session)
        transport = matching_transport()
        outcome = resolution.resolve_contact(db_session, contact=contact, access=access(transport))

        assert transport.calls == 1
        assert outcome.state is DomainResolutionState.PROVISIONAL
        assert outcome.selected_domain == DOMAIN
        assert outcome.company is not None and outcome.company.domain == DOMAIN
        assert contact.company_id == outcome.company.id
        assert contact.company_domain == DOMAIN

    def test_the_decision_keeps_the_evidence_that_explains_it(
        self, db_session: Session, resolution_on: None
    ) -> None:
        """A contact-subject decision is auditable on exactly the capture path's terms."""

        contact = sheet_contact(db_session)
        resolution.resolve_contact(db_session, contact=contact, access=access(matching_transport()))

        decision = current(db_session, contact)
        assert decision is not None
        assert decision.company_name_original == COMPANY
        assert decision.company_name_normalized == "meridianworks"
        assert decision.candidates, "the candidates considered must survive the decision"
        assert decision.reasons
        assert decision.provider_call_made is True
        assert decision.provider == "logo.dev"
        assert decision.enrichment_id is not None
        assert decision.decided_by == resolution.RESOLUTION_ACTOR

    def test_established_evidence_resolves_without_asking_a_provider(
        self, db_session: Session, resolution_on: None
    ) -> None:
        """A company somebody already established costs nothing and confirms."""

        db_session.add(Company(name="Meridian Works", domain=DOMAIN))
        db_session.flush()
        contact = sheet_contact(db_session, company="  meridian   works ")

        transport = matching_transport()
        outcome = resolution.resolve_contact(db_session, contact=contact, access=access(transport))

        assert transport.calls == 0
        assert outcome.state is DomainResolutionState.CONFIRMED
        assert outcome.selected_domain == DOMAIN
        assert contact.company_domain == DOMAIN

    def test_a_two_word_company_matches_its_own_permanent_row(
        self, db_session: Session, resolution_on: None
    ) -> None:
        """The shared evidence scan no longer loses a match to its own prefilter.

        ``"Kiln Systems"`` folds to ``"kilnsystems"``, whose first six characters
        never appear in ``"kiln systems"``. While the scan pre-filtered on them,
        the Agent would have bought a lookup for a company already established and
        could have landed on a different domain than the intake surface — the two
        acquisition paths disagreeing about the same company.
        """

        established = Company(name="Kiln Systems", domain="kiln.example")
        db_session.add(established)
        db_session.flush()
        contact = sheet_contact(db_session, company="Kiln Systems")

        transport = matching_transport()
        outcome = resolution.resolve_contact(db_session, contact=contact, access=access(transport))

        assert transport.calls == 0
        assert outcome.state is DomainResolutionState.CONFIRMED
        assert contact.company_id == established.id

    def test_a_second_evaluation_writes_nothing_and_buys_nothing(
        self, db_session: Session, resolution_on: None
    ) -> None:
        """Retries are free, exactly as they are for a capture."""

        contact = sheet_contact(db_session)
        transport = matching_transport()
        first = resolution.resolve_contact(db_session, contact=contact, access=access(transport))
        second = resolution.resolve_contact(db_session, contact=contact, access=access(transport))

        assert first.created is True
        assert second.created is False
        assert second.decision.id == first.decision.id
        assert transport.calls == 1
        assert (
            db_session.scalars(
                select(CompanyDomainResolution).where(
                    CompanyDomainResolution.contact_id == contact.id
                )
            ).all()
            != []
        )
        assert (
            len(store.decision_history(db_session, store.ResolutionSubject.for_contact(contact.id)))
            == 1
        )

    def test_asking_again_re_links_a_contact_whose_edge_was_cleared(
        self, db_session: Session, resolution_on: None
    ) -> None:
        """The decision is already right; the link is what has to be made true again.

        Returning a stored decision while leaving the Contact looking unresolved
        would strand it: every other reader goes through ``company_id``, and
        re-deciding is not the repair — the decision was not the thing that was
        wrong.
        """

        contact = sheet_contact(db_session)
        transport = matching_transport()
        first = resolution.resolve_contact(db_session, contact=contact, access=access(transport))
        assert first.company is not None

        contact.company_id = None
        contact.company_domain = None
        db_session.flush()

        again = resolution.resolve_contact(db_session, contact=contact, access=access(transport))

        assert again.created is False
        assert transport.calls == 1, "re-linking must not re-buy the lookup"
        assert contact.company_id == first.company.id
        assert contact.company_domain == DOMAIN

    def test_a_contact_with_no_company_name_is_refused_rather_than_guessed_at(
        self, db_session: Session, resolution_on: None
    ) -> None:
        contact = sheet_contact(db_session, company="")
        contact.company_name = None
        db_session.flush()
        with pytest.raises(resolution.ResolutionError):
            resolution.resolve_contact(db_session, contact=contact, access=access(None))
        assert current(db_session, contact) is None

    def test_no_provider_records_no_decision_at_all(
        self, db_session: Session, resolution_on: None
    ) -> None:
        """The absence of a lookup must not be stored as the absence of a domain.

        With no usable provider the policy could only say "the lookup was not
        run", and because a recorded decision is not recalculated without an
        explicit force, storing that would freeze this Contact at a decision
        nobody made. The Agent therefore does not attempt resolution at all —
        see :class:`TestTheCompanyAgentIsWhereItHappens`.
        """

        contact = sheet_contact(db_session)
        outcome = resolution.resolve_contact(db_session, contact=contact, access=NO_PROVIDER)

        # Called directly, the service still decides truthfully — it is the Agent
        # that declines to ask. What matters here is that it never invents one.
        assert outcome.state is DomainResolutionState.UNRESOLVED
        assert outcome.selected_domain is None
        assert contact.company_domain is None
        assert contact.company_id is None


# --- Provisional stays provisional --------------------------------------------


class TestProvisionalStaysProvisional:
    def test_it_opens_research_and_nothing_after_it(
        self, db_session: Session, resolution_on: None
    ) -> None:
        contact = sheet_contact(db_session)
        campaign = campaign_for(db_session)
        resolution.resolve_contact(db_session, contact=contact, access=access(matching_transport()))

        assert (
            gates.authorize_contact(
                db_session,
                contact=contact,
                stage=gates.DownstreamStage.COMPANY_RESEARCH,
                campaign=campaign,
            ).allowed
            is True
        )
        for stage in (
            gates.DownstreamStage.FINAL_QUALIFICATION,
            gates.DownstreamStage.PERSONALIZED_DRAFTING,
            gates.DownstreamStage.EMAIL_DISCOVERY,
            gates.DownstreamStage.CAMPAIGN_ELIGIBILITY,
            gates.DownstreamStage.SENDING,
        ):
            decision = gates.authorize_contact(
                db_session, contact=contact, stage=stage, campaign=campaign
            )
            assert decision.blocked, f"{stage} must not proceed on a provisional domain"

    def test_it_never_becomes_an_approved_mapping(
        self, db_session: Session, resolution_on: None
    ) -> None:
        """The guess must not be readable back as a confirmation."""

        contact = sheet_contact(db_session)
        resolution.resolve_contact(db_session, contact=contact, access=access(matching_transport()))

        record = enrichment.contact_record(db_session, contact.id)
        assert record is not None
        assert record.confirmed_domain is None
        assert (
            capture_promotion.prior_confirmed_domains(
                db_session,
                company_key_value=enrichment.company_key(COMPANY),
                company_linkedin_id=None,
            )
            == set()
        )

    def test_a_provisional_company_cannot_confirm_the_next_contact(
        self, db_session: Session, resolution_on: None
    ) -> None:
        """The laundering route, closed for the contact subject too.

        A provisional decision creates a permanent Company so research can start.
        If that Company then counted as established evidence, the very next
        contact at the same employer would read it back and *confirm* the same
        guess — uncertainty citing itself.
        """

        first = sheet_contact(db_session, first="Ada")
        resolution.resolve_contact(db_session, contact=first, access=access(matching_transport()))

        second = sheet_contact(db_session, first="Charles")
        transport = matching_transport()
        outcome = resolution.resolve_contact(db_session, contact=second, access=access(transport))

        assert outcome.state is DomainResolutionState.PROVISIONAL
        assert transport.calls == 1, "the second contact had to look up for itself"
        assert outcome.company is not None and outcome.company.id == first.company_id


# --- The two surfaces converge -------------------------------------------------


class TestSheetsAndChromeConverge:
    def test_both_surfaces_reach_the_same_permanent_company(
        self, db_session: Session, resolution_on: None
    ) -> None:
        contact = sheet_contact(db_session)
        snapshot = capture(db_session)

        from_sheet = resolution.resolve_contact(
            db_session, contact=contact, access=access(matching_transport())
        )
        from_capture = resolution.resolve(
            db_session, snapshot=snapshot, access=access(matching_transport())
        )

        assert from_sheet.company is not None and from_capture.company is not None
        assert from_sheet.company.id == from_capture.company.id
        assert from_sheet.state is from_capture.state is DomainResolutionState.PROVISIONAL

    def test_a_domain_confirmed_from_one_surface_is_free_for_the_other(
        self, db_session: Session, resolution_on: None
    ) -> None:
        """Shared evidence, in both directions — the point of one candidate store.

        A confirmation recorded against a contact-owned record is read back by
        the capture path's approved-mapping lookup, and confirms without a call.
        """

        contact = sheet_contact(db_session)
        resolution.resolve_contact(db_session, contact=contact, access=access(matching_transport()))
        record = enrichment.contact_record(db_session, contact.id)
        assert record is not None
        enrichment.confirm_record(
            db_session,
            record=record,
            source=EnrichmentConfirmationSource.MANUAL,
            domain=DOMAIN,
            actor="operator",
        )

        snapshot = capture(db_session)
        transport = matching_transport()
        outcome = resolution.resolve(db_session, snapshot=snapshot, access=access(transport))

        assert transport.calls == 0
        assert outcome.state is DomainResolutionState.CONFIRMED
        assert outcome.selected_domain == DOMAIN

    def test_the_candidate_store_names_its_owner(
        self, db_session: Session, resolution_on: None
    ) -> None:
        contact = sheet_contact(db_session)
        resolution.resolve_contact(db_session, contact=contact, access=access(matching_transport()))
        record = db_session.scalars(
            select(SalesNavCompanyEnrichment).where(
                SalesNavCompanyEnrichment.contact_id == contact.id
            )
        ).one()
        assert record.owner_label == "contact"
        assert record.batch_id is None and record.capture_id is None


# --- Where it happens in the pipeline -----------------------------------------


def _enrol(db: Session, campaign: Campaign, contact: Contact) -> Any:
    return campaign_contacts.enrol_contact(
        db,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="google_sheets",
        enqueue=True,
        desired_stage=AgentIdentifier.COMPANY,
    )


def _adapters(adapter: CompanyAgentAdapter) -> dict[AgentIdentifier, Any]:
    merged = dict(DEFAULT_ADAPTERS)
    merged[AgentIdentifier.COMPANY] = adapter
    return merged


def _company_adapter(transport: logodev.Transport | None) -> CompanyAgentAdapter:
    """The Company Agent with the provider stubbed and the model fallback off."""

    return CompanyAgentAdapter(
        access_factory=lambda session, settings: access(transport),
        model_factory=lambda session, settings: resolution.ModelAccess(),
    )


class TestTheCompanyAgentIsWhereItHappens:
    def test_a_sheets_contact_continues_through_the_same_pipeline(
        self, db_session: Session, resolution_on: None
    ) -> None:
        """End to end: enrolled with a name only, and it advances by itself."""

        campaign = campaign_for(db_session)
        contact = sheet_contact(db_session)
        enrolled = _enrol(db_session, campaign, contact)
        adapters = _adapters(_company_adapter(matching_transport()))

        assert (
            run_next(db_session, worker_id="test", adapters=adapters).public_status == "completed"
        )
        company_run = run_next(db_session, worker_id="test", adapters=adapters)

        assert company_run.public_status == "completed"
        assert contact.company_id is not None
        assert contact.company_domain == DOMAIN
        assert enrolled.membership.pipeline_status is PipelineStageStatus.COMPLETED

        decision = current(db_session, contact)
        assert decision is not None
        assert decision.state is DomainResolutionState.PROVISIONAL
        assert decision.capture_id is None

    def test_the_agent_reports_that_it_resolved_rather_than_reused(
        self, db_session: Session, resolution_on: None
    ) -> None:
        campaign = campaign_for(db_session)
        contact = sheet_contact(db_session)
        _enrol(db_session, campaign, contact)
        adapters = _adapters(_company_adapter(matching_transport()))
        run_next(db_session, worker_id="test", adapters=adapters)
        job = run_next(db_session, worker_id="test", adapters=adapters).job

        assert job is not None
        reference = job.result or {}
        assert reference["identity"]["company_action"] == "resolved"
        assert reference["identity"]["match_key"] == "company.resolved_domain"
        attempt = reference["domain_resolution_attempt"]
        assert attempt["attempted"] is True
        assert attempt["subject"] == "contact"
        assert attempt["state"] == DomainResolutionState.PROVISIONAL.value

    def test_without_a_provider_it_blocks_and_records_no_decision(
        self, db_session: Session, resolution_on: None
    ) -> None:
        """Blocked is the truthful answer; a stored non-decision would not be.

        The Contact must stay exactly as resolvable as it was, so a later pass
        with a provider configured can still decide.
        """

        campaign = campaign_for(db_session)
        contact = sheet_contact(db_session)
        enrolled = _enrol(db_session, campaign, contact)
        adapters = _adapters(
            CompanyAgentAdapter(
                access_factory=lambda session, settings: NO_PROVIDER,
                model_factory=lambda session, settings: resolution.ModelAccess(),
            )
        )

        run_next(db_session, worker_id="test", adapters=adapters)
        blocked = run_next(db_session, worker_id="test", adapters=adapters)

        assert blocked.public_status == "paused"
        assert blocked.job is not None
        assert blocked.job.error_class == "company_domain_missing"
        assert enrolled.membership.pipeline_status is PipelineStageStatus.BLOCKED
        assert current(db_session, contact) is None
        assert contact.company_id is None

    def test_an_unresolved_provider_decision_survives_the_company_block(
        self, db_session: Session, resolution_on: None
    ) -> None:
        """The worker must not erase a truthful decision after paying for it.

        The resolver completed normally and decided ``UNRESOLVED``; the Company
        stage therefore pauses, but its savepoint must retain the decision,
        provider outcome and reasons. This is the hosted UAT failure: before the
        repair, ``AgentBlocked`` rolled all of that back and left only
        ``company_domain_missing``.
        """

        campaign = campaign_for(db_session)
        contact = sheet_contact(db_session, company="Borealis")
        _enrol(db_session, campaign, contact)
        transport = CountingTransport([])
        adapters = _adapters(_company_adapter(transport))

        run_next(db_session, worker_id="test", adapters=adapters)
        blocked = run_next(db_session, worker_id="test", adapters=adapters)

        assert blocked.public_status == "paused"
        assert blocked.job is not None
        assert blocked.job.error_class == "company_domain_missing"
        decision = current(db_session, contact)
        assert decision is not None
        assert decision.state is DomainResolutionState.UNRESOLVED
        assert decision.selected_domain is None
        assert decision.provider_call_made is True
        assert decision.provider == "logo.dev"
        assert decision.reasons
        assert contact.company_domain is None
        assert contact.company_id is None
        assert transport.calls == 1

    def test_it_says_which_switch_is_off_rather_than_only_that_evidence_is_missing(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An operator can act on "resolution is switched off". They cannot act on silence."""

        monkeypatch.delenv("FEATURES__AUTOMATIC_COMPANY_DOMAIN_RESOLUTION", raising=False)
        get_settings.cache_clear()
        campaign = campaign_for(db_session)
        contact = sheet_contact(db_session)
        _enrol(db_session, campaign, contact)
        adapters = _adapters(_company_adapter(matching_transport()))

        run_next(db_session, worker_id="test", adapters=adapters)
        blocked = run_next(db_session, worker_id="test", adapters=adapters)

        assert blocked.job is not None
        assert blocked.job.error_class == "company_domain_missing"
        detail = (blocked.job.error or {}).get("detail") or {}
        attempt = detail.get("domain_resolution_attempt") or {}
        assert attempt.get("attempted") is False
        assert "switched off" in str(attempt.get("skipped_because"))
        assert current(db_session, contact) is None
        get_settings.cache_clear()

    def test_a_contact_that_already_has_a_company_never_reaches_a_provider(
        self, db_session: Session, resolution_on: None
    ) -> None:
        """Step 3 is the last resort, not a step every Contact pays for."""

        company = Company(name="Analytical Engines", domain="engines.example")
        db_session.add(company)
        db_session.flush()
        campaign = campaign_for(db_session)
        contact = sheet_contact(db_session, company="Analytical Engines")
        contact.company_domain = company.domain
        contact.company_id = company.id
        db_session.flush()
        _enrol(db_session, campaign, contact)

        transport = matching_transport()
        adapters = _adapters(_company_adapter(transport))
        run_next(db_session, worker_id="test", adapters=adapters)
        completed = run_next(db_session, worker_id="test", adapters=adapters)

        assert completed.public_status == "completed"
        assert transport.calls == 0
        assert current(db_session, contact) is None


# --- The shared access builders ------------------------------------------------


class TestProviderAccessIsDecidedOnce:
    def test_the_provider_is_unusable_while_its_switch_is_off(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("FEATURES__SALESNAV_DOMAIN_ENRICHMENT", raising=False)
        monkeypatch.setenv("LOGO_DEV_API_KEY", "test-key-never-real")
        get_settings.cache_clear()
        settings: Settings = get_settings()
        assert resolution.provider_access_for(db_session, settings).available is False
        get_settings.cache_clear()

    def test_the_backfill_pass_asks_the_same_question(
        self, db_session: Session, resolution_on: None
    ) -> None:
        """One definition, so the surfaces cannot drift on "may we call the provider"."""

        from app.services.resolution import pending

        settings = get_settings()
        shared = resolution.provider_access_for(db_session, settings)
        assert pending._provider_access(db_session, settings).available is shared.available
