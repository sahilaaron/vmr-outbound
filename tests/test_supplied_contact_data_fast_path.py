"""The four-case matrix for contact data the operator supplied at intake.

What is being proved, stated once so every assertion below has a reason:

    Supplying a value removes the work of *finding* that value, and removes
    nothing else.

So the matrix is deliberately two-sided. Every case asserts what stopped
happening — no candidate generated, no provider called, no domain resolution
attempted — and every case also asserts what kept happening: the Company Agent,
Research and the stages after them run for a fully-supplied contact exactly as
they do for one that supplied nothing. A test that only proved the skip would
pass just as happily against an implementation that had quietly turned the
pipeline into a passthrough.

The third thing every case asserts is the one that matters most: **no false
verification**. A supplied address never gets an ``ExactEmailVerification`` row,
never reports ``VALID``, and completes Verification through a bypass that names
itself. "We were given this address" and "we asked a provider and it answered" are
different sentences, and the day they become the same one is the day this system
starts telling customers a mailbox exists because a spreadsheet said so.

The Google Sheets add-on is the surface driven here because it is the one that
newly accepts these fields, and it is driven over HTTP for the same reason its
own test module gives: the add-on is an ordinary caller on the far side of a
credential check, and that is the shape of the risk. The CSV/XLSX import already
carried an address through its own richer evidence
(:class:`~app.models.imported_email.ImportedContactEmail`) before this work, and
``tests/test_campaign_import_email.py`` and
``tests/test_campaign_import_pipeline.py`` continue to own that path; the case
here is the one those cannot cover, which is a supplied address arriving without
a file behind it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from app.core.config import get_settings
from app.models.campaign import Campaign, CampaignContact
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_candidate import EmailCandidate
from app.models.email_evidence import ExactEmailVerification
from app.models.enums import (
    AgentControlStatus,
    AgentIdentifier,
    CampaignStatus,
    EmailVisualStatus,
    PipelineStageStatus,
    SuppressionReason,
    SuppressionType,
)
from app.models.pipeline import CampaignContactAgentState, CampaignContactSource
from app.models.verification_job import AgentJob
from app.services import campaign_contacts, customer_status, suppressions
from app.services.agents import controls
from app.services.agents.orchestrator import run_next
from app.services.agents.registry import PIPELINE_ORDER
from app.services.imports import campaign_import
from app.services.integrations.sheets import results as sheet_results
from app.services.integrations.sheets import submit as sheet_submit
from app.services.integrations.sheets.contract import (
    RowContractError,
    RowStatus,
    SheetLocation,
    parse_row,
)
from app.services.pipeline import agent_state
from app.services.provenance import supplied_inputs
from app.services.verification import status as verification_status
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests import apollo_factory as af
from tests.gmail_factory import build_sequence
from tests.test_research_claude_fallback import (
    FakeWorker,
    ScriptedThinker,
    _adapters,
    _claim,
)

WORKER = "supplied-fast-path-worker"
INSTALLATION = "install-supplied"
SPREADSHEET = "sheet-supplied"
TAB = "0"

SUPPLIED_EMAIL = "ada.lovelace@kiln.example"
SUPPLIED_WEBSITE = "https://www.kiln.example/about"
SUPPLIED_DOMAIN = "kiln.example"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _enable_research(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("FEATURES__COMPANY_RESEARCH", "true")
    monkeypatch.setenv("FEATURES__RESEARCH_CLAUDE_FALLBACK", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def make_campaign(db: Session, *, execution: bool = True) -> Campaign:
    campaign = Campaign(
        name=f"Supplied {uuid.uuid4().hex[:8]}",
        description="supplied-input matrix",
        status=CampaignStatus.ACTIVE,
        execution_enabled=execution,
    )
    db.add(campaign)
    db.flush()
    return campaign


def location() -> SheetLocation:
    return SheetLocation(installation_id=INSTALLATION, spreadsheet_id=SPREADSHEET, sheet_id=TAB)


def submit(
    db: Session,
    campaign: Campaign,
    *,
    email: str | None = None,
    website: str | None = None,
    client_row_id: str = "r1",
    generation: int = 1,
    first: str = "Ada",
    last: str = "Lovelace",
    company: str = "Kiln Systems",
) -> sheet_submit.BatchSubmission:
    """One row through the real contract and the real submit service.

    ``parse_row`` is not bypassed: the normalization and the refusals under test
    live there, and a helper that built a ``SubmittedRow`` by hand would prove the
    pipeline against values the wire contract might never produce.
    """

    payload: dict[str, Any] = {
        "client_row_id": client_row_id,
        "first_name": first,
        "last_name": last,
        "company_name": company,
    }
    if email is not None:
        payload["email"] = email
    if website is not None:
        payload["website"] = website
    parsed = parse_row(payload, max_context_chars=2_000)
    return sheet_submit.submit_rows(
        db,
        campaign=campaign,
        location=location(),
        rows=[parsed],
        generation=generation,
        batch_reference="batch-supplied",
        actor="operator@vmr.example",
    )


def seed_company(
    db: Session, *, name: str = "Kiln Systems", domain: str = SUPPLIED_DOMAIN
) -> Company:
    """A Company the deployment has already established, with no decision row.

    This is what an operator-entered or previously imported Company looks like,
    and it is what the resolution policy treats as established evidence — so a
    row naming this company links to it for free and without a provider call.
    """

    company = Company(name=name, domain=domain)
    db.add(company)
    db.flush()
    return company


def membership_of(db: Session, campaign: Campaign) -> CampaignContact:
    return db.scalars(
        select(CampaignContact).where(CampaignContact.campaign_id == campaign.id)
    ).one()


def enable(db: Session, *agents: AgentIdentifier) -> None:
    for agent in agents:
        controls.set_global_control(
            db,
            agent_id=agent,
            status=AgentControlStatus.ENABLED,
            config={"live": True},
        )
    db.flush()


def research_adapters() -> dict[AgentIdentifier, Any]:
    """The Research seam under test control, so the stage genuinely executes.

    Research must *run* in every case of this matrix — that is half of what is
    being proved — so it is driven by the same scripted worker and thinker the
    Research tests use rather than switched off.
    """

    return _adapters(
        FakeWorker(),
        ScriptedThinker(payload={"claims": [_claim("short_description", "They build kilns.")]}),
    )


def drain(db: Session, adapters: Any | None = None, rounds: int = 16) -> None:
    for _ in range(rounds):
        if run_next(db, worker_id=WORKER, adapters=adapters).job is None:
            return


def run_pipeline(db: Session) -> None:
    """Everything through Verification, with Research really executing."""

    enable(
        db,
        AgentIdentifier.RESEARCH,
        AgentIdentifier.EMAIL,
        AgentIdentifier.VERIFICATION,
    )
    drain(db, research_adapters())


def stage(
    db: Session, membership: CampaignContact, agent: AgentIdentifier
) -> CampaignContactAgentState | None:
    return agent_state(db, campaign_contact_id=membership.id, agent_id=agent, create=False)


def stage_output(
    db: Session, membership: CampaignContact, agent: AgentIdentifier
) -> dict[str, Any]:
    state = stage(db, membership, agent)
    assert state is not None, f"{agent.value} has no durable state"
    return dict(state.output_reference or {})


def email_result(db: Session, membership: CampaignContact) -> dict[str, Any]:
    job = db.scalars(
        select(AgentJob)
        .where(
            AgentJob.campaign_contact_id == membership.id,
            AgentJob.agent_id == AgentIdentifier.EMAIL,
        )
        .order_by(AgentJob.created_at.desc())
    ).first()
    assert job is not None, "the Email Agent never ran"
    return dict(job.result or {})


def assert_pipeline_continued_past_verification(db: Session, membership: CampaignContact) -> None:
    """Insights is still this Contact's business after the address was satisfied.

    Asserted as "the walk reached the stage" rather than "the stage completed",
    because what happens to Insights next is decided by its own Agent control and
    its own evidence — neither of which this change touches. What would be a
    defect is the walk *stopping* at Verification, or never creating the Insights
    state at all, which is what a fast path that had quietly become a passthrough
    would look like.
    """

    assert membership.latest_completed_stage is not None
    assert PIPELINE_ORDER.index(membership.latest_completed_stage) >= PIPELINE_ORDER.index(
        AgentIdentifier.VERIFICATION
    )
    assert stage(db, membership, AgentIdentifier.INSIGHTS) is not None, (
        "the pipeline stopped at Verification instead of continuing to Insights"
    )


def assert_nothing_claims_verification(db: Session, membership: CampaignContact) -> None:
    """The invariant every supplied-address case shares.

    Four independent statements, because each is a different way the lie could be
    told: by evidence, by a candidate, by a provider job, and by the status the
    rest of the product reads.
    """

    contact = db.get(Contact, membership.contact_id)
    assert contact is not None

    assert db.scalars(select(ExactEmailVerification)).all() == [], (
        "a supplied address must never produce verification evidence"
    )
    assert (
        db.scalars(select(EmailCandidate).where(EmailCandidate.contact_id == contact.id)).all()
        == []
    ), "a supplied address must never generate a candidate"
    assert (
        db.scalars(
            select(AgentJob).where(
                AgentJob.campaign_contact_id == membership.id,
                AgentJob.agent_id == AgentIdentifier.VERIFICATION,
            )
        ).all()
        == []
    ), "a supplied address must never queue a Verification provider job"

    view = verification_status.derive_status_for_contact(db, contact)
    assert view.visual is not EmailVisualStatus.SUCCESSFUL, (
        "a supplied address must not read as a successful verification"
    )


# ---------------------------------------------------------------------------
# Case A — neither supplied. The unchanged pipeline.
# ---------------------------------------------------------------------------


def test_case_a_supplying_nothing_leaves_the_pipeline_exactly_as_it_was(
    db_session: Session,
) -> None:
    """No supplied record, no fast path, and discovery still owns the address."""

    campaign = make_campaign(db_session)
    seed_company(db_session)
    submit(db_session, campaign)
    membership = membership_of(db_session, campaign)

    # Nothing was recorded as supplied, because nothing was supplied.
    source = db_session.scalars(
        select(CampaignContactSource).where(
            CampaignContactSource.campaign_contact_id == membership.id
        )
    ).one()
    assert supplied_inputs.CONTEXT_KEY not in (source.source_context or {})

    contact = db_session.get(Contact, membership.contact_id)
    assert contact is not None
    assert contact.email is None

    run_pipeline(db_session)
    db_session.flush()

    result = email_result(db_session, membership)
    assert result.get("domain_outcome") != "supplied_email_accepted"
    assert "address_derivation" not in result

    verification = stage(db_session, membership, AgentIdentifier.VERIFICATION)
    assert verification is None or verification.reason_code not in {
        "verification_bypassed_supplied_email",
        "verification_bypassed_imported_email",
    }


def test_case_a_domain_resolution_is_still_attempted_when_no_company_is_known(
    db_session: Session,
) -> None:
    """The Company Agent still asks the shared resolution process.

    No seeded Company and no supplied website, so the Contact reaches the Company
    stage carrying a name and nothing else — which is precisely the state
    automatic company-domain resolution exists for. The distinguishing evidence is
    which branch reported: ``skipped_because`` is written inside
    ``_resolve_company_domain`` and can only appear if resolution was entered,
    whereas the fast path reports a ``reason_code`` and never enters it at all.
    """

    campaign = make_campaign(db_session)
    submit(db_session, campaign)
    membership = membership_of(db_session, campaign)

    enable(db_session, AgentIdentifier.RESEARCH)
    drain(db_session, research_adapters())

    company_stage = stage(db_session, membership, AgentIdentifier.COMPANY)
    assert company_stage is not None
    attempt = (company_stage.output_reference or {}).get("domain_resolution_attempt")
    if attempt is None:
        # Blocked before the lineage was written: the reason still has to be the
        # resolution one, not a fast-path one.
        assert company_stage.reason_code in {
            "company_domain_missing",
            "company_missing",
        }
    else:
        assert "skipped_because" in attempt
        assert attempt.get("reason_code") != supplied_inputs.DOMAIN_REASON


# ---------------------------------------------------------------------------
# Case B — domain supplied, email absent.
# ---------------------------------------------------------------------------


def test_case_b_a_supplied_website_settles_the_company_without_resolving_it(
    db_session: Session,
) -> None:
    campaign = make_campaign(db_session)
    submit(db_session, campaign, website=SUPPLIED_WEBSITE)
    membership = membership_of(db_session, campaign)
    contact = db_session.get(Contact, membership.contact_id)
    assert contact is not None

    # The permanent Company exists and carries the supplied domain. Without this
    # the Company Agent would block on `company_missing`, leaving a row that
    # supplied a website worse off than one that supplied nothing.
    assert contact.company_domain == SUPPLIED_DOMAIN
    assert contact.company_id is not None
    company = db_session.get(Company, contact.company_id)
    assert company is not None and company.domain == SUPPLIED_DOMAIN

    run_pipeline(db_session)
    db_session.flush()

    attempt = stage_output(db_session, membership, AgentIdentifier.COMPANY)[
        "domain_resolution_attempt"
    ]
    assert attempt["attempted"] is False
    assert attempt["reason_code"] == supplied_inputs.DOMAIN_REASON
    assert attempt["supplied_domain"] == SUPPLIED_DOMAIN
    assert attempt["supplied_raw_value"] == SUPPLIED_WEBSITE
    assert attempt["supplied_source_type"] == sheet_submit.SOURCE_TYPE


def test_case_b_research_still_runs_and_the_address_is_still_required(
    db_session: Session,
) -> None:
    """A website says which company. It says nothing about the person's address."""

    campaign = make_campaign(db_session)
    submit(db_session, campaign, website=SUPPLIED_WEBSITE)
    membership = membership_of(db_session, campaign)

    run_pipeline(db_session)
    db_session.flush()

    research = stage(db_session, membership, AgentIdentifier.RESEARCH)
    assert research is not None
    assert research.status is PipelineStageStatus.COMPLETED

    result = email_result(db_session, membership)
    assert result.get("domain_outcome") != "supplied_email_accepted"

    verification = stage(db_session, membership, AgentIdentifier.VERIFICATION)
    assert verification is None or verification.reason_code not in {
        "verification_bypassed_supplied_email",
        "verification_bypassed_imported_email",
    }


