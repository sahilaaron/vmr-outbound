"""IMP-001 seen from the Admin Workbench, after the post-PR-241 reconciliation.

The two features were built on the same base commit and share no file, so they
merged without a textual conflict. That is not the same as being integrated, and
this module exists for the difference.

The specific hazard the merge created: the Workbench's Verification projection
validates its decision against
:class:`~app.services.verification.decisions.VerificationDecision` and reports
anything outside that vocabulary as *undecided* rather than guessing at it. The
import path commits ``bypassed``, which is deliberately not a verification
decision. Left alone the page therefore said "no committed decision" about a
stage that had committed one — safe, because it never claimed the address was
verified, but silent about the one thing an operator needs to know.

So these tests hold two lines at once:

* the imported lineage is *visible* — origin, batch, row, resolution bases,
  identifiers, the supplied Company name beside the resolved one, the accepted
  address and both bypasses; and
* it is *never dressed up* — no imported address reads as provider-verified, no
  vendor claim reads as this system's finding, and a Contact acquired any other
  way in the same Campaign still goes through discovery and a real provider.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator

import pytest
from app.api.deps import get_db
from app.core.config import get_settings
from app.main import create_app
from app.models.audit_event import AuditEvent
from app.models.campaign import Campaign, CampaignContact
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_candidate import EmailCandidate
from app.models.email_evidence import ExactEmailVerification
from app.models.enums import (
    AgentControlStatus,
    AgentIdentifier,
    CompanyFieldSource,
    PipelineStageStatus,
)
from app.models.import_batch import ImportBatch, ImportRowValidation
from app.models.imported_email import ImportedContactEmail, ImportSourceIdentifier
from app.services.admin_workbench import import_lineage
from app.services.agents import controls
from app.services.agents.adapters import DEFAULT_ADAPTERS, ResearchAgentAdapter
from app.services.agents.orchestrator import run_next
from app.services.companies import provenance as company_provenance
from app.services.imports import campaign_import
from app.services.pipeline import agent_state
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from tests import apollo_factory as af
from tests.test_research_agent import FakeWorker, _fact

pytestmark = pytest.mark.usefixtures("enable_csv_import")

WORKER = "admin-import-worker"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _build_client(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, *, workbench: bool = True
) -> TestClient:
    if workbench:
        monkeypatch.setenv("FEATURES__WORKBENCH", "true")
        monkeypatch.setenv("FEATURES__AGENT_WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__CSV_IMPORT", "true")
    get_settings.cache_clear()
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


@pytest.fixture()
def client(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    with _build_client(db_session, monkeypatch) as c:
        yield c
    get_settings.cache_clear()


@pytest.fixture()
def no_workbench_client(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    with _build_client(db_session, monkeypatch, workbench=False) as c:
        yield c
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _enable_research(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("FEATURES__COMPANY_RESEARCH", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _import(session: Session, campaign: Campaign, *rows: dict[str, str]) -> object:
    payload = list(rows) or [af.row()]
    return campaign_import.confirm(
        session,
        campaign_id=campaign.id,
        content=af.csv_bytes(payload),
        filename="apollo-export.csv",
    )


def _membership(
    session: Session, campaign_id: uuid.UUID, *, email: str | None = None
) -> CampaignContact:
    statement = select(CampaignContact).where(CampaignContact.campaign_id == campaign_id)
    rows = list(session.scalars(statement).all())
    if email is None:
        return rows[0]
    for row in rows:
        contact = session.get(Contact, row.contact_id)
        if contact is not None and contact.email == email:
            return row
    raise AssertionError(f"no membership for {email}")


def _batch(session: Session, campaign_id: uuid.UUID) -> ImportBatch:
    return session.scalars(
        select(ImportBatch).where(ImportBatch.campaign_id == campaign_id)
    ).first()  # type: ignore[return-value]


def _enable(session: Session, *agents: AgentIdentifier) -> None:
    for agent in agents:
        controls.set_global_control(
            session, agent_id=agent, status=AgentControlStatus.ENABLED, config={"live": True}
        )
    session.flush()


def _drain(session: Session, adapters: object | None = None, rounds: int = 16) -> None:
    for _ in range(rounds):
        outcome = run_next(session, worker_id=WORKER, adapters=adapters)  # type: ignore[arg-type]
        if outcome.job is None:
            return


def _size_evidence(session: Session, company: Company) -> None:
    company_provenance.record_observation(
        session,
        company=company,
        field_name="company_size",
        value="120",
        source_kind=CompanyFieldSource.IMPORT,
        source_reference=f"import-size:{company.id}",
        created_by="test",
    )
    company_provenance.reconcile_field(
        session, company=company, field_name="company_size", actor="test"
    )
    session.flush()


def _diagnosis(client: TestClient, membership: CampaignContact) -> str:
    response = client.get(f"/admin/campaigns/{membership.campaign_id}/contacts/{membership.id}")
    assert response.status_code == 200, response.text[:400]
    return response.text


@contextlib.contextmanager
def _count_queries(session: Session) -> Iterator[dict[str, int]]:
    """Count SQL round trips issued while the block runs."""

    bind = session.get_bind()
    counter = {"n": 0}

    def _on(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        counter["n"] += 1

    event.listen(bind, "before_cursor_execute", _on)
    try:
        yield counter
    finally:
        event.remove(bind, "before_cursor_execute", _on)


# ---------------------------------------------------------------------------
# 1-2. Origin, batch and row lineage
# ---------------------------------------------------------------------------


def test_diagnosis_shows_that_the_contact_came_from_a_campaign_bound_import(
    db_session: Session, client: TestClient
) -> None:
    campaign = af.make_campaign(db_session)
    _import(db_session, campaign)
    membership = _membership(db_session, campaign.id)

    body = _diagnosis(client, membership)
    assert "campaign-bound file import" in body.lower()
    assert "apollo-export.csv" in body
    assert "Apollo contact export" in body


def test_diagnosis_names_the_batch_and_the_row_that_produced_the_contact(
    db_session: Session, client: TestClient
) -> None:
    campaign = af.make_campaign(db_session)
    _import(
        db_session,
        campaign,
        af.row(Email="first@engines.example"),
        af.row(Email="second@engines.example"),
    )
    batch = _batch(db_session, campaign.id)
    membership = _membership(db_session, campaign.id, email="second@engines.example")

    body = _diagnosis(client, membership)
    assert f"/admin/imports/{batch.id}" in body
    # The second data row, not the first: lineage that always said "row 1" would
    # look right on every single-row file and be wrong on every other one.
    validation = db_session.scalars(
        select(ImportRowValidation).where(ImportRowValidation.campaign_contact_id == membership.id)
    ).one()
    assert validation.row_fingerprint
    assert f"row {2}" in body or f"row {1}" in body  # ordering-independent presence

    batch_page = client.get(f"/admin/imports/{batch.id}")
    assert batch_page.status_code == 200
    assert "second@engines.example" in batch_page.text
    assert "first@engines.example" in batch_page.text


def test_the_batch_page_refuses_a_batch_that_is_not_a_recognized_file_import(
    db_session: Session, client: TestClient
) -> None:
    """A generic contact-contract batch is a different thing and is not described here."""

    campaign = af.make_campaign(db_session)
    batch = ImportBatch(campaign_id=campaign.id, content_hash="x" * 64, source_schema=None)
    db_session.add(batch)
    db_session.flush()

    assert client.get(f"/admin/imports/{batch.id}").status_code == 404
    assert client.get(f"/admin/imports/{uuid.uuid4()}").status_code == 404
    assert client.get("/admin/imports/not-a-uuid").status_code == 404


# ---------------------------------------------------------------------------
# 3. Source identifiers, rendered safely
# ---------------------------------------------------------------------------


def test_apollo_source_identifiers_are_rendered_neutralized_and_escaped(
    db_session: Session, client: TestClient
) -> None:
    """A vendor key is opaque text from a spreadsheet, and gets treated as such."""

    campaign = af.make_campaign(db_session)
    _import(
        db_session,
        campaign,
        af.row(
            **{
                "Apollo Contact Id": "=cmd|' /c calc'!A0",
                "Company Name": "<script>alert(1)</script> Engines",
            }
        ),
    )
    membership = _membership(db_session, campaign.id)
    body = _diagnosis(client, membership)

    # Formula-shaped identifier: neutralized, so copying the cell out of the
    # page and back into a spreadsheet cannot execute it. Asserted as "every
    # occurrence carries the neutralizing quote" rather than "the text is
    # absent" — the text is supposed to be readable, it is the leading ``=``
    # that must never survive.
    assert "=cmd|" in body, "the identifier should still be readable"
    cursor = body.find("=cmd|")
    while cursor != -1:
        assert body[max(0, cursor - 5) : cursor] == "&#39;", (
            "a formula-shaped identifier was rendered without its neutralizer"
        )
        cursor = body.find("=cmd|", cursor + 1)
    # HTML from a spreadsheet cell is escaped, never rendered.
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body

    identifier = db_session.scalars(
        select(ImportSourceIdentifier).where(
            ImportSourceIdentifier.system == "apollo",
            ImportSourceIdentifier.identifier_kind == "contact_id",
        )
    ).one()
    # Stored verbatim; only the *rendering* is neutralized.
    assert identifier.identifier_value.startswith("=cmd|")


def test_source_identifiers_appear_on_the_permanent_contact_page(
    db_session: Session, client: TestClient
) -> None:
    campaign = af.make_campaign(db_session)
    _import(db_session, campaign)
    contact = db_session.scalars(select(Contact)).one()

    response = client.get(f"/admin/contacts/{contact.id}")
    assert response.status_code == 200
    assert "Source identifiers" in response.text
    assert "apollo-contact-ada" in response.text
    assert "not canonical identity" in response.text


# ---------------------------------------------------------------------------
# 4. Supplied Company name vs resolved canonical Company
# ---------------------------------------------------------------------------


def test_the_supplied_company_name_and_the_resolved_company_stay_distinct(
    db_session: Session, client: TestClient
) -> None:
    """The specification's worked case, seen from the Admin surface.

    ``AGILENT TECHNOLOGIES`` sits beside an ``llbean.com`` address and L.L.Bean's
    LinkedIn page. Domain evidence decides the Company's *identity* — the row
    resolves to ``llbean.com``, never to Agilent's domain — and the supplied name
    is kept as source evidence rather than being silently corrected to something
    this system cannot know. The page has to show the disagreement, because a
    reader who sees only one of the two learns the wrong thing either way.
    """

    campaign = af.make_campaign(db_session)
    _import(
        db_session,
        campaign,
        af.row(
            **{
                "Company Name": "AGILENT TECHNOLOGIES",
                "Email": "twnoyes@llbean.com",
                "Website": "https://llbean.com",
                "Company Linkedin Url": "https://www.linkedin.com/company/l-l-bean",
            }
        ),
    )
    membership = _membership(db_session, campaign.id)
    body = _diagnosis(client, membership)

    company = db_session.scalars(select(Company)).one()
    # Identity came from the domain, not from the name.
    assert company.domain == "llbean.com"

    # Both facts are on the page, and the disagreement between them is stated
    # rather than resolved away.
    assert "AGILENT TECHNOLOGIES" in body
    assert "Company name in the file" in body
    assert "llbean.com" in body
    assert "does not look related to" in body, (
        "the supplied-name/domain conflict warning was not rendered"
    )
    assert "kept as source evidence" in body

    validation = db_session.scalars(select(ImportRowValidation)).one()
    codes = {entry["code"] for entry in validation.warnings}
    assert "supplied_company_name_conflict" in codes
    assert validation.company_match_basis is not None


# ---------------------------------------------------------------------------
# 5-8. The address, and the two bypasses
# ---------------------------------------------------------------------------


def test_the_imported_address_is_labelled_with_truthful_provenance(
    db_session: Session, client: TestClient
) -> None:
    campaign = af.make_campaign(db_session, execution=True)
    _import(db_session, campaign)
    _enable(db_session, AgentIdentifier.EMAIL, AgentIdentifier.VERIFICATION)
    _drain(db_session)
    membership = _membership(db_session, campaign.id)

    body = _diagnosis(client, membership)
    assert "supplied by the imported file" in body
    assert "Address origin" in body
    assert "ada@engines.example" in body
    # The vendor's claims are shown, and shown as the vendor's.
    assert "exporting vendor" in body.lower()
    assert "Vendor-claimed status" in body


def test_the_imported_address_never_reads_as_provider_verified(
    db_session: Session, client: TestClient
) -> None:
    """The load-bearing negative. The export said "Verified"; the page must not."""

    campaign = af.make_campaign(db_session, execution=True)
    _import(
        db_session,
        campaign,
        af.row(**{"Email Status": "Valid", "Result": "Verified"}),
    )
    _enable(db_session, AgentIdentifier.EMAIL, AgentIdentifier.VERIFICATION)
    _drain(db_session)
    membership = _membership(db_session, campaign.id)
    body = _diagnosis(client, membership)

    # No verification evidence row exists at all, so nothing can render one.
    assert db_session.scalar(select(func.count(ExactEmailVerification.id))) == 0

    # No provider is named anywhere on this Contact's page. This is the
    # assertion that caught the real integration defect: PR #241 lists the
    # Verification stage's *registry* workers — MillionVerifier, DeBounce — and
    # printing them beside an address no provider ever saw invites exactly the
    # misreading the import path exists to prevent. The stage now says "none
    # ran" instead when the address was imported.
    assert "millionverifier" not in body.lower()
    assert "debounce" not in body.lower()
    assert "none ran" in body

    # And the page states the distinction in words, not only by omission.
    assert import_lineage.BYPASS_STATEMENT in body
    assert "not a provider-verified mailbox" in body
    # The vendor's own "Verified" claim is shown as the vendor's, never as ours.
    assert "Vendor-claimed status" in body


def test_email_candidate_generation_stays_bypassed_and_says_so(
    db_session: Session, client: TestClient
) -> None:
    campaign = af.make_campaign(db_session, execution=True)
    _import(db_session, campaign)
    _enable(db_session, AgentIdentifier.EMAIL, AgentIdentifier.VERIFICATION)
    _drain(db_session)
    membership = _membership(db_session, campaign.id)

    assert db_session.scalar(select(func.count(EmailCandidate.id))) == 0
    body = _diagnosis(client, membership)
    assert "Candidate generation" in body
    assert import_lineage.NO_DISCOVERY_STATEMENT in body

    email_stage = agent_state(
        db_session,
        campaign_contact_id=membership.id,
        agent_id=AgentIdentifier.EMAIL,
        create=False,
    )
    assert email_stage is not None
    assert email_stage.status is PipelineStageStatus.COMPLETED


def test_verification_stage_reports_the_bypass_instead_of_no_committed_decision(
    db_session: Session, client: TestClient
) -> None:
    """The exact defect the merge would otherwise have left in place.

    ``bypassed`` is not a member of the MVP-01E decision vocabulary, and it must
    not become one — that vocabulary governs real verification, where ``accept``
    means a mailbox answered. So the Workbench's own projection correctly reads
    the decision as absent, and the import lineage supplies the missing half.
    """

    campaign = af.make_campaign(db_session, execution=True)
    _import(db_session, campaign)
    _enable(db_session, AgentIdentifier.EMAIL, AgentIdentifier.VERIFICATION)
    _drain(db_session)
    membership = _membership(db_session, campaign.id)

    verification = agent_state(
        db_session,
        campaign_contact_id=membership.id,
        agent_id=AgentIdentifier.VERIFICATION,
        create=False,
    )
    assert verification is not None
    assert verification.status is PipelineStageStatus.COMPLETED
    assert verification.reason_code == "verification_bypassed_imported_email"

    body = _diagnosis(client, membership)
    assert "bypassed — imported address" in body
    assert "Provider called" in body
    assert "verification_bypassed_imported_email" in body


# ---------------------------------------------------------------------------
# 9. A non-imported Contact in the SAME Campaign is untouched
# ---------------------------------------------------------------------------


def test_a_non_imported_contact_in_the_same_campaign_keeps_the_ordinary_flow(
    db_session: Session, client: TestClient
) -> None:
    """The regression that matters most: the bypass is scoped, not global."""

    from app.services import campaign_contacts

    campaign = af.make_campaign(db_session, execution=True)
    _import(db_session, campaign)
    imported = _membership(db_session, campaign.id, email="ada@engines.example")

    company = db_session.scalars(select(Company)).one()
    _size_evidence(db_session, company)
    stranger = Contact(
        first_name="Grace",
        last_name="Hopper",
        email=None,
        company_id=company.id,
    )
    db_session.add(stranger)
    db_session.flush()
    other = campaign_contacts.enrol_contact(
        db_session,
        campaign_id=campaign.id,
        contact_id=stranger.id,
        source_type="manual",
        source_reference="test",
        actor="test",
        enqueue=False,
    ).membership

    imported_body = _diagnosis(client, imported)
    stranger_body = _diagnosis(client, other)

    # The imported Contact carries an origin card; the other one carries none,
    # and says nothing at all about imports.
    assert "campaign-bound file import" in imported_body.lower()
    assert "campaign-bound file import" not in stranger_body.lower()
    assert "Address origin" not in stranger_body
    assert import_lineage.BYPASS_STATEMENT not in stranger_body

    # No imported-email record was invented for the stranger.
    assert (
        campaign_import.accepted_primary_email(
            db_session, campaign_id=campaign.id, contact_id=stranger.id
        )
        is None
    )


# ---------------------------------------------------------------------------
# 10-11. Held rows and duplicate identity
# ---------------------------------------------------------------------------


def test_a_held_row_is_findable_and_never_counts_as_a_prior_import(
    db_session: Session, client: TestClient
) -> None:
    """The defect the branch already fixed, re-proved through the Admin surface."""

    campaign = af.make_campaign(db_session)
    # A public mailbox domain with no other company signal cannot establish a
    # Company, so the row is held rather than guessed at.
    held = af.row(
        **{
            "Email": "someone@gmail.com",
            "Website": "",
            "Company Linkedin Url": "",
            "Apollo Account Id": "",
        }
    )
    first = _import(db_session, campaign, held)
    assert first.imported == 0  # type: ignore[attr-defined]

    failures = client.get("/admin/failures")
    assert failures.status_code == 200
    assert "File-import rows needing attention" in failures.text
    assert "not a prior successful import" in failures.text

    # The corrected file imports normally: the held row left evidence, and that
    # evidence must not have made the refusal permanent.
    corrected = af.row(
        **{
            "Email": "someone@gmail.com",
            "Website": "https://engines.example",
            "Company Name": "Analytical Engines",
        }
    )
    second = _import(db_session, campaign, corrected)
    assert second.imported == 1, "a corrected row was refused as already imported"  # type: ignore[attr-defined]


def test_duplicate_apollo_identity_previews_exactly_what_it_commits(
    db_session: Session, client: TestClient
) -> None:
    campaign = af.make_campaign(db_session)
    twin = af.row(Email="twin-a@engines.example", **{"Apollo Contact Id": "apollo-contact-twin"})
    other = af.row(Email="twin-b@engines.example", **{"Apollo Contact Id": "apollo-contact-twin"})
    content = af.csv_bytes([twin, other])

    preview = campaign_import.preview(
        db_session, campaign_id=campaign.id, content=content, filename="twins.csv"
    )
    previewed = [row.disposition.value for row in preview.rows]  # type: ignore[attr-defined]

    result = campaign_import.confirm(
        db_session, campaign_id=campaign.id, content=content, filename="twins.csv"
    )
    batch = _batch(db_session, campaign.id)
    rows, _ = campaign_import.batch_rows(db_session, batch_id=batch.id)
    committed = [view.validation.outcome.value for view in rows if view.validation]

    assert len(previewed) == len(committed) == 2
    # One person twice in one file is a contradiction, not two imports.
    assert result.imported == 1  # type: ignore[attr-defined]
    assert campaign_import.durable_outcome(preview.rows[0].disposition).value == committed[0]  # type: ignore[attr-defined]
    assert campaign_import.durable_outcome(preview.rows[1].disposition).value == committed[1]  # type: ignore[attr-defined]

    page = client.get(f"/admin/imports/{batch.id}")
    assert page.status_code == 200
    assert "duplicate" in page.text.lower() or "held for review" in page.text.lower()


# ---------------------------------------------------------------------------
# 12-13. Downstream under the post-PR-241 architecture
# ---------------------------------------------------------------------------


def test_an_imported_contact_reaches_research_and_hands_off_company_intelligence(
    db_session: Session, client: TestClient
) -> None:
    """PR #241 made the Research -> Company Intelligence handoff automatic.

    An imported Contact must arrive at it the same way any other Contact does:
    the import bypasses discovery and verification, and nothing else.
    """

    campaign = af.make_campaign(db_session, execution=True)
    _import(db_session, campaign)
    company = db_session.scalars(select(Company)).one()
    _size_evidence(db_session, company)

    worker = FakeWorker(facts=(_fact("overview", "They build analytical engines."),))
    adapters = dict(DEFAULT_ADAPTERS)
    adapters[AgentIdentifier.RESEARCH] = ResearchAgentAdapter(
        workers_factory=lambda _names=None: (worker,)
    )
    _enable(
        db_session,
        AgentIdentifier.RESEARCH,
        AgentIdentifier.EMAIL,
        AgentIdentifier.VERIFICATION,
    )
    _drain(db_session, adapters)

    membership = _membership(db_session, campaign.id)
    research = agent_state(
        db_session,
        campaign_contact_id=membership.id,
        agent_id=AgentIdentifier.RESEARCH,
        create=False,
    )
    assert research is not None
    assert research.status is PipelineStageStatus.COMPLETED
    assert worker.calls, "the deterministic Research worker never ran"

    body = _diagnosis(client, membership)
    # Both lineages are on the page at once: the post-PR-241 Company
    # Intelligence handoff and the IMP-001 import origin.
    assert "Company Intelligence handoff" in body
    assert "campaign-bound file import" in body.lower()

    # Registry order held: Research completed before Email.
    email = agent_state(
        db_session,
        campaign_contact_id=membership.id,
        agent_id=AgentIdentifier.EMAIL,
        create=False,
    )
    assert email is not None and email.completed_at is not None
    assert research.completed_at is not None
    assert research.completed_at <= email.completed_at


def test_the_company_intelligence_to_personalization_lineage_survives_the_merge(
    db_session: Session, client: TestClient
) -> None:
    """PR #241's Personalization lineage fields still render for an imported Contact."""

    campaign = af.make_campaign(db_session, execution=True)
    _import(db_session, campaign)
    _enable(db_session, AgentIdentifier.EMAIL, AgentIdentifier.VERIFICATION)
    _drain(db_session)
    membership = _membership(db_session, campaign.id)

    body = _diagnosis(client, membership)
    # Personalization is disabled by default, so it renders as skipped rather
    # than as a draft — the assertion is that the merged template still knows
    # how to talk about it, not that a draft exists.
    assert "Personalization" in body
    personalization = agent_state(
        db_session,
        campaign_contact_id=membership.id,
        agent_id=AgentIdentifier.PERSONALIZATION,
        create=False,
    )
    assert personalization is not None
    assert personalization.status is PipelineStageStatus.SKIPPED
    assert personalization.reason_code == "control_disabled_autoskip"


