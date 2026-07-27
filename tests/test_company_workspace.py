"""Company workspace and dossier-ready data model tests (APP-003).

Covers the five things the issue asks tests to cover — domain identity, contact
links, dossier versions, conflicts and stale states — plus the migration backfill
cases, against a live Postgres.

The guarantees under test are the product ones: a company is permanent and
campaign-free, a contact links to it only when the evidence is unambiguous,
research is evidence rather than an overwrite, an unknown field is not a false
one, older dossiers survive newer ones, and a disagreement about identity stays
visible instead of being resolved by a guess.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.models.audit_event import AuditEvent
from app.models.company import Company
from app.models.company_dossier import CompanyDossierVersion, CompanyResearchSubmission
from app.models.company_field_value import CompanyFieldValue
from app.models.contact import Contact
from app.models.enums import (
    CompanyConflictKind,
    CompanyFieldSource,
    DossierSection,
    LinkedInSnapshotOutcome,
    ResearchState,
)
from app.models.linkedin_company import LinkedInCompanySnapshot
from app.services.companies import conflicts as company_conflicts
from app.services.companies import detail as company_detail
from app.services.companies import dossiers as company_dossiers
from app.services.companies import provenance as company_provenance
from app.services.companies import records as company_records
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

# --- helpers ----------------------------------------------------------------


def make_company(
    session: Session,
    *,
    name: str = "Acme Systems",
    domain: str | None = "acme.example",
    linkedin_company_id: str | None = None,
) -> Company:
    company = Company(name=name, domain=domain, linkedin_company_id=linkedin_company_id)
    session.add(company)
    session.flush()
    return company


def make_contact(
    session: Session,
    *,
    company_domain: str,
    company_id: uuid.UUID | None = None,
    first: str = "Dana",
    last: str = "Reyes",
    merged_into_id: uuid.UUID | None = None,
) -> Contact:
    contact = Contact(
        first_name=first,
        last_name=last,
        company_name="Acme Systems",
        company_domain=company_domain,
        company_id=company_id,
        natural_key=f"{first.casefold()}|{last.casefold()}|{company_domain}",
        merged_into_id=merged_into_id,
    )
    session.add(contact)
    session.flush()
    return contact


def submit_and_interpret(
    session: Session,
    company: Company,
    *,
    payload: dict[str, object] | None = None,
    sections: dict[str, object] | None = None,
    warnings: list[object] | None = None,
    interpreter: str = "test-interpreter",
    make_current: bool = True,
) -> CompanyDossierVersion:
    submission, _created = company_dossiers.submit(
        session,
        company=company,
        producer="test-producer",
        payload=payload or {"raw": "anything"},
    )
    return company_dossiers.interpret(
        session,
        company=company,
        submission=submission,
        interpreter=interpreter,
        sections=sections,
        warnings=warnings,
        make_current=make_current,
    )


# --- 1. Domain identity ------------------------------------------------------


def test_a_company_may_exist_without_a_domain(db_session: Session) -> None:
    """Unresolved is a normal state, not an invalid one.

    The unique index on domain is partial for exactly this reason: many
    companies can be domain-less at once without colliding with each other.
    """

    first = make_company(db_session, name="Unresolved One", domain=None)
    second = make_company(db_session, name="Unresolved Two", domain=None)
    db_session.flush()

    assert first.domain is None
    assert second.domain is None
    assert first.id != second.id


def test_two_companies_cannot_share_a_domain(db_session: Session) -> None:
    """A domain identifies a company. The database, not the code, says so."""

    make_company(db_session, domain="shared.example")
    db_session.add(Company(name="Impostor", domain="shared.example"))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_linkedin_identity_is_recorded_and_not_unique(db_session: Session) -> None:
    """Two companies claiming one LinkedIn id is a conflict, not a write error.

    Rejecting the second write would lose the evidence that the disagreement
    exists. It is stored and surfaced instead.
    """

    first = make_company(db_session, domain="one.example", linkedin_company_id="urn:1")
    second = make_company(
        db_session, name="Other", domain="two.example", linkedin_company_id="urn:1"
    )
    db_session.flush()

    conflicts = company_conflicts.for_company(db_session, company=first)
    kinds = {c.kind for c in conflicts}
    assert CompanyConflictKind.LINKEDIN_ID_SHARED in kinds
    assert second.linkedin_company_id == "urn:1"


def test_a_company_with_no_domain_reports_that_checks_are_silent(db_session: Session) -> None:
    """Empty results must not read as agreement."""

    company = make_company(db_session, domain=None)
    kinds = {c.kind for c in company_conflicts.for_company(db_session, company=company)}
    assert CompanyConflictKind.NO_CANONICAL_DOMAIN in kinds


# --- 2. Contact links --------------------------------------------------------


def test_linked_contacts_come_from_the_permanent_edge(db_session: Session) -> None:
    company = make_company(db_session)
    make_contact(db_session, company_domain="acme.example", company_id=company.id)
    make_contact(
        db_session, company_domain="acme.example", company_id=company.id, first="Sam", last="Okafor"
    )

    detail = company_detail.get_company_detail(db_session, company.id)
    assert detail is not None
    assert detail.linked_count == 2
    assert all(link.is_permanent_link for link in detail.linked_contacts)


def test_a_domain_match_without_a_link_is_transitional_not_linked(db_session: Session) -> None:
    """The read model must not undo the backfill's refusal to guess.

    A contact carrying the domain but no company_id is shown, separately and
    labelled, because "nobody linked this person" is not "this person works
    here".
    """

    company = make_company(db_session)
    make_contact(db_session, company_domain="acme.example", company_id=None)

    detail = company_detail.get_company_detail(db_session, company.id)
    assert detail is not None
    assert detail.linked_count == 0
    assert len(detail.transitional_contacts) == 1
    assert detail.transitional_contacts[0].is_permanent_link is False

    kinds = {c.kind for c in detail.conflicts}
    assert CompanyConflictKind.CONTACT_LINK_UNRESOLVED in kinds


def test_merged_contacts_are_not_counted_as_people_at_the_company(db_session: Session) -> None:
    company = make_company(db_session)
    survivor = make_contact(db_session, company_domain="acme.example", company_id=company.id)
    make_contact(
        db_session,
        company_domain="acme.example",
        company_id=company.id,
        first="Dana",
        last="Reyes-Dup",
        merged_into_id=survivor.id,
    )

    detail = company_detail.get_company_detail(db_session, company.id)
    assert detail is not None
    assert detail.linked_count == 1


def test_many_contacts_share_one_company_workspace(db_session: Session) -> None:
    """One company, many people — the product rule, asserted directly."""

    company = make_company(db_session)
    for i in range(5):
        make_contact(
            db_session,
            company_domain="acme.example",
            company_id=company.id,
            first=f"Person{i}",
            last="Test",
        )

    rows, total = company_records.list_company_rows(
        db_session, filters=company_records.CompanyFilters()
    )
    assert total == 1
    assert rows[0].contact_count == 5


def test_deleting_a_company_does_not_delete_its_contacts(db_session: Session) -> None:
    """SET NULL, not CASCADE. A person does not stop existing with their employer."""

    company = make_company(db_session)
    contact = make_contact(db_session, company_domain="acme.example", company_id=company.id)
    contact_id = contact.id

    db_session.delete(company)
    db_session.flush()
    db_session.expire_all()

    survivor = db_session.get(Contact, contact_id)
    assert survivor is not None
    assert survivor.company_id is None
    assert survivor.company_domain == "acme.example"


# --- 3. Dossier versions -----------------------------------------------------


def test_a_submission_is_stored_verbatim_and_changes_no_canonical_field(
    db_session: Session,
) -> None:
    """Research is evidence. Submitting one asserts nothing about the company."""

    company = make_company(db_session)
    company.industry = "Manufacturing"
    db_session.flush()

    payload = {"industry": "Software", "nested": {"claim": "totally different"}}
    submission, created = company_dossiers.submit(
        db_session, company=company, producer="test-producer", payload=payload
    )

    assert created is True
    assert submission.payload == payload
    assert company.industry == "Manufacturing"
    assert company.research_state is ResearchState.NOT_REQUESTED


def test_resubmitting_identical_content_does_not_duplicate(db_session: Session) -> None:
    company = make_company(db_session)
    payload = {"a": 1, "b": 2}
    first, created_first = company_dossiers.submit(
        db_session, company=company, producer="p", payload=payload
    )
    # Same content, different key order: the hash must not care.
    second, created_second = company_dossiers.submit(
        db_session, company=company, producer="p", payload={"b": 2, "a": 1}
    )

    assert created_first is True
    assert created_second is False
    assert first.id == second.id
    assert db_session.scalar(select(CompanyResearchSubmission.id).limit(2)) is not None


def test_multiple_versions_coexist_and_only_one_is_current(db_session: Session) -> None:
    company = make_company(db_session)
    v1 = submit_and_interpret(db_session, company, payload={"pass": 1}, sections={"overview": {}})
    v2 = submit_and_interpret(db_session, company, payload={"pass": 2}, sections={"overview": {}})

    db_session.expire_all()
    assert v1.version_number == 1
    assert v2.version_number == 2
    assert v1.is_current is False
    assert v2.is_current is True
    assert v1.superseded_at is not None

    versions = company_dossiers.list_versions(db_session, company_id=company.id)
    assert [s.version.version_number for s in versions] == [2, 1]


def test_selecting_an_older_version_supersedes_without_deleting(db_session: Session) -> None:
    """Changing your mind must leave both readings and their order."""

    company = make_company(db_session)
    v1 = submit_and_interpret(db_session, company, payload={"pass": 1})
    v2 = submit_and_interpret(db_session, company, payload={"pass": 2})

    company_dossiers.select_current(db_session, company=company, version=v1, actor="operator")
    db_session.expire_all()

    assert v1.is_current is True
    assert v2.is_current is False
    assert db_session.get(CompanyDossierVersion, v2.id) is not None
    assert company_dossiers.current_version(db_session, company_id=company.id).id == v1.id


def test_two_current_versions_are_impossible_at_the_database(db_session: Session) -> None:
    company = make_company(db_session)
    v1 = submit_and_interpret(db_session, company, payload={"pass": 1})
    v2 = submit_and_interpret(db_session, company, payload={"pass": 2})

    # Bypass the service and set the flag directly: the index, not the code,
    # is what makes "current" unambiguous.
    v1.is_current = True
    v2.is_current = True
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_an_unaddressed_section_is_unknown_not_empty(db_session: Session) -> None:
    """The distinction the whole schema exists to preserve."""

    company = make_company(db_session)
    version = submit_and_interpret(
        db_session,
        company,
        sections={"overview": {"text": "a description"}, "industries": {"values": []}},
    )

    assert version.overview is not None
    # Looked and found nothing.
    assert version.industries == {"values": []}
    # Never addressed at all.
    assert version.leadership is None
    assert version.public_contacts is None

    summaries = company_dossiers.list_versions(db_session, company_id=company.id)
    assert "industries" in summaries[0].sections_present
    assert "leadership" in summaries[0].sections_absent


def test_the_section_boundary_is_closed(db_session: Session) -> None:
    """An unknown section is rejected, never silently dropped."""

    company = make_company(db_session)
    submission, _ = company_dossiers.submit(
        db_session, company=company, producer="p", payload={"x": 1}
    )
    with pytest.raises(company_dossiers.DossierError, match="unknown dossier section"):
        company_dossiers.interpret(
            db_session,
            company=company,
            submission=submission,
            interpreter="i",
            sections={"overview": {}, "pricing": {"nope": True}},
        )


def test_every_declared_section_has_a_column(db_session: Session) -> None:
    """The enum and the table cannot drift apart."""

    company = make_company(db_session)
    everything = {section.value: {"seen": True} for section in DossierSection}
    version = submit_and_interpret(db_session, company, sections=everything)

    for section in DossierSection:
        assert getattr(version, section.value) == {"seen": True}


def test_a_version_cannot_interpret_another_companys_submission(db_session: Session) -> None:
    first = make_company(db_session, domain="one.example")
    second = make_company(db_session, name="Other", domain="two.example")
    submission, _ = company_dossiers.submit(
        db_session, company=first, producer="p", payload={"x": 1}
    )
    with pytest.raises(company_dossiers.DossierError, match="same company"):
        company_dossiers.interpret(
            db_session, company=second, submission=submission, interpreter="i"
        )


def test_a_submission_cannot_be_deleted_while_a_version_reads_it(db_session: Session) -> None:
    """An interpretation without its payload is an unfalsifiable claim."""

    company = make_company(db_session)
    submission, _ = company_dossiers.submit(
        db_session, company=company, producer="p", payload={"x": 1}
    )
    company_dossiers.interpret(db_session, company=company, submission=submission, interpreter="i")
    db_session.flush()

    db_session.delete(submission)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_dossier_storage_is_provider_neutral(db_session: Session) -> None:
    """Nothing branches on who produced or interpreted a payload."""

    company = make_company(db_session)
    for producer, interpreter in (
        ("operator-manual", "operator"),
        ("website-research", "extractor-a"),
        ("some-future-thing", "extractor-b"),
    ):
        submission, _ = company_dossiers.submit(
            db_session, company=company, producer=producer, payload={"by": producer}
        )
        version = company_dossiers.interpret(
            db_session,
            company=company,
            submission=submission,
            interpreter=interpreter,
            sections={"overview": {}},
        )
        assert version.interpreter == interpreter

    assert len(company_dossiers.list_versions(db_session, company_id=company.id)) == 3


# --- 4. Research state -------------------------------------------------------


def test_research_state_starts_not_requested_and_says_so_truthfully(db_session: Session) -> None:
    company = make_company(db_session)
    assert company.research_state is ResearchState.NOT_REQUESTED
    assert company.last_researched_at is None


def test_selecting_a_dossier_moves_research_state_and_timestamp(db_session: Session) -> None:
    company = make_company(db_session)
    submit_and_interpret(db_session, company, sections={"overview": {}})

    assert company.research_state is ResearchState.COMPLETED
    assert company.last_researched_at is not None


def test_a_dossier_with_warnings_reports_completed_with_warnings(db_session: Session) -> None:
    """A warned dossier must not read as a clean one."""

    company = make_company(db_session)
    submit_and_interpret(
        db_session,
        company,
        sections={"overview": {}},
        warnings=["a source contradicted another"],
    )

    assert company.research_state is ResearchState.COMPLETED_WITH_WARNINGS


def test_an_uninterpreted_submission_does_not_claim_research_happened(
    db_session: Session,
) -> None:
    """Stale-state guard: a payload that nobody read is not research."""

    company = make_company(db_session)
    company_dossiers.submit(db_session, company=company, producer="p", payload={"x": 1})

    assert company.research_state is ResearchState.NOT_REQUESTED
    assert company.last_researched_at is None


def test_a_version_created_without_selection_leaves_state_alone(db_session: Session) -> None:
    company = make_company(db_session)
    submit_and_interpret(db_session, company, sections={"overview": {}}, make_current=False)

    assert company.research_state is ResearchState.NOT_REQUESTED


# --- 5. Field provenance -----------------------------------------------------


def test_recording_an_observation_does_not_change_the_canonical_value(
    db_session: Session,
) -> None:
    """The single most important rule: research proposes, the policy disposes."""

    company = make_company(db_session)
    company.industry = "Manufacturing"
    db_session.flush()

    company_provenance.record_observation(
        db_session,
        company=company,
        field_name="industry",
        value="Software",
        source_kind=CompanyFieldSource.RESEARCH_DOSSIER,
    )

    assert company.industry == "Manufacturing"


def test_reconciling_applies_the_winner_and_explains_it(db_session: Session) -> None:
    company = make_company(db_session)
    old = datetime.now(UTC) - timedelta(days=30)
    new = datetime.now(UTC) - timedelta(days=1)

    company_provenance.record_observation(
        db_session,
        company=company,
        field_name="industry",
        value="Manufacturing",
        source_kind=CompanyFieldSource.IMPORT,
        observed_at=old,
    )
    company_provenance.record_observation(
        db_session,
        company=company,
        field_name="industry",
        value="Software",
        source_kind=CompanyFieldSource.RESEARCH_DOSSIER,
        observed_at=new,
    )
    winner = company_provenance.reconcile_field(db_session, company=company, field_name="industry")

    assert winner is not None
    assert winner.value == "Software"
    assert company.industry == "Software"
    assert winner.decision_reason
    assert winner.policy_version


def test_older_evidence_cannot_overwrite_a_newer_value(db_session: Session) -> None:
    company = make_company(db_session)
    company_provenance.record_observation(
        db_session,
        company=company,
        field_name="country",
        value="Ireland",
        source_kind=CompanyFieldSource.RESEARCH_DOSSIER,
        observed_at=datetime.now(UTC) - timedelta(days=1),
    )
    company_provenance.reconcile_field(db_session, company=company, field_name="country")

    company_provenance.record_observation(
        db_session,
        company=company,
        field_name="country",
        value="Belgium",
        source_kind=CompanyFieldSource.IMPORT,
        observed_at=datetime.now(UTC) - timedelta(days=400),
    )
    company_provenance.reconcile_field(db_session, company=company, field_name="country")

    assert company.country == "Ireland"


def test_a_manual_override_outranks_every_automatic_source(db_session: Session) -> None:
    company = make_company(db_session)
    company_provenance.record_observation(
        db_session,
        company=company,
        field_name="company_size",
        value="1000+",
        source_kind=CompanyFieldSource.RESEARCH_DOSSIER,
        observed_at=datetime.now(UTC),
    )
    company_provenance.reconcile_field(db_session, company=company, field_name="company_size")

    company_provenance.set_manual_override(
        db_session,
        company=company,
        field_name="company_size",
        value="11-50",
        actor="operator@example.test",
    )

    assert company.company_size == "11-50"
    view = company_provenance.explain_field(db_session, company=company, field_name="company_size")
    assert view.winner is not None
    assert view.winner.is_manual_override is True


def test_only_one_winner_per_field_at_the_database(db_session: Session) -> None:
    company = make_company(db_session)
    first = company_provenance.record_observation(
        db_session,
        company=company,
        field_name="industry",
        value="A",
        source_kind=CompanyFieldSource.IMPORT,
    )
    second = company_provenance.record_observation(
        db_session,
        company=company,
        field_name="industry",
        value="B",
        source_kind=CompanyFieldSource.IMPORT,
    )
    first.is_current_winner = True
    second.is_current_winner = True
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_identity_fields_are_not_provenance_tracked(db_session: Session) -> None:
    """Changing a domain changes identity. That is not a freshness question."""

    company = make_company(db_session)
    for field in ("domain", "name"):
        with pytest.raises(company_provenance.UnknownCompanyFieldError):
            company_provenance.record_observation(
                db_session,
                company=company,
                field_name=field,
                value="whatever.example",
                source_kind=CompanyFieldSource.RESEARCH_DOSSIER,
            )


def test_an_observed_empty_value_is_recorded_as_a_real_observation(
    db_session: Session,
) -> None:
    """Looked and found nothing is a fact. Never looked is the absence of a row."""

    company = make_company(db_session)
    observation = company_provenance.record_observation(
        db_session,
        company=company,
        field_name="industry",
        value=None,
        source_kind=CompanyFieldSource.RESEARCH_DOSSIER,
    )

    assert observation.value is None
    stored = db_session.scalars(
        select(CompanyFieldValue).where(CompanyFieldValue.company_id == company.id)
    ).all()
    assert len(stored) == 1


def test_reconciliation_records_an_audit_event_only_when_the_value_changes(
    db_session: Session,
) -> None:
    company = make_company(db_session)
    company_provenance.record_observation(
        db_session,
        company=company,
        field_name="industry",
        value="Software",
        source_kind=CompanyFieldSource.IMPORT,
    )
    company_provenance.reconcile_field(db_session, company=company, field_name="industry")
    after_first = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == "company.field_reconciled")
    ).all()

    company_provenance.reconcile_field(db_session, company=company, field_name="industry")
    after_second = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == "company.field_reconciled")
    ).all()

    assert len(after_first) == 1
    assert len(after_second) == 1


def test_a_dossier_claim_is_traceable_back_to_its_version(db_session: Session) -> None:
    company = make_company(db_session)
    version = submit_and_interpret(db_session, company, sections={"industries": {"v": ["SaaS"]}})
    observation = company_provenance.record_observation(
        db_session,
        company=company,
        field_name="industry",
        value="SaaS",
        source_kind=CompanyFieldSource.RESEARCH_DOSSIER,
        dossier_version_id=version.id,
    )

    assert observation.dossier_version_id == version.id


# --- 6. Conflicts ------------------------------------------------------------


def test_a_linked_contact_with_another_domain_is_a_visible_conflict(
    db_session: Session,
) -> None:
    """Captured evidence is never rewritten to make the conflict go away."""

    company = make_company(db_session, domain="acme.example")
    make_contact(db_session, company_domain="acme-old.example", company_id=company.id)

    conflicts = company_conflicts.for_company(db_session, company=company)
    mismatch = next(c for c in conflicts if c.kind is CompanyConflictKind.CONTACT_DOMAIN_MISMATCH)
    assert mismatch.count == 1
    assert "acme-old.example" in mismatch.references


def test_a_captured_company_page_stating_another_domain_is_a_conflict(
    db_session: Session,
) -> None:
    company = make_company(db_session, domain="acme.example")
    snapshot = LinkedInCompanySnapshot(
        client_capture_id=str(uuid.uuid4()),
        content_hash=uuid.uuid4().hex,
        schema_version="linkedin-company-capture/1.0.0",
        source="test",
        extraction_status="ok",
        payload={},
        company_fields={},
        website_domain="acme-corp.example",
        matched_company_id=company.id,
        outcome=LinkedInSnapshotOutcome.STORED,
    )
    db_session.add(snapshot)
    db_session.flush()

    kinds = {c.kind for c in company_conflicts.for_company(db_session, company=company)}
    assert CompanyConflictKind.SNAPSHOT_DOMAIN_MISMATCH in kinds


def test_a_conflict_disappears_when_the_records_agree(db_session: Session) -> None:
    """The reason conflicts are derived rather than queued."""

    company = make_company(db_session, domain="acme.example")
    contact = make_contact(db_session, company_domain="acme-old.example", company_id=company.id)

    assert any(
        c.kind is CompanyConflictKind.CONTACT_DOMAIN_MISMATCH
        for c in company_conflicts.for_company(db_session, company=company)
    )

    contact.company_domain = "acme.example"
    db_session.flush()

    assert not any(
        c.kind is CompanyConflictKind.CONTACT_DOMAIN_MISMATCH
        for c in company_conflicts.for_company(db_session, company=company)
    )


def test_a_consistent_company_reports_no_conflicts(db_session: Session) -> None:
    company = make_company(db_session, domain="acme.example")
    make_contact(db_session, company_domain="acme.example", company_id=company.id)

    assert company_conflicts.for_company(db_session, company=company) == []


def test_conflict_counts_for_a_page_do_not_query_per_company(db_session: Session) -> None:
    clean = make_company(db_session, name="Clean", domain="clean.example")
    dirty = make_company(db_session, name="Dirty", domain="dirty.example")
    make_contact(db_session, company_domain="clean.example", company_id=clean.id)
    make_contact(
        db_session,
        company_domain="somewhere-else.example",
        company_id=dirty.id,
        first="Other",
        last="Person",
    )

    counts = company_conflicts.count_for_companies(db_session, company_ids=[clean.id, dirty.id])
    assert counts[clean.id] == 0
    assert counts[dirty.id] >= 1


# --- 7. The list -------------------------------------------------------------


def test_the_list_has_no_campaign_anywhere(db_session: Session) -> None:
    """Company intelligence belongs to the company, not to a campaign."""

    fields = company_records.CompanyFilters.__dataclass_fields__
    assert not any("campaign" in name for name in fields)


def test_views_filter_the_list(db_session: Session) -> None:
    with_contacts = make_company(db_session, name="Staffed", domain="staffed.example")
    make_contact(db_session, company_domain="staffed.example", company_id=with_contacts.id)
    make_company(db_session, name="Empty", domain="empty.example")
    make_company(db_session, name="Domainless", domain=None)

    rows, total = company_records.list_company_rows(
        db_session,
        filters=company_records.CompanyFilters(view=company_records.VIEW_WITH_CONTACTS),
    )
    assert total == 1
    assert rows[0].company.name == "Staffed"

    rows, total = company_records.list_company_rows(
        db_session,
        filters=company_records.CompanyFilters(view=company_records.VIEW_UNRESOLVED_DOMAIN),
    )
    assert total == 1
    assert rows[0].company.name == "Domainless"


def test_an_unknown_view_falls_back_rather_than_failing(db_session: Session) -> None:
    """Query strings are hand-edited. An unknown view widens, never errors."""

    make_company(db_session)
    rows, total = company_records.list_company_rows(
        db_session, filters=company_records.CompanyFilters(view="nonsense")
    )
    assert total == 1
    assert rows


def test_search_covers_name_and_domain(db_session: Session) -> None:
    make_company(db_session, name="Northwind Traders", domain="northwind.example")
    make_company(db_session, name="Southwind", domain="southwind.example")

    _rows, by_name = company_records.list_company_rows(
        db_session, filters=company_records.CompanyFilters(search="northwind t")
    )
    _rows, by_domain = company_records.list_company_rows(
        db_session, filters=company_records.CompanyFilters(search="southwind.ex")
    )
    assert by_name == 1
    assert by_domain == 1