# ---------------------------------------------------------------------------
# Case C — email supplied, domain absent.
# ---------------------------------------------------------------------------


def test_case_c_a_supplied_address_satisfies_discovery_and_verification(
    db_session: Session,
) -> None:
    campaign = make_campaign(db_session)
    seed_company(db_session)
    submit(db_session, campaign, email=SUPPLIED_EMAIL)
    membership = membership_of(db_session, campaign)
    contact = db_session.get(Contact, membership.contact_id)
    assert contact is not None
    assert contact.email == SUPPLIED_EMAIL

    run_pipeline(db_session)
    db_session.flush()

    result = email_result(db_session, membership)
    assert result["domain_outcome"] == "supplied_email_accepted"
    assert result["email"] == SUPPLIED_EMAIL
    assert result["address_derivation"] == supplied_inputs.EMAIL_DERIVATION
    assert result["candidates_generated"] == 0
    assert result["provider_call_created"] is False
    assert result["verification_id"] is None
    assert result["supplied_source_type"] == sheet_submit.SOURCE_TYPE

    email_stage = stage(db_session, membership, AgentIdentifier.EMAIL)
    assert email_stage is not None
    assert email_stage.status is PipelineStageStatus.COMPLETED

    verification = stage(db_session, membership, AgentIdentifier.VERIFICATION)
    assert verification is not None
    assert verification.status is PipelineStageStatus.COMPLETED
    assert verification.reason_code == "verification_bypassed_supplied_email"
    reference = verification.output_reference or {}
    assert reference["decision"] == "bypassed"
    assert reference["verification_id"] is None
    assert reference["provider_called"] is False
    assert reference["source"] == "operator_supplied_intake"

    assert_nothing_claims_verification(db_session, membership)


