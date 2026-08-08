"""Idempotency, partial success, and failure safety (IMP-001 §25.38-44)."""

from __future__ import annotations

from typing import Any

import pytest
from app.models.campaign import CampaignContact
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_evidence import ExactEmailVerification
from app.models.enums import AgentControlStatus, AgentIdentifier, ImportBatchStatus
from app.models.import_batch import ImportBatch, ImportRow, ImportRowValidation
from app.models.imported_email import ImportedContactEmail, ImportSourceIdentifier
from app.models.provenance import ProvenanceRecord
from app.models.verification_job import AgentJob
from app.services.agents import controls
from app.services.agents.orchestrator import run_next
from app.services.imports import campaign_import
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from tests import apollo_factory as af

pytestmark = pytest.mark.usefixtures("enable_csv_import")

WORKER = "import-idempotency-worker"


def _counts(session: Session) -> dict[str, int]:
    return {
        model.__name__: int(session.scalar(select(func.count()).select_from(model)) or 0)
        for model in (
            Contact,
            Company,
            CampaignContact,
            ImportedContactEmail,
            ImportSourceIdentifier,
            ProvenanceRecord,
            AgentJob,
        )
    }


def _confirm(session: Session, campaign: Any, rows: list[dict[str, str]], name: str) -> Any:
    return campaign_import.confirm(
        session, campaign_id=campaign.id, content=af.csv_bytes(rows), filename=name
    )


# --- 38-39. The same file, and the same row ---------------------------------


def test_reimporting_the_same_file_into_the_same_campaign_creates_nothing_new(
    db_session: Session,
) -> None:
    campaign = af.make_campaign(db_session)
    rows = [af.row(), af.row(**{"Email": "grace@engines.example", "First Name": "Grace"})]
    first = _confirm(db_session, campaign, rows, "apollo.csv")
    after_first = _counts(db_session)

    second = _confirm(db_session, campaign, rows, "apollo.csv")
    assert second.reused_existing_batch is True
    assert second.batch_id == first.batch_id
    assert _counts(db_session) == after_first
    # And exactly one batch exists, not two.
    assert db_session.scalar(select(func.count()).select_from(ImportBatch)) == 1


def test_the_same_row_never_produces_duplicate_email_provenance(db_session: Session) -> None:
    campaign = af.make_campaign(db_session)
    _confirm(db_session, campaign, [af.row()], "apollo.csv")
    # A different FILE (so a new batch) that repeats the identical row.
    _confirm(db_session, campaign, [af.row(), af.row(**{"Email": "g@engines.example"})], "v2.csv")

    ada_evidence = db_session.scalars(
        select(ImportedContactEmail).where(
            ImportedContactEmail.normalized_email == "ada@engines.example"
        )
    ).all()
    assert len(ada_evidence) == 1
    assert db_session.scalar(select(func.count()).select_from(Contact)) == 2


def test_a_repeated_row_is_reported_rather_than_silently_dropped(db_session: Session) -> None:
    campaign = af.make_campaign(db_session)
    _confirm(db_session, campaign, [af.row()], "apollo.csv")
    second = _confirm(
        db_session, campaign, [af.row(), af.row(**{"Email": "g@e.example"})], "v2.csv"
    )

    assert second.skipped_duplicate == 1
    views, _total = campaign_import.batch_rows(db_session, batch_id=second.batch_id)
    repeated = next(view for view in views if view.row.row_number == 1)
    assert repeated.validation is not None
    assert repeated.validation.error_code == "already_imported"


# --- 40. A modified file processes only what genuinely changed --------------


def test_a_modified_file_processes_only_new_or_changed_rows(db_session: Session) -> None:
    campaign = af.make_campaign(db_session)
    original = [af.row(), af.row(**{"Email": "grace@engines.example", "First Name": "Grace"})]
    _confirm(db_session, campaign, original, "apollo.csv")

    modified = [
        af.row(),  # byte-identical row
        af.row(**{"Email": "grace@engines.example", "First Name": "Grace"}),  # identical
        af.row(**{"Email": "joan@engines.example", "First Name": "Joan"}),  # new
    ]
    second = _confirm(db_session, campaign, modified, "apollo-v2.csv")

    assert second.imported == 1
    assert second.skipped_duplicate == 2
    assert db_session.scalar(select(func.count()).select_from(Contact)) == 3


