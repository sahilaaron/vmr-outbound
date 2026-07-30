"""The company workspace web layer (APP-003).

Route smoke tests plus the behaviour that must not regress: an operator can
browse companies and the people at them without a campaign existing anywhere,
an unresolved company is shown as unresolved rather than hidden, a research
claim is never displayed as a canonical value, and an identity conflict stays
on the page instead of being resolved by a guess.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from app.core.config import get_settings
from app.main import create_app
from app.models.company import Company
from app.models.contact import Contact
from app.models.enums import CompanyFieldSource, ResearchState
from app.services.companies import dossiers as company_dossiers
from app.services.companies import provenance as company_provenance
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The workbench is off by default (FND-007); this suite opts in."""

    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


def _company(session: Session, **kwargs: Any) -> Company:
    company = Company(
        name=kwargs.pop("name", "Acme Systems"),
        domain=kwargs.pop("domain", "acme.example"),
        **kwargs,
    )
    session.add(company)
    session.commit()
    return company


def _contact(
    session: Session, *, domain: str, company_id: uuid.UUID | None, **kwargs: Any
) -> Contact:
    first = kwargs.pop("first", "Dana")
    last = kwargs.pop("last", "Reyes")
    contact = Contact(
        first_name=first,
        last_name=last,
        company_name="Acme Systems",
        company_domain=domain,
        company_id=company_id,
        natural_key=f"{first.casefold()}|{last.casefold()}|{domain}",
        **kwargs,
    )
    session.add(contact)
    session.commit()
    return contact


# --- browsing ----------------------------------------------------------------


def test_the_company_list_renders_with_no_companies(client: TestClient) -> None:
    response = client.get("/companies")
    assert response.status_code == 200
    assert "No companies yet" in response.text


def test_the_company_list_shows_a_company_and_its_people(
    client: TestClient, committed_session: Session
) -> None:
    company = _company(committed_session)
    _contact(committed_session, domain="acme.example", company_id=company.id)

    response = client.get("/companies")
    assert response.status_code == 200
    assert "Acme Systems" in response.text
    assert "acme.example" in response.text


def test_the_company_detail_page_lists_linked_contacts(
    client: TestClient, committed_session: Session
) -> None:
    """The acceptance criterion, end to end: browse companies and their people."""

    company = _company(committed_session)
    _contact(committed_session, domain="acme.example", company_id=company.id, first="Wren")

    response = client.get(f"/companies/{company.id}")
    assert response.status_code == 200
    assert "Wren" in response.text
    assert "/contacts/" in response.text


def test_a_missing_company_is_a_clean_not_found(client: TestClient) -> None:
    response = client.get(f"/companies/{uuid.uuid4()}")
    assert response.status_code == 404
    assert "does not exist" in response.text


def test_a_malformed_company_id_does_not_raise(client: TestClient) -> None:
    """Hand-edited URLs get an answer, not a stack trace."""

    assert client.get("/companies/not-a-uuid").status_code == 404


def test_companies_appear_in_the_navigation(client: TestClient) -> None:
    response = client.get("/companies")
    assert 'href="/companies"' in response.text


# --- the product rules, as rendered ------------------------------------------


def test_no_page_mentions_a_campaign(client: TestClient, committed_session: Session) -> None:
    """Company intelligence belongs to the company, not to a campaign."""

    company = _company(committed_session)
    listing = client.get("/companies").text.lower()
    detail = client.get(f"/companies/{company.id}").text.lower()

    # The rail links to /campaigns on every page; what must not appear is a
    # campaign control or column inside the company workspace itself.
    for body in (listing, detail):
        assert "campaign selector" not in body
        assert 'name="campaign' not in body


def test_an_unresolved_company_is_shown_as_unresolved(
    client: TestClient, committed_session: Session
) -> None:
    company = _company(committed_session, name="Unresolved", domain=None)

    response = client.get(f"/companies/{company.id}")
    assert response.status_code == 200
    assert "no domain" in response.text
    assert "No canonical domain" in response.text


def test_an_unknown_field_reads_as_unknown_not_empty(
    client: TestClient, committed_session: Session
) -> None:
    """Missing data is unknown, not false."""

    company = _company(committed_session)
    response = client.get(f"/companies/{company.id}")
    assert "unknown" in response.text.lower()


def test_a_research_claim_is_not_displayed_as_a_canonical_value(
    client: TestClient, committed_session: Session
) -> None:
    """A dossier claiming an industry has not set one, and the page must agree."""

    company = _company(committed_session)
    submission, _ = company_dossiers.submit(
        committed_session,
        company=company,
        producer="test-producer",
        payload={"industry": "Aerospace"},
    )
    company_dossiers.interpret(
        committed_session,
        company=company,
        submission=submission,
        interpreter="test-interpreter",
        sections={"industries": {"values": ["Aerospace"]}},
    )
    committed_session.commit()

    response = client.get(f"/companies/{company.id}")
    assert response.status_code == 200
    committed_session.refresh(company)
    assert company.industry is None, "the claim must not have become the canonical value"
    assert "current" in response.text


def test_a_reconciled_value_is_shown_with_its_reason(
    client: TestClient, committed_session: Session
) -> None:
    company = _company(committed_session)
    company_provenance.record_observation(
        committed_session,
        company=company,
        field_name="industry",
        value="Logistics",
        source_kind=CompanyFieldSource.LINKEDIN_COMPANY_SNAPSHOT,
    )
    company_provenance.reconcile_field(committed_session, company=company, field_name="industry")
    committed_session.commit()

    response = client.get(f"/companies/{company.id}")
    assert "Logistics" in response.text
    assert "linkedin company snapshot" in response.text