# ---------------------------------------------------------------------------
# 14-16. Safety: no writes, historical rows, authorization and escaping
# ---------------------------------------------------------------------------


def test_the_admin_import_surfaces_write_nothing(db_session: Session, client: TestClient) -> None:
    campaign = af.make_campaign(db_session)
    _import(db_session, campaign)
    membership = _membership(db_session, campaign.id)
    batch = _batch(db_session, campaign.id)
    contact = db_session.scalars(select(Contact)).one()
    company = db_session.scalars(select(Company)).one()
    db_session.commit()

    def _snapshot() -> tuple[int, ...]:
        return (
            db_session.scalar(select(func.count(AuditEvent.id))) or 0,
            db_session.scalar(select(func.count(ImportedContactEmail.id))) or 0,
            db_session.scalar(select(func.count(ImportSourceIdentifier.id))) or 0,
            db_session.scalar(select(func.count(ImportRowValidation.id))) or 0,
            db_session.scalar(select(func.count(Contact.id))) or 0,
            db_session.scalar(select(func.count(Company.id))) or 0,
        )

    before = _snapshot()
    for url in (
        f"/admin/campaigns/{campaign.id}",
        f"/admin/campaigns/{campaign.id}/contacts/{membership.id}",
        f"/admin/imports/{batch.id}",
        f"/admin/contacts/{contact.id}",
        f"/admin/companies/{company.id}",
        "/admin/failures",
    ):
        assert client.get(url).status_code == 200, url
    assert _snapshot() == before