def test_a_changed_row_is_treated_as_new_evidence_not_a_duplicate(db_session: Session) -> None:
    campaign = af.make_campaign(db_session)
    _confirm(db_session, campaign, [af.row()], "apollo.csv")
    # Same person, a changed title: a genuinely different statement about them.
    second = _confirm(db_session, campaign, [af.row(**{"Title": "Director"})], "apollo-v2.csv")
    assert second.already_in_campaign == 1
    assert second.skipped_duplicate == 0
    # Still one person and one membership.
    assert db_session.scalar(select(func.count()).select_from(Contact)) == 1
    assert db_session.scalar(select(func.count()).select_from(CampaignContact)) == 1


# --- 41-42. Partial success and transactional safety ------------------------


def test_one_invalid_row_does_not_abort_the_valid_rows(db_session: Session) -> None:
    campaign = af.make_campaign(db_session)
    result = _confirm(
        db_session,
        campaign,
        [
            af.row(),
            af.row(**{"Email": "definitely-not-an-address", "First Name": "Broken"}),
            af.row(**{"Email": "grace@engines.example", "First Name": "Grace"}),
            af.row(**{"First Name": "", "Last Name": "", "Email": "anon@engines.example"}),
        ],
        "mixed.csv",
    )
    assert result.imported == 2
    assert result.failed == 2
    assert result.status is ImportBatchStatus.COMPLETED
    assert db_session.scalar(select(func.count()).select_from(Contact)) == 2

    # Every row kept its raw capture and got exactly one outcome.
    assert db_session.scalar(select(func.count()).select_from(ImportRow)) == 4
    assert db_session.scalar(select(func.count()).select_from(ImportRowValidation)) == 4


