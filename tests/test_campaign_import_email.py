"""The imported-email truth model (IMP-001 §25.20-28).

The assertions here are mostly about what does NOT exist: no candidate rows, no
verification evidence, no provider call, no claim of deliverability anywhere.
That is the point of the feature. A test suite that only checked the address
arrived would pass just as happily against an implementation that quietly
verified it.
"""

from __future__ import annotations

import pytest
from app.models.email_candidate import EmailCandidate
from app.models.email_evidence import ExactEmailVerification
from app.models.enums import (
    AgentControlStatus,
    AgentIdentifier,
    ImportedEmailSlot,
    ImportedEmailStageOutcome,
    ImportedVerificationOutcome,
    PipelineStageStatus,
)
from app.models.imported_email import ImportedContactEmail
from app.models.verification_job import AgentJob
from app.services.agents import controls
from app.services.agents.adapters import (
    DEFAULT_ADAPTERS,
    AgentAdapter,
    VerificationAgentAdapter,
)
from app.services.agents.orchestrator import run_next
from app.services.imports import apollo, campaign_import
from app.services.pipeline import agent_state
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tests import apollo_factory as af

pytestmark = pytest.mark.usefixtures("enable_csv_import")

WORKER = "import-email-worker"


class _ExplodingProviderFactory:
    """A provider seam that fails the test if anything ever asks for a provider.

    Stronger than asserting no provider call was recorded: this fails at the
    moment of construction, so an implementation that built a MillionVerifier
    client and then decided not to use it would still be caught.
    """

    def __call__(self, _settings: object) -> object:  # pragma: no cover - must not run
        raise AssertionError(
            "The imported-address path constructed a verification provider. It must "
            "never call MillionVerifier, ZeroBounce or any other provider."
        )


def _adapters() -> dict[AgentIdentifier, AgentAdapter]:
    adapters = dict(DEFAULT_ADAPTERS)
    adapters[AgentIdentifier.VERIFICATION] = VerificationAgentAdapter(
        provider_factory=_ExplodingProviderFactory()  # type: ignore[arg-type]
    )
    return adapters


def _import_one(session: Session, **overrides: str) -> tuple[object, ImportedContactEmail]:
    campaign = af.make_campaign(session, execution=True)
    result = campaign_import.confirm(
        session,
        campaign_id=campaign.id,
        content=af.csv_bytes([af.row(**overrides)]),
        filename="apollo.csv",
    )
    assert result.imported == 1
    record = session.scalars(
        select(ImportedContactEmail).where(
            ImportedContactEmail.slot == ImportedEmailSlot.PRIMARY,
        )
    ).one()
    return campaign, record


# --- 20-21. Provenance is persisted, and it stays the vendor's claim ---------


def test_imported_primary_email_is_persisted_with_full_source_provenance(
    db_session: Session,
) -> None:
    _campaign, record = _import_one(db_session)

    assert record.normalized_email == "ada@engines.example"
    assert record.raw_email == "ada@engines.example"
    assert record.slot is ImportedEmailSlot.PRIMARY
    assert record.source_schema == apollo.APOLLO_SCHEMA_ID
    assert record.source_row_number == 1
    assert len(record.source_file_checksum) == 64
    assert len(record.row_fingerprint) == 64
    assert record.import_batch_id is not None
    assert record.campaign_id is not None
    assert record.contact_id is not None


def test_provider_metadata_is_stored_as_the_providers_claim(db_session: Session) -> None:
    _campaign, record = _import_one(db_session)

    assert record.provider_source == "Apollo"
    assert record.provider_verification_source == "Apollo Verification"
    assert record.provider_status_raw == "Valid"
    assert record.provider_last_verified_raw == "2026-05-01T09:30:00Z"
    assert record.provider_last_verified_at is not None

    # VMR's own outcomes are separate columns and say only what VMR did.
    assert record.email_stage_outcome is ImportedEmailStageOutcome.IMPORTED_EMAIL_ACCEPTED
    assert (
        record.verification_stage_outcome
        is ImportedVerificationOutcome.VERIFICATION_BYPASSED_IMPORTED_EMAIL
    )


def test_the_display_projection_never_calls_anything_verified(db_session: Session) -> None:
    _campaign, record = _import_one(db_session)
    summary = campaign_import.imported_email_summary(record)

    assert summary["provider_claimed_status"] == "valid"
    assert summary["vmr_email_stage_outcome"] == "imported_email_accepted"
    assert summary["vmr_verification_stage_outcome"] == "verification_bypassed_imported_email"
    # Nothing in the projection asserts a verification VMR did not perform.
    assert "verified" not in summary
    assert not any(
        isinstance(value, str) and value in {"valid_verified", "vmr_verified", "deliverable"}
        for value in summary.values()
    )