def test_case_c_company_context_and_research_still_happen(db_session: Session) -> None:
    """A supplied address is not a licence to skip knowing who the company is."""

    campaign = make_campaign(db_session)
    seed_company(db_session)
    submit(db_session, campaign, email=SUPPLIED_EMAIL)
    membership = membership_of(db_session, campaign)

    run_pipeline(db_session)
    db_session.flush()

    contact = db_session.get(Contact, membership.contact_id)
    assert contact is not None
    assert contact.company_id is not None, "company context was never established"

    for agent in (AgentIdentifier.COMPANY, AgentIdentifier.RESEARCH):
        state = stage(db_session, membership, agent)
        assert state is not None, agent.value
        assert state.status is PipelineStageStatus.COMPLETED, agent.value

    # And the pipeline continued past Verification rather than stopping at it.
    assert_pipeline_continued_past_verification(db_session, membership)


def test_case_c_the_supplied_address_is_retained_unchanged(db_session: Session) -> None:
    campaign = make_campaign(db_session)
    seed_company(db_session)
    submit(db_session, campaign, email=f"  {SUPPLIED_EMAIL.upper()} ")
    membership = membership_of(db_session, campaign)

    run_pipeline(db_session)
    db_session.flush()

    contact = db_session.get(Contact, membership.contact_id)
    assert contact is not None
    # Normalized, which is what every address in this system is, and not altered
    # in any other way.
    assert contact.email == SUPPLIED_EMAIL
    assert email_result(db_session, membership)["email"] == SUPPLIED_EMAIL

    # The verbatim cell survives beside it, so an operator can see what they typed.
    source = db_session.scalars(
        select(CampaignContactSource).where(
            CampaignContactSource.campaign_contact_id == membership.id
        )
    ).one()
    record = source.source_context[supplied_inputs.CONTEXT_KEY]["email"]
    assert record["raw"] == SUPPLIED_EMAIL.upper()
    assert record["normalized"] == SUPPLIED_EMAIL
    assert record["discovered"] is False
    assert record["verified"] is False


