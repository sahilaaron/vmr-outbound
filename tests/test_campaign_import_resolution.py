"""Contact and Company identity resolution for the file import (IMP-001 §25.10-19)."""

from __future__ import annotations

import pytest
from app.models.company import Company
from app.models.contact import Contact
from app.models.enums import LinkedInIdentifierKind
from app.models.imported_email import ImportSourceIdentifier
from app.services import identity_links
from app.services.imports import campaign_import
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tests import apollo_factory as af

pytestmark = pytest.mark.usefixtures("enable_csv_import")


def _confirm(session: Session, campaign_id: object, rows: list[dict[str, str]]) -> object:
    return campaign_import.confirm(
        session,
        campaign_id=campaign_id,  # type: ignore[arg-type]
        content=af.csv_bytes(rows),
        filename="apollo.csv",
    )


def _only_row(session: Session, batch_id: object) -> object:
    views, _total = campaign_import.batch_rows(session, batch_id=batch_id)  # type: ignore[arg-type]
    assert len(views) == 1
    return views[0]


def _company(session: Session, **kwargs: object) -> Company:
    company = Company(**kwargs)  # type: ignore[arg-type]
    session.add(company)
    session.flush()
    return company


def _contact(session: Session, **kwargs: object) -> Contact:
    contact = Contact(**kwargs)  # type: ignore[arg-type]
    session.add(contact)
    session.flush()
    return contact


# --- 10-13. Contact identity ------------------------------------------------


def test_existing_contact_is_reused_by_normalized_email(db_session: Session) -> None:
    campaign = af.make_campaign(db_session)
    existing = _contact(
        db_session,
        first_name="Ada",
        last_name="Lovelace",
        email="ada@engines.example",
        company_domain="engines.example",
    )
    before = db_session.scalar(select(func.count()).select_from(Contact))

    result = _confirm(db_session, campaign.id, [af.row(**{"Email": "ADA@Engines.Example"})])
    assert result.matched_existing == 1  # type: ignore[attr-defined]
    assert result.contacts_created == 0  # type: ignore[attr-defined]
    assert db_session.scalar(select(func.count()).select_from(Contact)) == before

    view = _only_row(db_session, result.batch_id)  # type: ignore[attr-defined]
    assert view.contact is not None and view.contact.id == existing.id  # type: ignore[attr-defined]
    assert view.validation.contact_match_basis == "normalized_email"  # type: ignore[attr-defined]


def test_existing_contact_is_reused_by_apollo_contact_id(db_session: Session) -> None:
    campaign = af.make_campaign(db_session)
    existing = _contact(db_session, first_name="Ada", last_name="Lovelace")
    db_session.add(
        ImportSourceIdentifier(
            contact_id=existing.id,
            system="apollo",
            identifier_kind="contact_id",
            identifier_value="apollo-contact-ada",
        )
    )
    db_session.flush()

    result = _confirm(db_session, campaign.id, [af.row()])
    assert result.matched_existing == 1  # type: ignore[attr-defined]
    view = _only_row(db_session, result.batch_id)  # type: ignore[attr-defined]
    assert view.contact.id == existing.id  # type: ignore[attr-defined]
    assert view.validation.contact_match_basis == "apollo_contact_id"  # type: ignore[attr-defined]
    # The address the file supplied fills the Contact's empty one.
    db_session.refresh(existing)
    assert existing.email == "ada@engines.example"


def test_existing_contact_is_reused_by_linkedin_profile_identity(db_session: Session) -> None:
    campaign = af.make_campaign(db_session)
    existing = _contact(db_session, first_name="Ada", last_name="Lovelace")
    identity_links.record_observed(
        db_session,
        contact=existing,
        kind=LinkedInIdentifierKind.PUBLIC_VANITY_URL,
        value="https://www.linkedin.com/in/ada",
        decided_by="test",
    )
    db_session.flush()

    # No Apollo id, so LinkedIn is the only signal that can match.
    result = _confirm(
        db_session,
        campaign.id,
        [
            af.row(
                **{
                    "Apollo Contact Id": "",
                    "Apollo Record Id": "",
                    "Person Linkedin Url": "https://LinkedIn.com/in/Ada/",
                }
            )
        ],
    )
    assert result.matched_existing == 1  # type: ignore[attr-defined]
    view = _only_row(db_session, result.batch_id)  # type: ignore[attr-defined]
    assert view.contact.id == existing.id  # type: ignore[attr-defined]
    assert view.validation.contact_match_basis == "linkedin_profile_url"  # type: ignore[attr-defined]


