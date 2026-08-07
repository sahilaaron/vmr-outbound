"""Regressions for the defects an independent adversarial review reproduced.

One test per corrected invariant, named for the invariant rather than for the
defect number, because a name that only means something next to a review
document stops meaning anything once the document is filed.

Every assertion here states what the system must now do. The review's own attack
suite asserted the opposite — that the defect reproduced — and is deliberately
not copied: a test that documents broken behaviour passes forever once the
behaviour is fixed, which is the least useful thing a test can do.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any

import pytest
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_candidate import EmailCandidate
from app.models.email_evidence import ExactEmailVerification
from app.models.enums import (
    AgentControlStatus,
    AgentIdentifier,
    ImportedEmailSlot,
    ImportedEmailStageOutcome,
    ImportRowOutcome,
)
from app.models.import_batch import ImportBatch, ImportRow, ImportRowValidation
from app.models.imported_email import ImportedContactEmail
from app.services.admin_workbench.import_lineage import (
    ATTENTION_OUTCOMES,
    OUTCOME_LABELS,
    UNPROCESSED_OUTCOME,
    ImportLineageReader,
)
from app.services.imports import apollo, campaign_import, parsing
from app.services.imports import normalization as norm
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tests import apollo_factory as af

pytestmark = pytest.mark.usefixtures("enable_csv_import")

MIGRATION = Path("migrations/versions/c1f7a3e29b04_campaign_contact_file_import.py")


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """A live application with both switches on and a staging directory of its own."""

    from collections.abc import Iterator  # noqa: F401

    from app.core.config import get_settings
    from app.main import create_app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__CSV_IMPORT", "true")
    monkeypatch.setenv("STAGED_UPLOADS_DIR", str(tmp_path / "staged"))
    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


def _confirm(session: Session, campaign: Any, rows: list[dict[str, str]], name: str) -> Any:
    return campaign_import.confirm(
        session, campaign_id=campaign.id, content=af.csv_bytes(rows), filename=name
    )


def _csv(header: tuple[str, ...], *rows: list[str]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(list(header))
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8")


def _read_first(content: bytes, filename: str = "dupes.csv") -> apollo.ApolloRow:
    parsed = parsing.parse_file(content, filename)
    sheet = parsed.sheets[0]
    detection = apollo.detect_schema(sheet.columns)
    row = parsed.rows[0]
    return apollo.read_row(row.raw, detection, row_number=row.row_number, cells=row.cells)


# ---------------------------------------------------------------------------
# D-1. The migration refuses to downgrade over evidence it cannot rebuild.
# ---------------------------------------------------------------------------


def test_the_migration_downgrade_guards_the_data_it_would_destroy() -> None:
    """Structural check on the migration source, alongside the round-trip test
    in ``tests/test_migrations.py`` that exercises the empty case for real."""

    body = MIGRATION.read_text().split("def downgrade()", 1)[1]
    assert "RuntimeError" in body
    assert "_unrecoverable_state" in body


def test_the_downgrade_guard_names_categories_and_leaks_no_content() -> None:
    """The refusal is read by whoever runs the migration, who is not
    necessarily entitled to read the imported data. It names what is held and
    counts it; it quotes no address, filename, identifier or SQL."""

    source = MIGRATION.read_text()
    guard = source[source.index("def _unrecoverable_state") : source.index("def downgrade()")]
    message = source.split("raise RuntimeError(", 1)[1].split(")", 1)[0]

    for category in (
        "imported address record",
        "imported source identifier",
        "per-row import decision",
        "file import batch record",
    ):
        assert category in guard
    for leak in ("SELECT ", "raw_email", "normalized_email", "filename", "%s"):
        assert leak not in message


def test_the_downgrade_guard_reads_written_defaults_as_absence() -> None:
    """A database that ran the upgrade and nothing else must still reverse.

    ``warnings`` defaults to an empty array and ``already_in_campaign_rows`` to
    zero; both were written by the upgrade itself, so treating either as a
    decision worth protecting would make the migration one-way for everyone.
    """

    source = MIGRATION.read_text()
    assert "warnings <> '[]'::jsonb" in source
    assert "already_in_campaign_rows <> 0" in source


# ---------------------------------------------------------------------------
# D-2. A repeated header name: the FIRST column wins, as detection reports.
# ---------------------------------------------------------------------------


def test_a_repeated_email_column_reads_the_first_one() -> None:
    row = _read_first(
        _csv(
            ("First Name", "Last Name", "Company Name", "Email", "Email"),
            ["Ada", "Lovelace", "Engines", "first@engines.example", "second@engines.example"],
        )
    )
    assert row.primary is not None
    assert row.primary.normalized == "first@engines.example"


def test_a_repeated_name_column_reads_the_first_one() -> None:
    row = _read_first(
        _csv(
            ("First Name", "First Name", "Last Name", "Company Name", "Email"),
            ["Ada", "Overwritten", "Lovelace", "Engines", "ada@engines.example"],
        )
    )
    assert row.first_name == "Ada"


def test_a_repeated_provider_status_column_reads_the_first_one() -> None:
    row = _read_first(
        _csv(
            ("First Name", "Last Name", "Company Name", "Email", "Email Status", "Email Status"),
            ["Ada", "Lovelace", "Engines", "ada@engines.example", "valid", "invalid"],
        )
    )
    assert row.primary is not None
    assert row.primary.provider_status_normalized == "valid"


def test_three_columns_claiming_one_field_all_lose_to_the_first() -> None:
    content = _csv(
        ("First Name", "Last Name", "Company Name", "Email", "Email", "Email"),
        ["Ada", "Lovelace", "Engines", "first@engines.example", "b@x.example", "c@x.example"],
    )
    detection = apollo.detect_schema(parsing.parse_file(content, "d.csv").sheets[0].columns)
    assert len(detection.duplicate_columns) == 2
    row = _read_first(content)
    assert row.primary is not None
    assert row.primary.normalized == "first@engines.example"


def test_a_losing_duplicate_carries_its_own_value_into_extras() -> None:
    """ "Not applied" must not become "reported as the winner's value"."""

    row = _read_first(
        _csv(
            ("First Name", "Last Name", "Company Name", "Email", "Email"),
            ["Ada", "Lovelace", "Engines", "first@engines.example", "second@engines.example"],
        )
    )
    assert "second@engines.example" in row.extras.values()


def test_case_equivalent_aliases_for_one_field_behave_like_a_duplicate() -> None:
    row = _read_first(
        _csv(
            ("First Name", "Last Name", "Company Name", "Email", "E-Mail"),
            ["Ada", "Lovelace", "Engines", "first@engines.example", "second@engines.example"],
        )
    )
    assert row.primary is not None
    assert row.primary.normalized == "first@engines.example"


def test_xlsx_resolves_a_repeated_header_the_same_way_as_csv() -> None:
    header = ("First Name", "Last Name", "Company Name", "Email", "Email")
    content = af.xlsx_bytes(
        {
            "Contacts": (
                header,
                [
                    {
                        "First Name": "Ada",
                        "Last Name": "Lovelace",
                        "Company Name": "Engines",
                        "Email": "first@engines.example",
                    }
                ],
            )
        }
    )
    parsed = parsing.parse_file(content, "dupes.xlsx")
    sheet = parsed.sheets[0]
    assert sheet.columns == header
    row = parsed.rows[0]
    assert row.cells[3] == "first@engines.example"
    reading = apollo.read_row(
        row.raw, apollo.detect_schema(sheet.columns), row_number=1, cells=row.cells
    )
    assert reading.primary is not None
    assert reading.primary.normalized == "first@engines.example"


def test_the_preview_and_the_committed_result_agree_on_the_imported_address(
    db_session: Session,
) -> None:
    content = _csv(
        ("First Name", "Last Name", "Company Name", "Email", "Email"),
        ["Ada", "Lovelace", "Engines", "first@engines.example", "second@engines.example"],
    )
    campaign = af.make_campaign(db_session)
    preview = campaign_import.preview(
        db_session, campaign_id=campaign.id, content=content, filename="dupes.csv"
    )
    previewed = preview.rows[0].apollo_row.primary
    assert previewed is not None

    campaign_import.confirm(
        db_session, campaign_id=campaign.id, content=content, filename="dupes.csv"
    )
    stored = db_session.scalars(
        select(ImportedContactEmail).where(
            ImportedContactEmail.slot == ImportedEmailSlot.PRIMARY,
            ImportedContactEmail.email_stage_outcome
            == ImportedEmailStageOutcome.IMPORTED_EMAIL_ACCEPTED,
        )
    ).one()
    assert stored.normalized_email == previewed.normalized == "first@engines.example"


# ---------------------------------------------------------------------------
# D-3. An accepted address has a domain that is a valid hostname.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("supplied", "expected"),
    [
        ("<jane@gmail.com>", "jane@gmail.com"),
        ("jane@gmail.com>", "jane@gmail.com"),
        ("  Jane@Gmail.COM  ", "jane@gmail.com"),
        ("jane@gmail.com.", "jane@gmail.com"),
        ("jane@gmail.com,", "jane@gmail.com"),
        # IDN and its punycode spelling are one mailbox, not two employers.
        ("user@bücher.de", "user@xn--bcher-kva.de"),
        ("user@xn--bcher-kva.de", "user@xn--bcher-kva.de"),
    ],
)
def test_ordinary_paste_forms_normalize_to_the_address_they_mean(
    supplied: str, expected: str
) -> None:
    normalized = norm.normalize_email(supplied)
    assert normalized == expected
    assert norm.is_valid_email(normalized) is True


@pytest.mark.parametrize("supplied", ["jane@x..com", "jane@-.-", "jane@x", "jane@.com", "jane@"])
def test_an_unusable_domain_makes_the_address_invalid(supplied: str) -> None:
    normalized = norm.normalize_email(supplied)
    assert normalized is None or norm.is_valid_email(normalized) is False


def test_a_bracketed_public_mailbox_is_recognized_as_a_public_mailbox(
    db_session: Session,
) -> None:
    """The rule apollo.py states: a personal mailbox never establishes an
    employer. Before the domain was validated, one stray ``>`` defeated it."""

    assert apollo.is_public_email_domain("gmail.com") is True
    campaign = af.make_campaign(db_session)
    _confirm(
        db_session,
        campaign,
        [af.row(**{"Email": "<jane@gmail.com>", "First Name": "Jane", "Website": ""})],
        "public.csv",
    )
    for company in db_session.scalars(select(Company)).all():
        assert company.domain != "gmail.com"
        assert company.domain != "gmail.com>"


def test_no_company_is_founded_on_a_domain_that_is_not_a_hostname(
    db_session: Session,
) -> None:
    campaign = af.make_campaign(db_session)
    _confirm(
        db_session,
        campaign,
        [af.row(**{"Email": "jane@x..com", "First Name": "Jane", "Website": ""})],
        "bad.csv",
    )
    for company in db_session.scalars(select(Company)).all():
        assert company.domain is None or norm.is_valid_hostname(company.domain)


# ---------------------------------------------------------------------------
# D-4. A bypass is not a provider pass.
# ---------------------------------------------------------------------------


def _run_pipeline(session: Session, worker: str = "review-fix-worker") -> None:
    from app.services.agents import controls
    from app.services.agents.orchestrator import run_next

    for agent in (AgentIdentifier.EMAIL, AgentIdentifier.VERIFICATION):
        controls.set_global_control(
            session, agent_id=agent, status=AgentControlStatus.ENABLED, config={"live": True}
        )
    session.flush()
    for _ in range(20):
        if run_next(session, worker_id=worker).job is None:
            break
    session.flush()


def _verification_step(session: Session, campaign_id: Any) -> Any:
    from app.core.config import get_settings
    from app.services.admin_workbench import reader as wb_reader

    view = wb_reader.AdminWorkbenchReader(session, settings=get_settings()).campaign_detail(
        campaign_id
    )
    assert view is not None
    return next(step for step in view.funnel if step.agent_id is AgentIdentifier.VERIFICATION)


def test_an_imported_contact_is_counted_as_bypassed_not_as_provider_passed(
    db_session: Session,
) -> None:
    campaign = af.make_campaign(db_session, execution=True)
    _confirm(db_session, campaign, [af.row()], "a.csv")
    _run_pipeline(db_session)

    assert db_session.scalar(select(func.count()).select_from(ExactEmailVerification)) == 0
    step = _verification_step(db_session, campaign.id)
    assert step.bypassed_through == 1
    assert step.provider_passed == 0
    assert step.has_bypassed is True


def test_a_campaign_with_no_import_reports_no_bypass(db_session: Session) -> None:
    campaign = af.make_campaign(db_session, execution=True)
    step = _verification_step(db_session, campaign.id)
    assert step.bypassed_through == 0
    assert step.has_bypassed is False
    assert step.provider_passed == step.completed_through


def test_the_two_numbers_always_account_for_everyone_past_the_stage(
    db_session: Session,
) -> None:
    campaign = af.make_campaign(db_session, execution=True)
    _confirm(
        db_session,
        campaign,
        [af.row(), af.row(**{"Email": "grace@engines.example", "First Name": "Grace"})],
        "two.csv",
    )
    _run_pipeline(db_session)
    step = _verification_step(db_session, campaign.id)
    assert step.provider_passed + step.bypassed_through == step.completed_through


# ---------------------------------------------------------------------------
# D-5. The failures inbox lists rows that need a decision, and only those.
# ---------------------------------------------------------------------------


def test_attention_outcomes_exclude_every_benign_disposition() -> None:
    assert ImportRowOutcome.ACCEPTED.value not in ATTENTION_OUTCOMES
    assert ImportRowOutcome.DUPLICATE.value not in ATTENTION_OUTCOMES
    assert ImportRowOutcome.PENDING.value not in ATTENTION_OUTCOMES
    assert ATTENTION_OUTCOMES == {
        ImportRowOutcome.REJECTED.value,
        ImportRowOutcome.AMBIGUOUS.value,
        ImportRowOutcome.SUPPRESSED.value,
    }


def test_an_already_imported_row_is_not_listed_as_a_failure(db_session: Session) -> None:
    campaign = af.make_campaign(db_session)
    _confirm(db_session, campaign, [af.row()], "a.csv")
    _confirm(
        db_session,
        campaign,
        [af.row(), af.row(**{"Email": "grace@engines.example", "First Name": "Grace"})],
        "b.csv",
    )
    db_session.flush()

    listed = ImportLineageReader(db_session).unresolved_rows()
    assert "already_imported" not in {row.error_code for row in listed}
    assert all(row.needs_attention for row in listed)


def test_a_refused_row_is_still_listed(db_session: Session) -> None:
    campaign = af.make_campaign(db_session)
    _confirm(
        db_session,
        campaign,
        [af.row(**{"Email": "not-an-address", "First Name": "Bad"})],
        "bad.csv",
    )
    db_session.flush()

    listed = ImportLineageReader(db_session).unresolved_rows()
    assert [row.outcome for row in listed] == [ImportRowOutcome.REJECTED.value]
    assert listed[0].needs_attention is True


# ---------------------------------------------------------------------------
# D-6 / D-14. Every outcome an operator sees has a truthful label.
# ---------------------------------------------------------------------------


def test_every_row_outcome_has_an_operator_label() -> None:
    assert {member.value for member in ImportRowOutcome} <= set(OUTCOME_LABELS)
    assert UNPROCESSED_OUTCOME in OUTCOME_LABELS
    # And no label is just the machine name handed back unchanged.
    assert all(label != key for key, label in OUTCOME_LABELS.items() if key != "suppressed")


def test_a_row_with_no_validation_is_reported_as_having_no_result(
    db_session: Session,
) -> None:
    campaign = af.make_campaign(db_session)
    result = _confirm(db_session, campaign, [af.row()], "a.csv")
    row_ids = [
        row.id
        for row in db_session.scalars(
            select(ImportRow).where(ImportRow.batch_id == result.batch_id)
        ).all()
    ]
    db_session.execute(
        ImportRowValidation.__table__.delete().where(ImportRowValidation.import_row_id.in_(row_ids))
    )
    db_session.flush()
    db_session.expire_all()

    rows, _total = ImportLineageReader(db_session).batch_rows(result.batch_id)
    assert [row.outcome for row in rows] == [UNPROCESSED_OUTCOME]
    assert rows[0].unprocessed is True
    assert rows[0].needs_attention is False
    assert rows[0].outcome_label == "no result recorded"


def test_a_processed_row_keeps_its_real_outcome(db_session: Session) -> None:
    campaign = af.make_campaign(db_session)
    result = _confirm(db_session, campaign, [af.row()], "a.csv")
    rows, _total = ImportLineageReader(db_session).batch_rows(result.batch_id)
    assert [row.outcome for row in rows] == [ImportRowOutcome.ACCEPTED.value]
    assert rows[0].imported is True


# ---------------------------------------------------------------------------
# D-7. The fingerprint covers everything the import persists as meaning.
# ---------------------------------------------------------------------------


def _fingerprint(**overrides: str) -> str:
    detection = apollo.detect_schema(af.APOLLO_HEADER)
    return apollo.row_fingerprint(apollo.read_row(af.row(**overrides), detection, row_number=1))


def test_an_unchanged_row_keeps_its_fingerprint() -> None:
    assert _fingerprint() == _fingerprint()


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("Email Status", "invalid"),
        ("Primary Email Last Verified At", "2026-07-04T09:30:00Z"),
        ("Primary Email Source", "Some Other Provider"),
        ("Primary Email Catch-all Status", "Catch-all"),
        ("Departments", "sales"),
        ("Company City", "Paris"),
        ("Annual Revenue", "1000000"),
    ],
)
def test_a_changed_vendor_claim_is_a_different_statement(column: str, value: str) -> None:
    assert _fingerprint() != _fingerprint(**{column: value})


def test_a_corrected_re_export_records_the_corrected_claim(db_session: Session) -> None:
    campaign = af.make_campaign(db_session)
    _confirm(db_session, campaign, [af.row(**{"Email Status": "invalid"})], "a.csv")
    second = _confirm(db_session, campaign, [af.row(**{"Email Status": "valid"})], "b.csv")

    assert second.imported + second.already_in_campaign + second.matched_existing >= 1
    claims = {
        record.provider_status_normalized
        for record in db_session.scalars(
            select(ImportedContactEmail).where(
                ImportedContactEmail.slot == ImportedEmailSlot.PRIMARY
            )
        ).all()
    }
    assert "valid" in claims


def test_position_in_the_file_is_not_part_of_the_statement() -> None:
    detection = apollo.detect_schema(af.APOLLO_HEADER)
    first = apollo.read_row(af.row(), detection, row_number=1, sheet_name="Sheet1")
    later = apollo.read_row(af.row(), detection, row_number=97, sheet_name="Elsewhere")
    assert apollo.row_fingerprint(first) == apollo.row_fingerprint(later)


def test_the_filename_is_not_part_of_the_statement(db_session: Session) -> None:
    campaign = af.make_campaign(db_session)
    _confirm(db_session, campaign, [af.row()], "export-january.csv")
    second = _confirm(db_session, campaign, [af.row(), af.row()], "export-february.csv")
    assert second.imported == 0


def test_case_only_differences_are_the_same_statement() -> None:
    assert _fingerprint() == _fingerprint(
        **{"First Name": "ADA", "Title": "HEAD OF ANALYTICAL ENGINES"}
    )


# ---------------------------------------------------------------------------
# D-8. A value that is formula-like once stored is flagged, and rendered inert.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    ["=cmd|x", "+cmd|x", "-cmd|x", "@SUM(A1)", " =cmd|x", "\t=cmd|x", " =cmd|x"],
)
def test_a_formula_is_recognized_whatever_whitespace_precedes_it(payload: str) -> None:
    assert apollo.looks_like_formula(payload) is True
    neutralized = apollo.neutralize_formula(payload)
    assert neutralized is not None and neutralized.startswith("'")


@pytest.mark.parametrize(
    "payload", ["-5", "+3.14", "-1.5e6", "Analytical Engines", "ada@x.example"]
)
def test_ordinary_values_are_left_alone(payload: str) -> None:
    assert apollo.neutralize_formula(payload) == payload


def test_a_leading_space_no_longer_hides_a_formula_from_the_operator(
    db_session: Session,
) -> None:
    campaign = af.make_campaign(db_session)
    _confirm(db_session, campaign, [af.row(**{"First Name": " =cmd|'/c calc'!A0"})], "f.csv")
    validation = db_session.scalars(select(ImportRowValidation)).one()
    codes = {entry.get("code") for entry in (validation.warnings or [])}
    assert "formula_like_cells" in codes


def test_the_stored_evidence_keeps_the_value_the_file_supplied(db_session: Session) -> None:
    """Neutralization is a rendering concern. What was imported has to stay
    auditable, or the operator cannot see what the file actually said."""

    campaign = af.make_campaign(db_session)
    _confirm(db_session, campaign, [af.row(**{"First Name": "=cmd|x"})], "f.csv")
    row = db_session.scalars(select(ImportRow)).one()
    assert row.raw_data["First Name"] == "=cmd|x"


def _formula_row() -> dict[str, str]:
    return af.row(
        **{
            "First Name": "=cmd|first",
            "Last Name": "=cmd|last",
            "Title": "=cmd|title",
            "Company Name": "=cmd|company",
            "Website": "=cmd|website",
            "Email Status": "=cmd|status",
            "Primary Email Source": "=cmd|source",
            "Primary Email Catch-all Status": "=cmd|catchall",
        }
    )


def _assert_no_live_formula(body: str) -> None:
    """No rendered field may begin with a formula character.

    Scans the text between tags rather than the markup, so an ordinary ``-``
    inside prose is not mistaken for one. This is the enforcement mechanism for
    the render-time invariant: adding a binding to one of these templates
    without the filter fails here rather than in production.
    """

    import re

    for chunk in re.split(r"<[^>]*>", body):
        for line in chunk.splitlines():
            stripped = line.strip()
            assert not stripped.startswith("=cmd|"), stripped


def test_the_preview_renders_no_live_formula(committed_session: Session, client: Any) -> None:
    campaign = af.make_campaign(committed_session)
    committed_session.commit()
    upload = client.post(
        f"/app/campaigns/{campaign.id}/imports",
        files={"file": ("=cmd|name.csv", af.csv_bytes([_formula_row()]), "text/csv")},
        follow_redirects=False,
    )
    assert upload.status_code in (302, 303)
    body = client.get(upload.headers["location"], follow_redirects=True).text
    assert "cmd|first" in body  # it is displayed...
    _assert_no_live_formula(body)  # ...but never as an expression


def test_the_batch_page_renders_no_live_formula(committed_session: Session, client: Any) -> None:
    campaign = af.make_campaign(committed_session)
    committed_session.commit()
    result = campaign_import.confirm(
        committed_session,
        campaign_id=campaign.id,
        content=af.csv_bytes([_formula_row()]),
        filename="=cmd|name.csv",
    )
    committed_session.commit()
    body = client.get(f"/app/campaigns/{campaign.id}/imports/{result.batch_id}").text
    assert "cmd|first" in body
    _assert_no_live_formula(body)


def test_the_admin_batch_page_renders_no_live_formula(
    committed_session: Session, client: Any
) -> None:
    campaign = af.make_campaign(committed_session)
    committed_session.commit()
    result = campaign_import.confirm(
        committed_session,
        campaign_id=campaign.id,
        content=af.csv_bytes([_formula_row()]),
        filename="=cmd|name.csv",
    )
    committed_session.commit()
    _assert_no_live_formula(client.get(f"/admin/imports/{result.batch_id}").text)


# ---------------------------------------------------------------------------
# D-9. An imported address is described as one, wherever it appears.
# ---------------------------------------------------------------------------


def test_the_verification_read_model_knows_an_imported_address(
    db_session: Session,
) -> None:
    from app.services.verification import status as vstatus

    campaign = af.make_campaign(db_session)
    _confirm(db_session, campaign, [af.row()], "a.csv")
    contact = db_session.scalars(select(Contact)).one()

    view = vstatus.derive_status_for_contact(db_session, contact)
    assert view.is_imported is True
    assert view.label == vstatus.IMPORTED_LABEL
    assert "pending" not in view.label
    assert "No verification provider was called" in view.explanation


def test_an_ordinary_unverified_contact_is_unchanged(db_session: Session) -> None:
    from app.services.verification import status as vstatus

    contact = Contact(
        first_name="Grace",
        last_name="Hopper",
        email="grace@navy.example",
        company_domain="navy.example",
    )
    db_session.add(contact)
    db_session.flush()

    view = vstatus.derive_status_for_contact(db_session, contact)
    assert view.is_imported is False
    assert view.label == "pending"


def test_provider_evidence_outranks_the_import_label(db_session: Session) -> None:
    """A mailbox somebody actually checked is a stronger fact than a
    spreadsheet cell, and the import label would hide it."""

    from app.models.enums import EmailVerificationResult
    from app.services.verification import status as vstatus

    campaign = af.make_campaign(db_session)
    _confirm(db_session, campaign, [af.row()], "a.csv")
    contact = db_session.scalars(select(Contact)).one()
    db_session.add(
        ExactEmailVerification(
            contact_id=contact.id,
            email=contact.email,
            result=EmailVerificationResult.VALID,
            provider="millionverifier",
            policy_version="test-policy/v1",
            checked_at=func.now(),
        )
    )
    db_session.flush()

    view = vstatus.derive_status_for_contact(db_session, contact)
    assert view.is_imported is False


def test_the_admin_contact_card_marks_the_address_as_imported(
    committed_session: Session, client: Any
) -> None:
    campaign = af.make_campaign(committed_session)
    committed_session.commit()
    _confirm(committed_session, campaign, [af.row()], "a.csv")
    contact = committed_session.scalars(select(Contact)).one()
    committed_session.commit()

    body = client.get(f"/admin/contacts/{contact.id}").text
    assert "no provider called" in body
    assert "imported (vendor-supplied)" in body


def test_campaign_context_does_not_change_a_contacts_own_address_story(
    db_session: Session,
) -> None:
    """The Contact page is not campaign-scoped, so its statement must hold for
    the address itself: this address was supplied, not checked. Enrolment in a
    second Campaign does not make it checked."""

    from app.services import campaign_contacts
    from app.services.verification import status as vstatus

    imported_campaign = af.make_campaign(db_session, name="imported")
    other_campaign = af.make_campaign(db_session, name="other")
    _confirm(db_session, imported_campaign, [af.row()], "a.csv")
    contact = db_session.scalars(select(Contact)).one()
    campaign_contacts.enrol_contact(
        db_session,
        campaign_id=other_campaign.id,
        contact_id=contact.id,
        source_type="manual",
        source_reference="not-an-import",
        enqueue=False,
    )
    db_session.flush()

    assert vstatus.derive_status_for_contact(db_session, contact).is_imported is True
    # ...and the campaign-scoped question still answers only for its Campaign.
    assert (
        campaign_import.accepted_primary_email(
            db_session, campaign_id=other_campaign.id, contact_id=contact.id
        )
        is None
    )


# ---------------------------------------------------------------------------
# D-10. The displayed outcome buckets partition the file.
# ---------------------------------------------------------------------------


def _counts(session: Session, batch_id: Any) -> Any:
    batch = session.get(ImportBatch, batch_id)
    assert batch is not None
    session.refresh(batch)
    return campaign_import.batch_counts(batch)


def test_a_straightforward_import_partitions(db_session: Session) -> None:
    campaign = af.make_campaign(db_session)
    result = _confirm(
        db_session,
        campaign,
        [af.row(), af.row(**{"Email": "grace@engines.example", "First Name": "Grace"})],
        "a.csv",
    )
    counts = _counts(db_session, result.batch_id)
    assert counts.imported == 2
    assert counts.partitions is True


def test_an_already_present_person_is_counted_once(db_session: Session) -> None:
    campaign = af.make_campaign(db_session)
    _confirm(db_session, campaign, [af.row()], "a.csv")
    second = _confirm(
        db_session, campaign, [af.row(**{"Title": "Director of Analytical Engines"})], "b.csv"
    )
    counts = _counts(db_session, second.batch_id)
    assert counts.already_in_campaign == 1
    assert counts.matched_or_duplicate == 0
    assert counts.partitions is True


@pytest.mark.parametrize(
    "rows",
    [
        [af.row(**{"Email": "not-an-address"})],
        [af.row(**{"First Name": "", "Last Name": ""})],
        [af.row(), af.row()],
    ],
)
def test_every_batch_partitions_whatever_the_rows_did(
    db_session: Session, rows: list[dict[str, str]]
) -> None:
    campaign = af.make_campaign(db_session)
    result = _confirm(db_session, campaign, rows, "a.csv")
    assert _counts(db_session, result.batch_id).partitions is True


def test_rows_staged_but_never_processed_are_shown_as_such(db_session: Session) -> None:
    campaign = af.make_campaign(db_session)
    result = _confirm(db_session, campaign, [af.row()], "a.csv")
    batch = db_session.get(ImportBatch, result.batch_id)
    assert batch is not None
    batch.total_rows = 3  # as an interrupted batch would leave it
    db_session.flush()

    counts = campaign_import.batch_counts(batch)
    assert counts.unprocessed == 2
    assert counts.partitions is True


# ---------------------------------------------------------------------------
# D-11 / D-12 / D-13. Bad input is refused with something actionable.
# ---------------------------------------------------------------------------


def test_a_cell_beyond_the_field_limit_is_a_typed_import_error() -> None:
    content = _csv(
        ("First Name", "Last Name", "Company Name", "Email", "Keywords"),
        [
            "Ada",
            "Lovelace",
            "Engines",
            "ada@engines.example",
            "x" * (parsing.MAX_CSV_FIELD_CHARS + 1),
        ],
    )
    with pytest.raises(campaign_import.CampaignImportError):
        campaign_import.inspect(content, "big.csv")


def test_an_ordinary_large_keywords_cell_still_imports(db_session: Session) -> None:
    """Apollo's Keywords column runs to kilobytes. Python's 128 KB default made
    that an unhandled crash; the limit is now explicit and generous."""

    campaign = af.make_campaign(db_session)
    result = _confirm(db_session, campaign, [af.row(**{"Keywords": "k" * 200_000})], "big.csv")
    assert result.imported == 1


@pytest.mark.parametrize("hostile", ["--5", "²", "abc", "", "  ", "1e3"])
def test_a_malformed_worksheet_selection_is_not_a_crash(hostile: str) -> None:
    from app.web.v2.routes import _sheet_index

    assert _sheet_index(hostile) is None


@pytest.mark.parametrize(("value", "expected"), [("0", 0), ("2", 2), (" 3 ", 3), ("-1", -1)])
def test_a_well_formed_worksheet_selection_is_read(value: str, expected: int) -> None:
    from app.web.v2.routes import _sheet_index

    assert _sheet_index(value) == expected


def test_an_overlong_address_is_refused_with_a_reason(db_session: Session) -> None:
    campaign = af.make_campaign(db_session)
    address = f"{'a' * 400}@engines.example"
    result = _confirm(
        db_session,
        campaign,
        [af.row(), af.row(**{"Email": address, "First Name": "Grace"})],
        "long.csv",
    )
    assert result.imported == 1  # the good row is unaffected
    assert result.failed == 1
    codes = {
        validation.error_code
        for validation in db_session.scalars(select(ImportRowValidation)).all()
        if validation.error_code
    }
    assert codes == {"email_too_long"}
    assert "database_error" not in codes


def test_an_overlong_vendor_claim_is_trimmed_rather_than_losing_the_row(
    db_session: Session,
) -> None:
    campaign = af.make_campaign(db_session)
    result = _confirm(
        db_session, campaign, [af.row(**{"Primary Email Source": "s" * 900})], "v.csv"
    )
    assert result.imported == 1
    record = db_session.scalars(select(ImportedContactEmail)).one()
    assert record.provider_source is not None
    assert len(record.provider_source) == campaign_import.MAX_PROVIDER_TEXT_CHARS


# ---------------------------------------------------------------------------
# D-15. Two spellings that render identically are one person.
# ---------------------------------------------------------------------------


def test_composed_and_decomposed_spellings_are_one_statement() -> None:
    detection = apollo.detect_schema(af.APOLLO_HEADER)
    nfc = apollo.read_row(af.row(**{"First Name": "José"}), detection, row_number=1)
    nfd = apollo.read_row(af.row(**{"First Name": "José"}), detection, row_number=1)
    assert nfc.first_name == nfd.first_name
    assert apollo.row_fingerprint(nfc) == apollo.row_fingerprint(nfd)


def test_a_zero_width_space_does_not_split_a_name() -> None:
    assert norm.normalize_name("Ac​me") == "Acme"


def test_meaningful_joiners_are_preserved() -> None:
    """The zero-width non-joiner is meaningful in several scripts. Removing it
    would change how a name is written, which is not normalization."""

    assert "‌" in (norm.normalize_name("عل‌ی") or "")


def test_an_opaque_vendor_identifier_is_compared_byte_for_byte() -> None:
    detection = apollo.detect_schema(af.APOLLO_HEADER)
    lower = apollo.read_row(af.row(**{"Apollo Contact Id": "abc"}), detection, row_number=1)
    upper = apollo.read_row(af.row(**{"Apollo Contact Id": "ABC"}), detection, row_number=1)
    assert apollo.row_fingerprint(lower) != apollo.row_fingerprint(upper)


# ---------------------------------------------------------------------------
# D-16. One accepted address per person per Campaign, at the database.
# ---------------------------------------------------------------------------


def test_a_second_accepted_primary_for_one_person_is_refused_by_the_database(
    db_session: Session,
) -> None:
    from sqlalchemy.exc import IntegrityError

    campaign = af.make_campaign(db_session)
    result = _confirm(db_session, campaign, [af.row()], "a.csv")
    contact = db_session.scalars(select(Contact)).one()
    original = db_session.scalars(select(ImportedContactEmail)).one()

    twin_row = ImportRow(
        batch_id=result.batch_id,
        row_number=99,
        sheet_index=0,
        raw_data={"Email": "other@engines.example"},
    )
    db_session.add(twin_row)
    db_session.flush()
    db_session.add(
        ImportedContactEmail(
            import_batch_id=original.import_batch_id,
            import_row_id=twin_row.id,
            campaign_id=campaign.id,
            contact_id=contact.id,
            slot=ImportedEmailSlot.PRIMARY,
            raw_email="other@engines.example",
            normalized_email="other@engines.example",
            source_row_number=99,
            source_file_checksum=original.source_file_checksum,
            source_schema=original.source_schema,
            row_fingerprint="a-different-row-fingerprint",
            email_stage_outcome=ImportedEmailStageOutcome.IMPORTED_EMAIL_ACCEPTED,
            verification_stage_outcome=original.verification_stage_outcome,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_a_refused_address_for_the_same_person_is_still_allowed(db_session: Session) -> None:
    """The constraint is on ACCEPTED records only. A refused row must still be
    able to leave its evidence, or an operator cannot see what the file said."""

    campaign = af.make_campaign(db_session)
    result = _confirm(db_session, campaign, [af.row()], "a.csv")
    contact = db_session.scalars(select(Contact)).one()
    original = db_session.scalars(select(ImportedContactEmail)).one()

    other_row = ImportRow(
        batch_id=result.batch_id, row_number=98, sheet_index=0, raw_data={"Email": "x"}
    )
    db_session.add(other_row)
    db_session.flush()
    db_session.add(
        ImportedContactEmail(
            import_batch_id=original.import_batch_id,
            import_row_id=other_row.id,
            campaign_id=campaign.id,
            contact_id=contact.id,
            slot=ImportedEmailSlot.PRIMARY,
            raw_email="broken",
            normalized_email=None,
            source_row_number=98,
            source_file_checksum=original.source_file_checksum,
            source_schema=original.source_schema,
            row_fingerprint="another-fingerprint",
            email_stage_outcome=ImportedEmailStageOutcome.IMPORTED_EMAIL_REJECTED,
            rejection_code="email_malformed",
        )
    )
    db_session.flush()  # must not raise


# ---------------------------------------------------------------------------
# D-17 / D-19. Provenance is recorded, never invented; scope is not disclosed.
# ---------------------------------------------------------------------------


def test_a_genuine_apollo_header_is_recorded_as_an_apollo_export() -> None:
    detection = apollo.detect_schema(af.APOLLO_HEADER)
    assert detection.is_apollo_export is True
    assert campaign_import.source_name_for(detection) == campaign_import.APOLLO_SOURCE_NAME


def test_a_bare_compatible_file_is_not_recorded_as_a_vendor_export(
    db_session: Session,
) -> None:
    detection = apollo.detect_schema(af.MINIMAL_HEADER)
    assert detection.recognized is True
    assert detection.is_apollo_export is False
    assert campaign_import.source_name_for(detection) == (
        campaign_import.APOLLO_COMPATIBLE_SOURCE_NAME
    )

    campaign = af.make_campaign(db_session)
    result = campaign_import.confirm(
        db_session,
        campaign_id=campaign.id,
        content=af.csv_bytes([af.row()], header=af.MINIMAL_HEADER),
        filename="hand-made.csv",
    )
    batch = db_session.get(ImportBatch, result.batch_id)
    assert batch is not None
    assert batch.source_name == campaign_import.APOLLO_COMPATIBLE_SOURCE_NAME


def test_the_duplicate_file_note_does_not_name_the_other_campaign(
    db_session: Session,
) -> None:
    first = af.make_campaign(db_session, name="Confidential prospect list")
    second = af.make_campaign(db_session, name="Second")
    content = af.csv_bytes([af.row()])
    campaign_import.confirm(db_session, campaign_id=first.id, content=content, filename="a.csv")

    note = campaign_import.preview(
        db_session, campaign_id=second.id, content=content, filename="a.csv"
    ).duplicate_file
    assert note is not None
    assert note.code == "imported_into_another_campaign"
    assert first.name not in note.message
    assert note.batch_id is None


# ---------------------------------------------------------------------------
# Phase 14 — the properties the review confirmed, asserted here so a later
# change cannot quietly take them away.
# ---------------------------------------------------------------------------


def test_an_import_still_fabricates_no_candidate_and_no_verification(
    db_session: Session,
) -> None:
    campaign = af.make_campaign(db_session, execution=True)
    _confirm(db_session, campaign, [af.row()], "a.csv")
    _run_pipeline(db_session)

    assert db_session.scalar(select(func.count()).select_from(EmailCandidate)) == 0
    assert db_session.scalar(select(func.count()).select_from(ExactEmailVerification)) == 0


def test_verification_decision_keeps_exactly_its_five_members() -> None:
    from app.services.verification.decisions import VerificationDecision

    assert {member.name for member in VerificationDecision} == {
        "ACCEPT",
        "TRY_NEXT_CANDIDATE",
        "RETRY_LATER",
        "STOP_NO_RESULT",
        "REFUSED",
    }


def test_the_import_modules_still_name_no_verification_provider() -> None:
    """No executable name in the import path refers to a verification provider.

    Tokenized rather than searched as text, and comments and string literals are
    excluded, because these modules legitimately say "not MillionVerifier, not
    ZeroBounce" in their own disclaimers. A naive substring scan would fail on
    the very sentence that states the guarantee — the review nearly filed a
    false defect on exactly this.
    """

    import io
    import tokenize

    banned = {"millionverifier", "zerobounce", "neverbounce", "hunter"}
    for module in (
        "app/services/imports/campaign_import.py",
        "app/services/imports/apollo.py",
        "app/services/imports/contact_resolution.py",
        "app/services/imports/company_resolution.py",
    ):
        source = Path(module).read_text()
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type in (tokenize.STRING, tokenize.COMMENT):
                continue
            assert token.string.lower() not in banned, f"{module}: {token.string}"


def test_campaign_scoping_still_refuses_another_campaigns_batch(
    committed_session: Session, client: Any
) -> None:
    mine = af.make_campaign(committed_session)
    theirs = af.make_campaign(committed_session)
    committed_session.commit()
    result = _confirm(committed_session, theirs, [af.row()], "a.csv")
    committed_session.commit()

    assert client.get(f"/app/campaigns/{mine.id}/imports/{result.batch_id}").status_code == 404


def test_one_bad_row_still_costs_exactly_one_row(db_session: Session) -> None:
    campaign = af.make_campaign(db_session)
    result = _confirm(
        db_session,
        campaign,
        [
            af.row(),
            af.row(**{"Email": "not-an-address", "First Name": "Bad"}),
            af.row(**{"Email": "grace@engines.example", "First Name": "Grace"}),
        ],
        "mixed.csv",
    )
    assert result.imported == 2
    assert result.failed == 1
    assert db_session.scalar(select(func.count()).select_from(Contact)) == 2


def test_reading_a_batchs_rows_still_costs_a_fixed_number_of_statements(
    db_session: Session,
) -> None:
    from sqlalchemy import event

    campaign = af.make_campaign(db_session)
    rows = [
        af.row(**{"Email": f"p{index}@engines.example", "First Name": f"P{index}"})
        for index in range(12)
    ]
    result = _confirm(db_session, campaign, rows, "many.csv")

    statements: list[str] = []

    def _record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        statements.append(statement)

    event.listen(db_session.get_bind(), "before_cursor_execute", _record)
    try:
        campaign_import.batch_rows(db_session, batch_id=result.batch_id, limit=100, offset=0)
    finally:
        event.remove(db_session.get_bind(), "before_cursor_execute", _record)

    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) <= 6, selects