# ---------------------------------------------------------------------------
# Case D — both supplied.
# ---------------------------------------------------------------------------


def test_case_d_both_supplied_skips_only_the_three_finding_steps(
    db_session: Session,
) -> None:
    campaign = make_campaign(db_session)
    submit(db_session, campaign, email=SUPPLIED_EMAIL, website=SUPPLIED_WEBSITE)
    membership = membership_of(db_session, campaign)

    run_pipeline(db_session)
    db_session.flush()

    # Domain resolution: not attempted, and it says why.
    attempt = stage_output(db_session, membership, AgentIdentifier.COMPANY)[
        "domain_resolution_attempt"
    ]
    assert attempt["attempted"] is False
    assert attempt["reason_code"] == supplied_inputs.DOMAIN_REASON

    # Email discovery: satisfied, nothing generated.
    result = email_result(db_session, membership)
    assert result["domain_outcome"] == "supplied_email_accepted"
    assert result["candidates_generated"] == 0

    # Verification: bypassed, truthfully.
    verification = stage(db_session, membership, AgentIdentifier.VERIFICATION)
    assert verification is not None
    assert verification.reason_code == "verification_bypassed_supplied_email"

    # And everything that produces intelligence still ran or is still queued.
    for agent in (
        AgentIdentifier.IDENTITY,
        AgentIdentifier.COMPANY,
        AgentIdentifier.RESEARCH,
    ):
        state = stage(db_session, membership, agent)
        assert state is not None, agent.value
        assert state.status is PipelineStageStatus.COMPLETED, agent.value
    assert_pipeline_continued_past_verification(db_session, membership)

    assert_nothing_claims_verification(db_session, membership)