def test_a_new_contact_is_created_when_nothing_matches(db_session: Session) -> None:
    campaign = af.make_campaign(db_session)
    result = _confirm(db_session, campaign.id, [af.row()])
    assert result.imported == 1  # type: ignore[attr-defined]
    assert result.contacts_created == 1  # type: ignore[attr-defined]
    view = _only_row(db_session, result.batch_id)  # type: ignore[attr-defined]
    assert view.contact.email == "ada@engines.example"  # type: ignore[attr-defined]
    assert view.contact.company_id is not None  # type: ignore[attr-defined]
    assert view.validation.contact_match_basis == "created"  # type: ignore[attr-defined]


def test_disagreeing_contact_signals_are_held_for_review_never_merged(
    db_session: Session,
) -> None:
    campaign = af.make_campaign(db_session)
    by_email = _contact(
        db_session, first_name="Ada", last_name="Lovelace", email="ada@engines.example"
    )
    by_apollo = _contact(db_session, first_name="Augusta", last_name="Byron")
    db_session.add(
        ImportSourceIdentifier(
            contact_id=by_apollo.id,
            system="apollo",
            identifier_kind="contact_id",
            identifier_value="apollo-contact-ada",
        )
    )
    db_session.flush()
    before = db_session.scalar(select(func.count()).select_from(Contact))

    result = _confirm(db_session, campaign.id, [af.row()])
    assert result.review_required == 1  # type: ignore[attr-defined]
    assert result.imported == 0  # type: ignore[attr-defined]
    assert db_session.scalar(select(func.count()).select_from(Contact)) == before
    view = _only_row(db_session, result.batch_id)  # type: ignore[attr-defined]
    assert view.validation.error_code == "contact_identity_ambiguous"  # type: ignore[attr-defined]
    # Neither contact was touched.
    db_session.refresh(by_email)
    db_session.refresh(by_apollo)
    assert by_apollo.email is None


def test_a_matched_contact_with_a_different_address_is_held_for_review(
    db_session: Session,
) -> None:
    campaign = af.make_campaign(db_session)
    existing = _contact(db_session, first_name="Ada", last_name="Lovelace", email="ada@old.example")
    db_session.add(
        ImportSourceIdentifier(
            contact_id=existing.id,
            system="apollo",
            identifier_kind="contact_id",
            identifier_value="apollo-contact-ada",
        )
    )
    db_session.flush()

    result = _confirm(db_session, campaign.id, [af.row()])
    assert result.review_required == 1  # type: ignore[attr-defined]
    view = _only_row(db_session, result.batch_id)  # type: ignore[attr-defined]
    assert view.validation.error_code == "contact_email_conflict"  # type: ignore[attr-defined]
    db_session.refresh(existing)
    assert existing.email == "ada@old.example"  # never overwritten


# --- 14-16. Company identity ------------------------------------------------


def test_existing_company_is_reused_by_apollo_account_id(db_session: Session) -> None:
    campaign = af.make_campaign(db_session)
    company = _company(db_session, name="Engines Ltd", domain="engines.example")
    db_session.add(
        ImportSourceIdentifier(
            company_id=company.id,
            system="apollo",
            identifier_kind="account_id",
            identifier_value="apollo-account-1",
        )
    )
    db_session.flush()
    before = db_session.scalar(select(func.count()).select_from(Company))

    result = _confirm(db_session, campaign.id, [af.row()])
    assert db_session.scalar(select(func.count()).select_from(Company)) == before
    view = _only_row(db_session, result.batch_id)  # type: ignore[attr-defined]
    assert view.company.id == company.id  # type: ignore[attr-defined]
    assert view.validation.company_match_basis == "apollo_account_id"  # type: ignore[attr-defined]