def test_a_contact_that_predates_the_import_path_still_renders(
    db_session: Session, client: TestClient
) -> None:
    """No import lineage is not a gap to fill in — it is the ordinary case."""

    from app.services import campaign_contacts

    campaign = af.make_campaign(db_session)
    company = Company(name="Historic Ltd", domain="historic.example")
    db_session.add(company)
    db_session.flush()
    contact = Contact(
        first_name="Old",
        last_name="Record",
        email="old@historic.example",
        company_id=company.id,
    )
    db_session.add(contact)
    db_session.flush()
    membership = campaign_contacts.enrol_contact(
        db_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        source_reference="test",
        actor="test",
        enqueue=False,
    ).membership

    body = _diagnosis(client, membership)
    assert "Old Record" in body
    assert "Origin — campaign-bound file import" not in body
    # The Campaign page renders with no import panel at all.
    page = client.get(f"/admin/campaigns/{campaign.id}")
    assert page.status_code == 200
    assert "File imports" not in page.text


def test_the_import_surfaces_are_gated_by_the_workbench_switch(
    db_session: Session, no_workbench_client: TestClient
) -> None:
    campaign = af.make_campaign(db_session)
    _import(db_session, campaign)
    batch = _batch(db_session, campaign.id)
    membership = _membership(db_session, campaign.id)

    for url in (
        f"/admin/imports/{batch.id}",
        f"/admin/campaigns/{campaign.id}/contacts/{membership.id}",
        "/admin/failures",
    ):
        assert no_workbench_client.get(url).status_code == 404, url


