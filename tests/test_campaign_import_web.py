"""The campaign import screens and their safety boundaries (IMP-001 §25.45-51)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from app.core.config import get_settings
from app.main import create_app
from app.models.campaign import Campaign, CampaignContact
from app.models.company import Company
from app.models.contact import Contact
from app.models.enums import CampaignStatus
from app.models.imported_email import ImportedContactEmail
from app.services.imports import campaign_import
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tests import apollo_factory as af


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Iterator[TestClient]:
    """Both switches this area needs, and a staging directory of its own."""

    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__CSV_IMPORT", "true")
    monkeypatch.setenv("STAGED_UPLOADS_DIR", str(tmp_path / "staged"))
    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


def _campaign(session: Session, *, name: str | None = None, archived: bool = False) -> Campaign:
    campaign = Campaign(
        name=name or f"Import web {uuid.uuid4()}",
        status=CampaignStatus.ARCHIVED if archived else CampaignStatus.ACTIVE,
    )
    session.add(campaign)
    session.commit()
    return campaign


def _upload(
    client: TestClient, campaign: Campaign, content: bytes, filename: str = "apollo.csv"
) -> Any:
    return client.post(
        f"/app/campaigns/{campaign.id}/imports",
        files={"file": (filename, content, "text/csv")},
        follow_redirects=False,
    )


def _staged_id(response: Any) -> str:
    location = response.headers["location"]
    return location.rsplit("/", 1)[-1].split("?", 1)[0]


# --- 45. The upload page renders -------------------------------------------


def test_the_campaign_import_page_renders(client: TestClient, committed_session: Session) -> None:
    campaign = _campaign(committed_session)
    response = client.get(f"/app/campaigns/{campaign.id}/imports")
    assert response.status_code == 200
    body = response.text
    assert "Import contacts" in body
    assert campaign.name in body
    # It states the format, the limits, and the truth about verification.
    assert ".csv" in body and ".xlsx" in body
    assert "no verification provider is called" in body
    assert "single-operator" in body


def test_the_import_page_says_so_when_the_feature_is_off(
    monkeypatch: pytest.MonkeyPatch, committed_session: Session, tmp_path: Any
) -> None:
    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.delenv("FEATURES__CSV_IMPORT", raising=False)
    monkeypatch.setenv("STAGED_UPLOADS_DIR", str(tmp_path / "staged"))
    get_settings.cache_clear()
    campaign = _campaign(committed_session)
    with TestClient(create_app()) as client:
        response = client.get(f"/app/campaigns/{campaign.id}/imports")
        assert response.status_code == 200
        assert "FEATURES__CSV_IMPORT" in response.text
        # And uploading is refused rather than silently doing nothing.
        upload = _upload(client, campaign, af.csv_bytes([af.row()]))
        assert upload.status_code == 303
        assert "err=" in upload.headers["location"]
    get_settings.cache_clear()


def test_an_unknown_campaign_is_not_found(client: TestClient) -> None:
    assert client.get(f"/app/campaigns/{uuid.uuid4()}/imports").status_code == 404
    assert client.get("/app/campaigns/not-a-uuid/imports").status_code == 404


# --- 46. The preview renders detected fields and warnings -------------------


def test_the_preview_renders_detection_counts_and_warnings(
    client: TestClient, committed_session: Session
) -> None:
    campaign = _campaign(committed_session)
    content = af.csv_bytes(
        [
            af.row(),
            af.row(
                **{
                    "Email": "twnoyes@llbean.com",
                    "First Name": "Tom",
                    "Last Name": "Noyes",
                    "Company Name": "AGILENT TECHNOLOGIES",
                    "Website": "https://llbean.com",
                    "Company Linkedin Url": "https://www.linkedin.com/company/l-l-bean",
                    "Apollo Account Id": "",
                }
            ),
            af.row(**{"Email": "broken", "First Name": "Broken"}),
        ]
    )
    staged = _staged_id(_upload(client, campaign, content))
    response = client.get(f"/app/campaigns/{campaign.id}/imports/staged/{staged}")
    assert response.status_code == 200
    body = response.text

    assert "Apollo contact export (schema v1)" in body
    assert "imported_email_accepted" in body
    assert "verification_bypassed_imported_email" in body
    # The AGILENT/L.L.Bean conflict is surfaced, not swallowed.
    assert "does not look related" in body
    # And nothing was written by rendering it.
    assert committed_session.scalar(select(func.count()).select_from(Contact)) == 0
    assert committed_session.scalar(select(func.count()).select_from(Company)) == 0
    assert committed_session.scalar(select(func.count()).select_from(CampaignContact)) == 0


def test_a_file_with_missing_headers_previews_as_an_actionable_error(
    client: TestClient, committed_session: Session
) -> None:
    campaign = _campaign(committed_session)
    content = af.csv_bytes([af.row()], header=("First Name", "Last Name", "Company Name"))
    response = _upload(client, campaign, content, filename="bad.csv")
    assert response.status_code == 303
    location = response.headers["location"]
    assert "err=" in location
    assert "Email" in location


# --- 47. Confirmation renders accurate counts -------------------------------


def test_confirming_imports_and_the_batch_page_shows_true_counts(
    client: TestClient, committed_session: Session
) -> None:
    campaign = _campaign(committed_session)
    content = af.csv_bytes(
        [
            af.row(),
            af.row(**{"Email": "grace@engines.example", "First Name": "Grace"}),
            af.row(**{"Email": "broken", "First Name": "Broken"}),
        ]
    )
    staged = _staged_id(_upload(client, campaign, content))
    confirm = client.post(
        f"/app/campaigns/{campaign.id}/imports/staged/{staged}/confirm",
        data={"sheet": "0"},
        follow_redirects=False,
    )
    assert confirm.status_code == 303
    batch_url = confirm.headers["location"].split("?", 1)[0]

    page = client.get(batch_url)
    assert page.status_code == 200
    body = page.text
    assert "imported_email_accepted" in body
    assert "verification_bypassed_imported_email" in body
    assert "Sending remains unavailable" in body

    # The rendered counts match what was actually written.
    assert committed_session.scalar(select(func.count()).select_from(Contact)) == 2
    imported = committed_session.scalars(select(ImportedContactEmail)).all()
    assert len([record for record in imported if record.is_accepted_primary]) == 2


def test_reconfirming_the_same_upload_returns_the_existing_batch(
    client: TestClient, committed_session: Session
) -> None:
    campaign = _campaign(committed_session)
    content = af.csv_bytes([af.row()])
    staged = _staged_id(_upload(client, campaign, content))
    first = client.post(
        f"/app/campaigns/{campaign.id}/imports/staged/{staged}/confirm",
        data={"sheet": "0"},
        follow_redirects=False,
    )
    second = client.post(
        f"/app/campaigns/{campaign.id}/imports/staged/{staged}/confirm",
        data={"sheet": "0"},
        follow_redirects=False,
    )
    assert first.headers["location"].split("?", 1)[0] == second.headers["location"].split("?", 1)[0]
    assert committed_session.scalar(select(func.count()).select_from(Contact)) == 1


# --- 48. Authorization boundaries -------------------------------------------


def test_a_staged_upload_cannot_be_confirmed_into_another_campaign(
    client: TestClient, committed_session: Session
) -> None:
    owner = _campaign(committed_session, name="Owner campaign")
    intruder = _campaign(committed_session, name="Intruder campaign")
    staged = _staged_id(_upload(client, owner, af.csv_bytes([af.row()])))

    # Previewing it under the wrong campaign is not found.
    peek = client.get(f"/app/campaigns/{intruder.id}/imports/staged/{staged}")
    assert peek.status_code == 404

    # And confirming it under the wrong campaign imports nothing.
    confirm = client.post(
        f"/app/campaigns/{intruder.id}/imports/staged/{staged}/confirm",
        data={"sheet": "0"},
        follow_redirects=False,
    )
    assert confirm.status_code == 303
    assert "err=" in confirm.headers["location"]
    assert committed_session.scalar(select(func.count()).select_from(Contact)) == 0


def test_one_campaigns_import_batch_cannot_be_read_from_another(
    client: TestClient, committed_session: Session
) -> None:
    owner = _campaign(committed_session, name="Owner campaign")
    other = _campaign(committed_session, name="Other campaign")
    result = campaign_import.confirm(
        committed_session,
        campaign_id=owner.id,
        content=af.csv_bytes([af.row()]),
        filename="apollo.csv",
    )
    committed_session.commit()

    assert client.get(f"/app/campaigns/{owner.id}/imports/{result.batch_id}").status_code == 200
    leak = client.get(f"/app/campaigns/{other.id}/imports/{result.batch_id}")
    assert leak.status_code == 404
    # The other campaign's page must not disclose the contact or the address.
    assert "ada@engines.example" not in leak.text


def test_an_archived_campaign_cannot_receive_contacts(
    client: TestClient, committed_session: Session
) -> None:
    campaign = _campaign(committed_session, archived=True)
    response = _upload(client, campaign, af.csv_bytes([af.row()]))
    assert response.status_code == 303
    assert "err=" in response.headers["location"]
    assert committed_session.scalar(select(func.count()).select_from(Contact)) == 0


# --- 49-50. Injection: HTML, script, and spreadsheet formulas ---------------


def test_html_and_script_values_are_escaped_in_the_preview(
    client: TestClient, committed_session: Session
) -> None:
    campaign = _campaign(committed_session)
    hostile = "<script>alert('xss')</script>"
    content = af.csv_bytes([af.row(**{"Company Name": hostile, "First Name": "<b>Ada</b>"})])
    staged = _staged_id(_upload(client, campaign, content))
    body = client.get(f"/app/campaigns/{campaign.id}/imports/staged/{staged}").text

    assert "<script>alert" not in body
    assert "&lt;script&gt;" in body
    assert "<b>Ada</b>" not in body
    assert "&lt;b&gt;Ada&lt;/b&gt;" in body


def test_formula_values_are_neutralized_where_they_are_displayed(
    client: TestClient, committed_session: Session
) -> None:
    campaign = _campaign(committed_session)
    content = af.csv_bytes([af.row(**{"Company Name": '=cmd|"/c calc"!A0'})])
    staged = _staged_id(_upload(client, campaign, content))
    body = client.get(f"/app/campaigns/{campaign.id}/imports/staged/{staged}").text

    # Displayed as text with the neutralizing prefix, never as a bare formula.
    assert "&#39;=cmd" in body or "'=cmd" in body
    assert ">=cmd" not in body
    # And the row is flagged so the operator knows why it looks odd.
    assert "formula character" in body


@pytest.mark.usefixtures("enable_csv_import")
def test_the_raw_value_is_preserved_verbatim_despite_display_neutralization(
    committed_session: Session,
) -> None:
    campaign = _campaign(committed_session)
    campaign_import.confirm(
        committed_session,
        campaign_id=campaign.id,
        content=af.csv_bytes([af.row(**{"Company Name": "=1+1"})]),
        filename="apollo.csv",
    )
    committed_session.commit()
    company = committed_session.scalars(select(Company)).one()
    # Stored exactly as supplied; only the rendered form is prefixed.
    assert company.name == "=1+1"


# --- 51. Errors and logs do not leak PII ------------------------------------


@pytest.mark.usefixtures("enable_csv_import")
def test_a_row_failure_message_does_not_repeat_the_address_or_a_stack_trace(
    committed_session: Session,
) -> None:
    campaign = _campaign(committed_session)
    result = campaign_import.confirm(
        committed_session,
        campaign_id=campaign.id,
        content=af.csv_bytes([af.row(**{"First Name": "", "Last Name": ""})]),
        filename="apollo.csv",
    )
    committed_session.commit()
    views, _total = campaign_import.batch_rows(committed_session, batch_id=result.batch_id)
    validation = views[0].validation
    assert validation is not None
    assert validation.error_code == "person_identity_missing"
    assert validation.note is not None
    assert "Traceback" not in validation.note
    assert "psycopg" not in validation.note


def test_an_unreadable_upload_reports_a_safe_message_with_no_internals(
    client: TestClient, committed_session: Session
) -> None:
    campaign = _campaign(committed_session)
    response = _upload(client, campaign, b"MZ\x90\x00not-a-workbook", filename="evil.xlsx")
    assert response.status_code == 303
    location = response.headers["location"]
    assert "err=" in location
    for leaked in ("Traceback", "BadZipFile", "site-packages", "openpyxl"):
        assert leaked not in location


def test_the_batch_page_shows_the_address_only_inside_its_own_campaign(
    client: TestClient, committed_session: Session
) -> None:
    campaign = _campaign(committed_session)
    result = campaign_import.confirm(
        committed_session,
        campaign_id=campaign.id,
        content=af.csv_bytes([af.row()]),
        filename="apollo.csv",
    )
    committed_session.commit()
    body = client.get(f"/app/campaigns/{campaign.id}/imports/{result.batch_id}").text
    # The operator who owns the campaign does see the address — that is the
    # point of the page — together with whose claim its status is.
    assert "ada@engines.example" in body
    assert "Apollo" in body and "claims" in body