def test_existing_company_is_reused_by_canonical_website_domain(db_session: Session) -> None:
    campaign = af.make_campaign(db_session)
    company = _company(db_session, name="Engines Ltd", domain="engines.example")
    result = _confirm(
        db_session, campaign.id, [af.row(**{"Apollo Account Id": "", "Company Linkedin Url": ""})]
    )
    view = _only_row(db_session, result.batch_id)  # type: ignore[attr-defined]
    assert view.company.id == company.id  # type: ignore[attr-defined]
    assert view.validation.company_match_basis == "website_domain"  # type: ignore[attr-defined]


def test_existing_company_is_reused_by_company_linkedin_url(db_session: Session) -> None:
    campaign = af.make_campaign(db_session)
    company = _company(
        db_session,
        name="Engines Ltd",
        domain="engines-old.example",
        linkedin_company_url="https://www.linkedin.com/company/analytical-engines",
        linkedin_company_id="analytical-engines",
    )
    result = _confirm(
        db_session,
        campaign.id,
        [
            af.row(
                **{
                    "Apollo Account Id": "",
                    "Website": "",
                    "Email": "ada@engines-old.example",
                }
            )
        ],
    )
    view = _only_row(db_session, result.batch_id)  # type: ignore[attr-defined]
    assert view.company.id == company.id  # type: ignore[attr-defined]
    assert view.validation.company_match_basis in {"company_linkedin", "email_domain"}  # type: ignore[attr-defined]


def test_conflicting_company_signals_do_not_silently_match(db_session: Session) -> None:
    """Two signals naming two different permanent Companies is a held row."""

    campaign = af.make_campaign(db_session)
    by_domain = _company(db_session, name="Engines Ltd", domain="engines.example")
    by_linkedin = _company(
        db_session,
        name="A Completely Different Firm",
        domain="different.example",
        linkedin_company_url="https://www.linkedin.com/company/analytical-engines",
        linkedin_company_id="analytical-engines",
    )
    db_session.flush()

    result = _confirm(db_session, campaign.id, [af.row(**{"Apollo Account Id": ""})])
    assert result.review_required == 1  # type: ignore[attr-defined]
    view = _only_row(db_session, result.batch_id)  # type: ignore[attr-defined]
    assert view.validation.error_code == "company_identity_ambiguous"  # type: ignore[attr-defined]
    assert view.contact is None  # type: ignore[attr-defined]
    assert by_domain.id != by_linkedin.id


# --- 17-18. The AGILENT / llbean.com pattern --------------------------------


AGILENT_LLBEAN = {
    "First Name": "Tom",
    "Last Name": "Noyes",
    "Email": "twnoyes@llbean.com",
    "Company Name": "AGILENT TECHNOLOGIES",
    "Company Name for Emails": "AGILENT TECHNOLOGIES",
    "Website": "https://llbean.com",
    "Company Linkedin Url": "https://www.linkedin.com/company/l-l-bean",
    "Apollo Account Id": "",
}


def test_agilent_llbean_resolves_to_the_agreeing_domain_with_a_warning(
    db_session: Session,
) -> None:
    """Website, email domain and LinkedIn agree; the supplied name is the outlier.

    The row is imported to the company the evidence names, and the conflicting
    supplied name is kept and surfaced. What must never happen is a Bean employee
    silently filed under Agilent.
    """

    campaign = af.make_campaign(db_session)
    result = _confirm(db_session, campaign.id, [af.row(**AGILENT_LLBEAN)])
    assert result.imported == 1  # type: ignore[attr-defined]

    view = _only_row(db_session, result.batch_id)  # type: ignore[attr-defined]
    assert view.company is not None  # type: ignore[attr-defined]
    assert view.company.domain == "llbean.com"  # type: ignore[attr-defined]
    codes = {warning["code"] for warning in view.validation.warnings}  # type: ignore[attr-defined]
    assert "supplied_company_name_conflict" in codes

    # There is no Agilent company anywhere.
    agilent = db_session.scalars(
        select(Company).where(func.lower(Company.name).like("%agilent%"))
    ).all()
    assert [company for company in agilent if company.domain != "llbean.com"] == []