def test_a_database_failure_on_one_row_leaves_no_half_written_identity(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Row 2 dies mid-write; it takes its Company and Contact down with it."""

    campaign = af.make_campaign(db_session)
    real_enrol = campaign_import.enrol_contact
    calls = {"n": 0}

    def exploding_enrol(*args: object, **kwargs: object) -> object:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OperationalError("INSERT", {}, Exception("simulated database failure"))
        return real_enrol(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(campaign_import, "enrol_contact", exploding_enrol)

    result = _confirm(
        db_session,
        campaign,
        [
            af.row(),
            af.row(
                **{
                    "Email": "grace@doomed.example",
                    "First Name": "Grace",
                    "Website": "https://doomed.example",
                    "Company Name": "Doomed Ltd",
                    "Apollo Account Id": "apollo-account-doomed",
                }
            ),
            af.row(**{"Email": "joan@engines.example", "First Name": "Joan"}),
        ],
        "faulty.csv",
    )

    assert result.imported == 2
    assert result.failed == 1

    # Nothing from the failed row survived — not the Company, not the Contact.
    doomed = db_session.scalars(select(Company).where(Company.domain == "doomed.example"))
    assert doomed.first() is None
    assert (
        db_session.scalars(select(Contact).where(Contact.email == "grace@doomed.example")).first()
        is None
    )
    assert (
        db_session.scalars(
            select(ImportedContactEmail).where(
                ImportedContactEmail.normalized_email == "grace@doomed.example"
            )
        ).first()
        is None
    )

    # The other two rows are complete and untouched.
    assert db_session.scalar(select(func.count()).select_from(Contact)) == 2

    # And the failure is on record, without a stack trace or the row's PII.
    views, _total = campaign_import.batch_rows(db_session, batch_id=result.batch_id)
    failed = next(view for view in views if view.row.row_number == 2)
    assert failed.validation is not None
    assert failed.validation.error_code == "database_error"
    assert failed.validation.note is not None
    assert "grace@doomed.example" not in failed.validation.note
    assert "Traceback" not in failed.validation.note


# --- 43-44. Reconfirmation costs nothing ------------------------------------


def test_reconfirmation_creates_no_duplicate_agent_jobs_and_no_new_spend(
    db_session: Session,
) -> None:
    campaign = af.make_campaign(db_session, execution=True)
    rows = [af.row()]
    _confirm(db_session, campaign, rows, "apollo.csv")
    for agent in (AgentIdentifier.EMAIL, AgentIdentifier.VERIFICATION):
        controls.set_global_control(
            db_session, agent_id=agent, status=AgentControlStatus.ENABLED, config={"live": True}
        )
    db_session.flush()
    for _ in range(10):
        if run_next(db_session, worker_id=WORKER).job is None:
            break

    jobs_before = db_session.scalar(select(func.count()).select_from(AgentJob))
    evidence_before = db_session.scalar(select(func.count()).select_from(ExactEmailVerification))

    # Same file again — short-circuits at the batch.
    again = _confirm(db_session, campaign, rows, "apollo.csv")
    assert again.reused_existing_batch is True
    # A different file naming the same person again — reaches the row logic.
    _confirm(db_session, campaign, [af.row(), af.row(**{"Email": "g@e.example"})], "v2.csv")

    assert db_session.scalar(select(func.count()).select_from(AgentJob)) >= jobs_before
    # The imported contact never acquired verification evidence, on any pass.
    assert db_session.scalar(select(func.count()).select_from(ExactEmailVerification)) == (
        evidence_before
    )
    ada_jobs = db_session.scalars(
        select(AgentJob).where(AgentJob.agent_id == AgentIdentifier.EMAIL)
    ).all()
    # One Email job per Campaign Contact, not one per confirmation.
    assert len({job.campaign_contact_id for job in ada_jobs}) == len(ada_jobs)


def test_the_same_file_into_two_campaigns_is_two_batches_and_one_person(
    db_session: Session,
) -> None:
    first = af.make_campaign(db_session)
    second = af.make_campaign(db_session)
    rows = [af.row()]
    a = _confirm(db_session, first, rows, "apollo.csv")
    b = _confirm(db_session, second, rows, "apollo.csv")

    assert a.batch_id != b.batch_id
    assert a.imported == 1
    assert b.matched_existing == 1
    assert db_session.scalar(select(func.count()).select_from(Contact)) == 1
    assert db_session.scalar(select(func.count()).select_from(Company)) == 1
    assert db_session.scalar(select(func.count()).select_from(CampaignContact)) == 2
    # Evidence is campaign-scoped, so each campaign has its own record.
    assert db_session.scalar(select(func.count()).select_from(ImportedContactEmail)) == 2


def test_a_row_held_for_review_can_be_imported_after_the_file_is_corrected(
    db_session: Session,
) -> None:
    """A refusal must not become permanent.

    A held row still writes its supplied address as evidence, which is what the
    operator resolving it needs to see. If that evidence counted as "already
    imported", the corrected file would be reported as a duplicate and the
    person could never be imported at all.
    """

    campaign = af.make_campaign(db_session)
    # No website, no company LinkedIn, no account id, public mailbox: held.
    held_row = {
        "Email": "ada.lovelace@gmail.com",
        "Website": "",
        "Company Linkedin Url": "",
        "Apollo Account Id": "",
    }
    first = _confirm(db_session, campaign, [af.row(**held_row)], "held.csv")
    assert first.review_required == 1
    assert db_session.scalar(select(func.count()).select_from(Contact)) == 0
    # The supplied address is on record as evidence — retained, not taken, and
    # not refused either. An operator has simply not decided about it yet, and
    # labelling it "rejected" said the file was wrong about an address that may
    # well be right. (Second IMP-001 review, SR-IMP-001.)
    held_evidence = db_session.scalars(select(ImportedContactEmail)).one()
    assert held_evidence.email_stage_outcome is None
    assert held_evidence.verification_stage_outcome is None
    assert held_evidence.rejection_code == campaign_import.HELD_CODE
    assert held_evidence.contact_id is None

    # The operator adds the Website the row was missing and imports again.
    corrected = _confirm(
        db_session,
        campaign,
        [af.row(**{**held_row, "Website": "https://engines.example"})],
        "corrected.csv",
    )
    assert corrected.imported == 1
    assert corrected.skipped_duplicate == 0
    contact = db_session.scalars(select(Contact)).one()
    assert contact.email == "ada.lovelace@gmail.com"
    accepted = campaign_import.accepted_primary_email(
        db_session, campaign_id=campaign.id, contact_id=contact.id
    )
    assert accepted is not None
    assert accepted.is_accepted_primary


def test_a_duplicate_file_is_announced_rather_than_vanishing(db_session: Session) -> None:
    first = af.make_campaign(db_session)
    second = af.make_campaign(db_session)
    content = af.csv_bytes([af.row()])
    campaign_import.confirm(
        db_session, campaign_id=first.id, content=content, filename="apollo.csv"
    )

    same_campaign = campaign_import.preview(
        db_session, campaign_id=first.id, content=content, filename="apollo.csv"
    )
    assert same_campaign.duplicate_file is not None
    assert same_campaign.duplicate_file.code == "already_imported"

    other_campaign = campaign_import.preview(
        db_session, campaign_id=second.id, content=content, filename="apollo.csv"
    )
    assert other_campaign.duplicate_file is not None
    assert other_campaign.duplicate_file.code == "imported_into_another_campaign"
    # The note says the file was seen before; it does NOT name the other
    # Campaign. This is the one query in the flow that deliberately crosses the
    # Campaign boundary, and the name is the part the uploader may have no other
    # route to. (IMP-001 review, D-19.)
    assert first.name not in other_campaign.duplicate_file.message
    assert other_campaign.duplicate_file.batch_id is None