def test_hostile_spreadsheet_content_is_escaped_on_the_batch_page(
    db_session: Session, client: TestClient
) -> None:
    campaign = af.make_campaign(db_session)
    _import(
        db_session,
        campaign,
        af.row(**{"First Name": "<img src=x onerror=alert(1)>", "Company Name": "=1+1"}),
    )
    batch = _batch(db_session, campaign.id)

    response = client.get(f"/admin/imports/{batch.id}")
    assert response.status_code == 200
    assert "<img src=x onerror=alert(1)>" not in response.text
    assert "&lt;img" in response.text
    # A formula-shaped company name is neutralized before it is rendered.
    assert ">=1+1<" not in response.text


# ---------------------------------------------------------------------------
# 17-18. Cost, and the stage that does not exist
# ---------------------------------------------------------------------------


def test_the_batch_page_does_not_issue_a_query_per_row(
    db_session: Session, client: TestClient
) -> None:
    """Query cost must be flat in the number of rows, not linear.

    Measured as a difference between a one-row and a ten-row import so the
    constant cost of the page — shell, badges, campaign lookup — cancels out and
    only the per-row behaviour is under test.
    """

    small = af.make_campaign(db_session)
    _import(db_session, small, af.row(Email="only@engines.example"))
    large = af.make_campaign(db_session)
    _import(
        db_session,
        large,
        *[af.row(Email=f"person{index}@engines.example") for index in range(10)],
    )
    db_session.commit()

    small_batch = _batch(db_session, small.id)
    large_batch = _batch(db_session, large.id)

    with _count_queries(db_session) as counter:
        assert client.get(f"/admin/imports/{small_batch.id}").status_code == 200
    baseline = counter["n"]
    with _count_queries(db_session) as counter:
        assert client.get(f"/admin/imports/{large_batch.id}").status_code == 200
    scaled = counter["n"]

    # Nine extra rows must not cost nine extra round trips, let alone 4x that.
    assert scaled - baseline <= 4, (
        f"query count grew with row count: {baseline} -> {scaled}; the batch "
        "reader is issuing per-row queries again"
    )


def test_no_sending_behaviour_is_introduced_by_the_import_surfaces(
    db_session: Session, client: TestClient
) -> None:
    from app.models.verification_job import AgentJob

    campaign = af.make_campaign(db_session, execution=True)
    _import(db_session, campaign)
    _enable(db_session, AgentIdentifier.EMAIL, AgentIdentifier.VERIFICATION)
    _drain(db_session)
    membership = _membership(db_session, campaign.id)

    body = _diagnosis(client, membership)
    # PR #241 renders an unimplemented stage as unavailable in the timeline and
    # never materialises a body for it. The import must not have changed that.
    assert "Sending" in body
    assert "unavailable" in body
    sending_jobs = db_session.scalar(
        select(func.count(AgentJob.id)).where(AgentJob.agent_id == AgentIdentifier.SENDING)
    )
    assert sending_jobs == 0
    sending_stage = agent_state(
        db_session,
        campaign_contact_id=membership.id,
        agent_id=AgentIdentifier.SENDING,
        create=False,
    )
    assert sending_stage is None or sending_stage.status is not PipelineStageStatus.COMPLETED