def test_agilent_llbean_becomes_review_required_when_agilent_already_exists(
    db_session: Session,
) -> None:
    """With a real Agilent on file, the two signals disagree and the row waits."""

    campaign = af.make_campaign(db_session)
    _company(db_session, name="Agilent Technologies", domain="agilent.com")
    _company(
        db_session,
        name="L.L.Bean",
        domain="llbean.com",
        linkedin_company_url="https://www.linkedin.com/company/l-l-bean",
        linkedin_company_id="l-l-bean",
    )
    # A website that names Agilent while the address and LinkedIn name Bean.
    result = _confirm(
        db_session,
        campaign.id,
        [af.row(**{**AGILENT_LLBEAN, "Website": "https://agilent.com"})],
    )
    assert result.review_required == 1  # type: ignore[attr-defined]
    view = _only_row(db_session, result.batch_id)  # type: ignore[attr-defined]
    assert view.validation.error_code == "company_identity_ambiguous"  # type: ignore[attr-defined]


def test_a_website_and_email_domain_that_disagree_hold_the_row(db_session: Session) -> None:
    campaign = af.make_campaign(db_session)
    result = _confirm(
        db_session,
        campaign.id,
        [
            af.row(
                **{
                    "Website": "https://agilent.com",
                    "Email": "twnoyes@llbean.com",
                    "Apollo Account Id": "",
                    "Company Linkedin Url": "",
                }
            )
        ],
    )
    assert result.review_required == 1  # type: ignore[attr-defined]
    view = _only_row(db_session, result.batch_id)  # type: ignore[attr-defined]
    assert view.validation.error_code == "company_domain_conflict"  # type: ignore[attr-defined]


# --- 19. Public email domains -----------------------------------------------


def test_a_public_email_domain_never_becomes_a_company(db_session: Session) -> None:
    campaign = af.make_campaign(db_session)
    result = _confirm(
        db_session,
        campaign.id,
        [af.row(**{"Email": "ada.lovelace@gmail.com"})],
    )
    assert result.imported == 1  # type: ignore[attr-defined]
    view = _only_row(db_session, result.batch_id)  # type: ignore[attr-defined]
    # The Website carried the identity, not the mailbox provider.
    assert view.company.domain == "engines.example"  # type: ignore[attr-defined]
    codes = {warning["code"] for warning in view.validation.warnings}  # type: ignore[attr-defined]
    assert "public_email_domain" in codes
    assert db_session.scalars(select(Company).where(Company.domain == "gmail.com")).first() is None


def test_a_public_email_with_no_other_company_signal_is_held_for_review(
    db_session: Session,
) -> None:
    campaign = af.make_campaign(db_session)
    result = _confirm(
        db_session,
        campaign.id,
        [
            af.row(
                **{
                    "Email": "ada.lovelace@gmail.com",
                    "Website": "",
                    "Company Linkedin Url": "",
                    "Apollo Account Id": "",
                }
            )
        ],
    )
    assert result.review_required == 1  # type: ignore[attr-defined]
    view = _only_row(db_session, result.batch_id)  # type: ignore[attr-defined]
    assert view.validation.error_code == "public_email_no_company_signal"  # type: ignore[attr-defined]


def test_company_name_alone_never_creates_a_company(db_session: Session) -> None:
    campaign = af.make_campaign(db_session)
    before = db_session.scalar(select(func.count()).select_from(Company))
    result = _confirm(
        db_session,
        campaign.id,
        [
            af.row(
                **{
                    "Website": "",
                    "Company Linkedin Url": "",
                    "Apollo Account Id": "",
                    "Email": "ada@gmail.com",
                }
            )
        ],
    )
    assert result.review_required == 1  # type: ignore[attr-defined]
    assert db_session.scalar(select(func.count()).select_from(Company)) == before


def test_source_identifiers_are_persisted_for_matching_and_never_repointed(
    db_session: Session,
) -> None:
    campaign = af.make_campaign(db_session)
    _confirm(db_session, campaign.id, [af.row()])

    contact_key = db_session.scalars(
        select(ImportSourceIdentifier).where(ImportSourceIdentifier.identifier_kind == "contact_id")
    ).one()
    account_key = db_session.scalars(
        select(ImportSourceIdentifier).where(ImportSourceIdentifier.identifier_kind == "account_id")
    ).one()
    assert contact_key.contact_id is not None and contact_key.company_id is None
    assert account_key.company_id is not None and account_key.contact_id is None
    assert account_key.identifier_value == "apollo-account-1"