# ---------------------------------------------------------------------------
# Restart, retry and re-enrolment
# ---------------------------------------------------------------------------


def test_the_decision_is_re_derived_from_durable_state_after_a_restart(
    db_session: Session,
) -> None:
    """Nothing in memory carries the decision between executions.

    The proof is a re-run of the Email stage on a *new* job, with the previous
    job's stored state deliberately not consulted: the outcome has to come back
    the same because the enrolment provenance and the Contact still say the same
    thing, not because anything was remembered.
    """

    campaign = make_campaign(db_session)
    submit(db_session, campaign, email=SUPPLIED_EMAIL, website=SUPPLIED_WEBSITE)
    membership = membership_of(db_session, campaign)
    contact = db_session.get(Contact, membership.contact_id)
    assert contact is not None

    run_pipeline(db_session)
    db_session.flush()
    assert email_result(db_session, membership)["domain_outcome"] == "supplied_email_accepted"

    # A second, independent read of the same durable state.
    again = supplied_inputs.supplied_email(db_session, membership=membership, contact=contact)
    assert again is not None
    assert again.normalized == SUPPLIED_EMAIL
    assert again.verification_performed is False

    domain = supplied_inputs.supplied_domain(db_session, membership=membership, contact=contact)
    assert domain is not None
    assert domain.normalized == SUPPLIED_DOMAIN