# --- 22-23. Casing normalizes consistently, meaning is preserved -------------


@pytest.mark.parametrize("supplied", ["Valid", "valid", "VALID", " Valid "])
def test_provider_status_casing_normalizes_consistently(db_session: Session, supplied: str) -> None:
    _campaign, record = _import_one(db_session, **{"Email Status": supplied})
    assert record.provider_status_normalized == "valid"
    assert record.provider_status_raw == supplied.strip()


@pytest.mark.parametrize("supplied", ["Catch-all", "catch-all", "CATCH-ALL", "Catch All"])
def test_catch_all_casing_normalizes_consistently(db_session: Session, supplied: str) -> None:
    _campaign, record = _import_one(db_session, **{"Primary Email Catch-all Status": supplied})
    assert record.provider_catch_all_normalized == "catch_all"
    assert record.provider_catch_all_raw == supplied.strip()


def test_normalization_folds_case_without_translating_meaning() -> None:
    assert apollo.normalize_provider_token("Valid") == apollo.normalize_provider_token("valid")
    assert apollo.normalize_provider_token("Catch-all") == apollo.normalize_provider_token(
        "catch all"
    )
    # A vendor status this system does not recognise is kept, not mapped away.
    assert apollo.normalize_provider_token("Unverifiable") == "unverifiable"
    assert apollo.normalize_provider_token("") is None


def test_an_unreadable_provider_timestamp_keeps_its_raw_text(db_session: Session) -> None:
    _campaign, record = _import_one(
        db_session, **{"Primary Email Last Verified At": "last Tuesday"}
    )
    assert record.provider_last_verified_at is None
    assert record.provider_last_verified_raw == "last Tuesday"


# --- 24-27. No discovery, no provider, an explicit bypass -------------------


def _run_to_email(session: Session, campaign: object) -> object:
    """Drive the pipeline until the Email stage has run for the imported contact."""

    for agent in (AgentIdentifier.EMAIL, AgentIdentifier.VERIFICATION):
        controls.set_global_control(
            session, agent_id=agent, status=AgentControlStatus.ENABLED, config={"live": True}
        )
    session.flush()
    adapters = _adapters()
    last = None
    for _ in range(10):
        last = run_next(session, worker_id=WORKER, adapters=adapters)
        if last.job is None:
            break
    return last


def test_email_stage_completes_through_the_imported_path_without_candidates(
    db_session: Session,
) -> None:
    campaign, record = _import_one(db_session)
    _run_to_email(db_session, campaign)

    membership_id = db_session.scalars(
        select(ImportedContactEmail.contact_id).where(ImportedContactEmail.id == record.id)
    ).one()
    assert membership_id is not None

    email_job = db_session.scalars(
        select(AgentJob).where(AgentJob.agent_id == AgentIdentifier.EMAIL)
    ).one()
    assert email_job.result is not None
    assert email_job.result["domain_outcome"] == "imported_email_accepted"
    assert email_job.result["candidates_generated"] == 0
    assert email_job.result["provider_call_created"] is False
    assert email_job.result["address_derivation"] == "operator_supplied_import_no_discovery"
    assert email_job.result["verification_id"] is None

    # 24. No candidate address was generated for this contact, at all.
    assert db_session.scalar(select(func.count()).select_from(EmailCandidate)) == 0


def test_no_verification_provider_is_called_and_no_evidence_is_written(
    db_session: Session,
) -> None:
    campaign, _record = _import_one(db_session)
    _run_to_email(db_session, campaign)

    # 25. No Verification job was ever created, so nothing could call a provider.
    #     The exploding provider factory in `_adapters` is the belt to this brace.
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(AgentJob)
            .where(AgentJob.agent_id == AgentIdentifier.VERIFICATION)
        )
        == 0
    )
    # 27. And no verification evidence exists to be mistaken for a VMR verdict.
    assert db_session.scalar(select(func.count()).select_from(ExactEmailVerification)) == 0


def test_verification_stage_completes_through_an_explicit_visible_bypass(
    db_session: Session,
) -> None:
    campaign, record = _import_one(db_session)
    _run_to_email(db_session, campaign)

    from app.models.campaign import CampaignContact

    membership = db_session.scalars(
        select(CampaignContact).where(CampaignContact.campaign_id == campaign.id)  # type: ignore[attr-defined]
    ).one()
    state = agent_state(
        db_session,
        campaign_contact_id=membership.id,
        agent_id=AgentIdentifier.VERIFICATION,
        create=False,
    )
    assert state is not None
    assert state.status is PipelineStageStatus.COMPLETED
    assert state.reason_code == "verification_bypassed_imported_email"
    assert state.output_reference is not None
    assert state.output_reference["decision"] == "bypassed"
    assert state.output_reference["verification_id"] is None
    assert state.output_reference["provider_called"] is False
    assert state.output_reference["source"] == "campaign_file_import"
    assert state.output_reference["imported_email_id"] == str(record.id)