def test_a_domain_only_match_is_labelled_transitional(
    client: TestClient, committed_session: Session
) -> None:
    """The UI must not present an unmade link as a made one."""

    company = _company(committed_session)
    _contact(committed_session, domain="acme.example", company_id=None, first="Unlinked")

    response = client.get(f"/companies/{company.id}")
    assert "matched by domain string only" in response.text.lower()
    assert "not evidence that they work here" in response.text


def test_an_identity_conflict_stays_on_the_page(
    client: TestClient, committed_session: Session
) -> None:
    """The conflict must be named, not merely alluded to.

    Asserting on the phrase "identity disagreement" alone would pass against the
    *empty* state, which says "No identity disagreement found." The assertions
    below are on content that only a real conflict produces.
    """

    company = _company(committed_session)
    _contact(committed_session, domain="acme-old.example", company_id=company.id)

    body = client.get(f"/companies/{company.id}").text
    assert "Linked contacts captured a different domain" in body
    assert "acme-old.example" in body
    assert "1 identity disagreement(s)" in body


def test_a_company_with_no_conflict_says_so_rather_than_staying_silent(
    client: TestClient, committed_session: Session
) -> None:
    """The counterpart of the test above, and what makes it meaningful."""

    company = _company(committed_session)
    _contact(committed_session, domain="acme.example", company_id=company.id)

    body = client.get(f"/companies/{company.id}").text
    assert "No identity disagreement found" in body
    assert "Linked contacts captured a different domain" not in body


def test_the_research_state_says_no_engine_exists(
    client: TestClient, committed_session: Session
) -> None:
    """Truthful empty state: the page must not imply a research engine."""

    company = _company(committed_session)
    response = client.get(f"/companies/{company.id}")
    assert "No research engine exists yet" in response.text
    assert "not requested" in response.text


def test_the_detail_page_is_read_only(client: TestClient, committed_session: Session) -> None:
    """Domain confirmation happens in one place, and it is not here.

    Two screens offering the same decision is two ways to make it differently.
    """

    company = _company(committed_session)
    response = client.get(f"/companies/{company.id}")
    assert "<form" not in response.text.lower()


def test_filtering_by_research_state_works(client: TestClient, committed_session: Session) -> None:
    _company(committed_session, name="Quiet", domain="quiet.example")
    response = client.get(f"/companies?research={ResearchState.NOT_REQUESTED.value}")
    assert response.status_code == 200
    assert "Quiet" in response.text


def test_an_unknown_filter_value_widens_rather_than_failing(client: TestClient) -> None:
    assert client.get("/companies?research=nonsense&view=nonsense&sort=nonsense").status_code == 200


def test_a_dossier_section_shows_its_content_not_just_that_it_exists(
    client: TestClient, committed_session: Session
) -> None:
    """The page used to prove research had happened without showing any of it.

    A badge reading "present" answered "did a producer address this section?" and
    nothing else. An operator reviewing what the system believes about a company
    needs the words.
    """

    from app.services.companies import dossiers

    company = _company(committed_session)
    payload = {
        "overview": {"summary": "Builds kiln controllers for cement plants."},
        "industries": {"values": []},
    }
    submission, _ = dossiers.submit(
        committed_session, company=company, producer="test-producer", payload=payload
    )
    dossiers.interpret(
        committed_session,
        company=company,
        submission=submission,
        interpreter="test",
        sections={"overview": payload["overview"], "industries": payload["industries"]},
    )
    committed_session.commit()

    body = client.get(f"/companies/{company.id}").text
    assert "Builds kiln controllers for cement plants." in body
    # An empty container still renders: "looked and found nothing" must not read
    # as "did not look". Jinja escapes the quotes, so match on the shape.
    assert "values" in body
    assert "[]" in body
    # And the raw submission stays available to check an interpretation against.
    assert "test-producer" in body or "raw submission" in body


def test_the_conflicts_card_is_absent_when_there_is_no_disagreement(
    client: TestClient, committed_session: Session
) -> None:
    """An always-present card whose content was "nothing to see" trained the eye
    to skip the one place a real conflict appears. The consistent state is still
    reported, by the badge beside the company name."""

    company = _company(committed_session)
    body = client.get(f"/companies/{company.id}").text
    assert "<h2>Identity conflicts</h2>" not in body
    assert "consistent" in body


def test_a_captured_linkedin_url_is_shown_when_the_canonical_column_is_empty(
    client: TestClient, committed_session: Session
) -> None:
    """Nothing writes companies.linkedin_company_url, so the row was always a dash.

    The captured URL is display-only and labelled as such — it is what was
    observed, not a canonical field and not an identity claim.
    """

    from datetime import UTC, datetime

    from app.models.enums import LinkedInSnapshotOutcome
    from app.models.linkedin_company import LinkedInCompanySnapshot

    company = _company(committed_session)
    committed_session.add(
        LinkedInCompanySnapshot(
            client_capture_id=f"cap-{uuid.uuid4()}",
            content_hash="hash",
            schema_version="linkedin-company/1.0.0",
            source="test",
            normalized_company_url="https://www.linkedin.com/company/acme-systems",
            captured_at=datetime.now(UTC),
            ingested_at=datetime.now(UTC),
            extraction_status="ok",
            payload={},
            company_fields={},
            outcome=LinkedInSnapshotOutcome.STORED,
            matched_company_id=company.id,
        )
    )
    committed_session.commit()

    body = client.get(f"/companies/{company.id}").text
    assert "https://www.linkedin.com/company/acme-systems" in body
    assert "From a capture, not a canonical field." in body
    assert "LinkedIn Profile" in body