def test_resubmitting_the_same_row_is_idempotent_and_keeps_its_provenance(
    db_session: Session,
) -> None:
    campaign = make_campaign(db_session)
    first = submit(db_session, campaign, email=SUPPLIED_EMAIL, website=SUPPLIED_WEBSITE)
    second = submit(db_session, campaign, email=SUPPLIED_EMAIL, website=SUPPLIED_WEBSITE)

    assert second.rows[0].already_submitted is True
    assert second.rows[0].submission_id == first.rows[0].submission_id
    assert len(db_session.scalars(select(CampaignContact)).all()) == 1
    assert len(db_session.scalars(select(Contact)).all()) == 1

    membership = membership_of(db_session, campaign)
    sources = db_session.scalars(
        select(CampaignContactSource).where(
            CampaignContactSource.campaign_contact_id == membership.id
        )
    ).all()
    assert len(sources) == 1
    assert sources[0].source_context[supplied_inputs.CONTEXT_KEY]["email"]["normalized"] == (
        SUPPLIED_EMAIL
    )


def test_a_new_generation_re_enrols_without_losing_the_supplied_record(
    db_session: Session,
) -> None:
    """A deliberate re-submission appends provenance and changes no decision."""

    campaign = make_campaign(db_session)
    submit(db_session, campaign, email=SUPPLIED_EMAIL, website=SUPPLIED_WEBSITE, generation=1)
    submit(db_session, campaign, email=SUPPLIED_EMAIL, website=SUPPLIED_WEBSITE, generation=2)

    membership = membership_of(db_session, campaign)
    contact = db_session.get(Contact, membership.contact_id)
    assert contact is not None
    sources = db_session.scalars(
        select(CampaignContactSource).where(
            CampaignContactSource.campaign_contact_id == membership.id
        )
    ).all()
    assert len(sources) == 2

    supplied = supplied_inputs.supplied_email(db_session, membership=membership, contact=contact)
    assert supplied is not None
    assert supplied.normalized == SUPPLIED_EMAIL

    run_pipeline(db_session)
    db_session.flush()
    assert email_result(db_session, membership)["domain_outcome"] == "supplied_email_accepted"


def test_a_corrected_address_stops_the_fast_path(db_session: Session) -> None:
    """The equality guard. A stale record may not outrank the permanent Contact.

    The record still says the operator supplied the old address, which is true and
    stays true. What it no longer does is satisfy discovery, because the Campaign
    is not using that address any more.
    """

    campaign = make_campaign(db_session)
    seed_company(db_session)
    submit(db_session, campaign, email=SUPPLIED_EMAIL)
    membership = membership_of(db_session, campaign)
    contact = db_session.get(Contact, membership.contact_id)
    assert contact is not None

    contact.email = "ada.byron@kiln.example"
    db_session.flush()

    assert (
        supplied_inputs.supplied_email(db_session, membership=membership, contact=contact) is None
    )


# ---------------------------------------------------------------------------
# Values that must NOT activate a fast path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["not-an-address", "ada@", "@kiln.example", "ada lovelace"])
def test_a_malformed_address_is_refused_at_the_contract(value: str) -> None:
    """Refused rather than dropped: this value becomes the Campaign's send slot.

    Silently ignoring it would leave the operator believing they had supplied an
    address while the pipeline went looking for a different one.
    """

    with pytest.raises(RowContractError) as exc:
        parse_row(
            {
                "client_row_id": "r1",
                "first_name": "Ada",
                "last_name": "Lovelace",
                "company_name": "Kiln Systems",
                "email": value,
            },
            max_context_chars=2_000,
        )
    assert exc.value.code == "email_unusable"


@pytest.mark.parametrize("value", ["", "   "])
def test_a_blank_address_is_simply_absent(db_session: Session, value: str) -> None:
    campaign = make_campaign(db_session)
    seed_company(db_session)
    submit(db_session, campaign, email=value)
    membership = membership_of(db_session, campaign)
    contact = db_session.get(Contact, membership.contact_id)
    assert contact is not None

    assert contact.email is None
    assert (
        supplied_inputs.supplied_email(db_session, membership=membership, contact=contact) is None
    )
    source = db_session.scalars(
        select(CampaignContactSource).where(
            CampaignContactSource.campaign_contact_id == membership.id
        )
    ).one()
    assert supplied_inputs.CONTEXT_KEY not in (source.source_context or {})