def test_the_bypass_is_visible_in_stage_history(db_session: Session) -> None:
    campaign, _record = _import_one(db_session)
    _run_to_email(db_session, campaign)

    from app.models.pipeline import PipelineEvent

    events = db_session.scalars(
        select(PipelineEvent).where(PipelineEvent.agent_id == AgentIdentifier.VERIFICATION)
    ).all()
    assert any(event.reason_code == "verification_bypassed_imported_email" for event in events)


# --- 28. Alternates are retained and never promoted -------------------------


def test_secondary_and_tertiary_emails_are_retained_but_never_promoted(
    db_session: Session,
) -> None:
    campaign = af.make_campaign(db_session)
    result = campaign_import.confirm(
        db_session,
        campaign_id=campaign.id,
        content=af.csv_bytes(
            [
                af.row(
                    **{
                        "Secondary Email": "A.Lovelace@Engines.Example",
                        "Secondary Email Source": "FindyMail",
                        "Secondary Email Status": "Catch-all",
                        "Secondary Email Verification Source": "ZeroBounce",
                        "Secondary Email Last Verified At": "2026-04-02",
                        "Tertiary Email": "ada.personal@gmail.com",
                        "Tertiary Email Status": "Valid",
                    }
                )
            ]
        ),
        filename="apollo.csv",
    )
    assert result.imported == 1

    records = {
        record.slot: record for record in db_session.scalars(select(ImportedContactEmail)).all()
    }
    assert set(records) == {
        ImportedEmailSlot.PRIMARY,
        ImportedEmailSlot.SECONDARY,
        ImportedEmailSlot.TERTIARY,
    }

    secondary = records[ImportedEmailSlot.SECONDARY]
    assert secondary.normalized_email == "a.lovelace@engines.example"
    assert secondary.provider_source == "FindyMail"
    assert secondary.provider_status_normalized == "catch_all"
    assert secondary.provider_verification_source == "ZeroBounce"
    assert secondary.provider_last_verified_at is not None
    # An alternate carries NO stage outcome: nothing acted on it.
    assert secondary.email_stage_outcome is None
    assert secondary.verification_stage_outcome is None

    # The primary is still the primary, on the permanent Contact too.
    primary = records[ImportedEmailSlot.PRIMARY]
    assert primary.is_accepted_primary
    from app.models.contact import Contact

    contact = db_session.scalars(select(Contact)).one()
    assert contact.email == "ada@engines.example"


def test_a_malformed_primary_with_a_valid_secondary_is_flagged_never_swapped(
    db_session: Session,
) -> None:
    campaign = af.make_campaign(db_session)
    result = campaign_import.confirm(
        db_session,
        campaign_id=campaign.id,
        content=af.csv_bytes(
            [
                af.row(
                    **{
                        "Email": "not-an-address",
                        "Secondary Email": "ada@engines.example",
                    }
                )
            ]
        ),
        filename="apollo.csv",
    )
    assert result.failed == 1
    assert result.imported == 0

    from app.models.contact import Contact

    assert db_session.scalar(select(func.count()).select_from(Contact)) == 0


def test_alternate_addresses_at_a_public_provider_are_warned_about() -> None:
    detection = apollo.detect_schema(list(af.APOLLO_HEADER))
    parsed = apollo.read_row(
        af.row(**{"Secondary Email": "ada@gmail.com"}), detection, row_number=1
    )
    codes = {code for code, _message in parsed.warnings}
    assert "secondary_public_domain" in codes


def test_a_duplicate_address_across_slots_is_warned_about() -> None:
    detection = apollo.detect_schema(list(af.APOLLO_HEADER))
    parsed = apollo.read_row(
        af.row(**{"Secondary Email": "ADA@engines.example"}), detection, row_number=1
    )
    codes = {code for code, _message in parsed.warnings}
    assert "duplicate_address" in codes


def test_addresses_spanning_two_companies_are_warned_about() -> None:
    detection = apollo.detect_schema(list(af.APOLLO_HEADER))
    parsed = apollo.read_row(
        af.row(**{"Secondary Email": "ada@otherfirm.example"}), detection, row_number=1
    )
    codes = {code for code, _message in parsed.warnings}
    assert "addresses_span_companies" in codes