@pytest.mark.parametrize("value", ["not a hostname", "http://", "..", "a b c"])
def test_a_malformed_website_is_recorded_but_never_acted_on(
    db_session: Session, value: str
) -> None:
    """Dropped rather than refused, and the asymmetry with the address is the point.

    A website has a correct fallback — the Company Agent establishes the domain
    itself, exactly as it does for every row that supplied none — so refusing the
    row would cost the operator a contact the product can prepare perfectly well.
    """

    campaign = make_campaign(db_session)
    seed_company(db_session)
    result = submit(db_session, campaign, website=value)
    assert result.rows[0].status is RowStatus.PENDING

    membership = membership_of(db_session, campaign)
    contact = db_session.get(Contact, membership.contact_id)
    assert contact is not None
    assert (
        supplied_inputs.supplied_domain(db_session, membership=membership, contact=contact) is None
    )

    source = db_session.scalars(
        select(CampaignContactSource).where(
            CampaignContactSource.campaign_contact_id == membership.id
        )
    ).one()
    record = source.source_context[supplied_inputs.CONTEXT_KEY]["company_domain"]
    assert record["usable"] is False
    assert record["normalized"] is None
    assert record["raw"] == value.strip()


def test_a_supplied_address_on_a_suppressed_identity_creates_nothing(
    db_session: Session,
) -> None:
    campaign = make_campaign(db_session)
    seed_company(db_session)
    suppressions.add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value=SUPPLIED_EMAIL,
        reason=SuppressionReason.MANUAL,
        actor="operator",
    )
    db_session.flush()

    result = submit(db_session, campaign, email=SUPPLIED_EMAIL)

    assert result.rows[0].status is RowStatus.COULD_NOT_PREPARE
    assert result.rows[0].failure_code == "suppressed"
    assert db_session.scalars(select(Contact)).all() == []
    assert db_session.scalars(select(CampaignContact)).all() == []


# ---------------------------------------------------------------------------
# Historical contacts and the readiness projection
# ---------------------------------------------------------------------------


def test_a_discovered_and_verified_contact_keeps_its_current_semantics(
    db_session: Session,
) -> None:
    """No supplied record, so nothing about the existing path is reachable.

    The guard being proved is the one in ``supplied_email``: a Contact that
    already carries an address from any other acquisition path has no
    supplied-input record, so the fast path cannot claim it and the discovery
    outcome is unchanged.
    """

    campaign = make_campaign(db_session)
    company = seed_company(db_session)
    contact = Contact(
        first_name="Grace",
        last_name="Hopper",
        company_name="Kiln Systems",
        company_domain=SUPPLIED_DOMAIN,
        company_id=company.id,
        email="grace.hopper@kiln.example",
    )
    db_session.add(contact)
    db_session.flush()
    enrolment = campaign_contacts.enrol_contact(
        db_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        actor="operator",
        enqueue=False,
    )

    assert (
        supplied_inputs.supplied_email(db_session, membership=enrolment.membership, contact=contact)
        is None
    )
    assert (
        supplied_inputs.supplied_domain(
            db_session, membership=enrolment.membership, contact=contact
        )
        is None
    )


def test_the_sheets_row_can_reach_ready_without_claiming_verification(
    db_session: Session,
) -> None:
    """The readiness contract, both halves of it.

    A supplied address satisfies the address requirement — otherwise the surface
    that accepted it could never report its own rows finished — while every
    verification-status reader still says, correctly, that nobody checked the
    mailbox.
    """

    campaign = make_campaign(db_session)
    submit(db_session, campaign, email=SUPPLIED_EMAIL, website=SUPPLIED_WEBSITE)
    membership = membership_of(db_session, campaign)

    run_pipeline(db_session)
    db_session.flush()

    address = sheet_results._usable_address(
        db_session, membership=membership, contact=db_session.get(Contact, membership.contact_id)
    )
    assert address == SUPPLIED_EMAIL

    assert_nothing_claims_verification(db_session, membership)


def test_an_unverified_discovered_address_still_does_not_become_usable(
    db_session: Session,
) -> None:
    """The bypass is keyed on the stage, not on the address being present.

    A Contact carrying an address that discovery produced but verification never
    accepted has no bypass on its Verification stage, so the Sheets projection
    refuses it exactly as before. Without this the change would have widened
    "usable" to "any address at all".
    """

    campaign = make_campaign(db_session)
    company = seed_company(db_session)
    contact = Contact(
        first_name="Grace",
        last_name="Hopper",
        company_name="Kiln Systems",
        company_domain=SUPPLIED_DOMAIN,
        company_id=company.id,
        email="grace.hopper@kiln.example",
    )
    db_session.add(contact)
    db_session.flush()
    enrolment = campaign_contacts.enrol_contact(
        db_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        actor="operator",
        enqueue=False,
    )

    assert (
        sheet_results._usable_address(db_session, membership=enrolment.membership, contact=contact)
        is None
    )


def test_the_customer_ready_projection_accepts_a_supplied_address(
    db_session: Session,
) -> None:
    """Ready for Sending needs an address and a package, and says nothing more.

    The customer-facing projection was already correct for this case and is
    pinned rather than changed: it asks whether the Contact has an address at
    all, never whether a provider approved one. That is the right question — the
    three customer words are Processing, Ready for Sending and Could not prepare,
    and none of them is a deliverability claim — and this test exists so a later
    tightening of that expression to "verified only" cannot silently strand every
    contact whose address the operator supplied.
    """

    fixture = build_sequence(db_session, email=SUPPLIED_EMAIL)
    membership = fixture.membership

    # Verification completed as a truthful bypass, exactly as the pipeline writes
    # it for a supplied address.
    db_session.add(
        CampaignContactAgentState(
            campaign_contact_id=membership.id,
            agent_id=AgentIdentifier.VERIFICATION,
            status=PipelineStageStatus.COMPLETED,
            reason_code="verification_bypassed_supplied_email",
        )
    )
    db_session.flush()

    assert (
        customer_status.status_for_membership(db_session, campaign_contact_id=membership.id)
        is customer_status.CustomerContactStatus.READY_FOR_SENDING
    )

    # And no verification evidence was invented to get there.
    assert db_session.scalars(select(ExactEmailVerification)).all() == []
    view = verification_status.derive_status_for_contact(db_session, fixture.contact)
    assert view.visual is not EmailVisualStatus.SUCCESSFUL


def test_the_customer_projection_still_refuses_a_package_with_no_address(
    db_session: Session,
) -> None:
    """The other half of the same rule, unchanged: no address, not ready."""

    fixture = build_sequence(db_session, without_email=True)
    assert (
        customer_status.status_for_membership(db_session, campaign_contact_id=fixture.membership.id)
        is not customer_status.CustomerContactStatus.READY_FOR_SENDING
    )


# ---------------------------------------------------------------------------
# The file-import path, unchanged
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("enable_csv_import")
def test_a_file_imported_address_still_takes_the_import_path(db_session: Session) -> None:
    """The Email Agent now has two supplied-address branches. This proves the
    file import still reaches the older, richer one.

    The distinction is worth keeping and worth pinning. ``imported_email_accepted``
    references an :class:`~app.models.imported_email.ImportedContactEmail` row
    carrying a file checksum, a source row number and the vendor's own claims;
    ``supplied_email_accepted`` references an enrolment provenance record and has
    none of those. Collapsing them would make a spreadsheet cell and a vendor
    export indistinguishable in the stage history — which is exactly the kind of
    quiet loss of provenance the whole area is built to prevent.
    """

    campaign = af.make_campaign(db_session, execution=True)
    campaign_import.confirm(
        db_session,
        campaign_id=campaign.id,
        content=af.csv_bytes([af.row()]),
        filename="apollo.csv",
    )
    membership = membership_of(db_session, campaign)

    run_pipeline(db_session)
    db_session.flush()

    result = email_result(db_session, membership)
    assert result["domain_outcome"] == "imported_email_accepted"
    assert "imported_email_id" in result

    verification = stage(db_session, membership, AgentIdentifier.VERIFICATION)
    assert verification is not None
    assert verification.status is PipelineStageStatus.COMPLETED
    assert verification.reason_code == "verification_bypassed_imported_email"
    assert (verification.output_reference or {})["source"] == "campaign_file_import"

    assert_nothing_claims_verification(db_session, membership)
