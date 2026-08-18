"""The Google Sheets add-on seam, proved at the boundary it actually has.

Every test here goes through the HTTP surface with a stubbed identity verifier,
because that is the shape of the risk: the add-on is an unauthenticated caller on
the public internet until its assertion is checked, and the thing worth proving
is what happens on the far side of that check.

The verifier is stubbed rather than mocked out. It still has to be *presented*
with a token, and the stub still refuses one it does not know, so every test
below carries a real credential decision — it is only Google's signature that is
replaced, and `tests/test_hosted_auth.py` already owns that boundary.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from app.api.deps import get_db
from app.api.integrations_sheets import require_account
from app.api.integrations_sheets import router as sheets_router
from app.core.auth.identity import IdentityAssertionError
from app.core.auth.sheets_assertion import (
    DEFAULT_ACCEPTED_ISSUERS,
    VerifiedAssertion,
    bearer_token,
    validate_assertion_claims,
)
from app.core.config import get_settings
from app.main import create_app
from app.models.campaign import Campaign, CampaignContact
from app.models.company import Company
from app.models.company_domain_resolution import CompanyDomainResolution
from app.models.contact import Contact
from app.models.email_evidence import ExactEmailVerification
from app.models.enums import (
    AgentIdentifier,
    CampaignStatus,
    ContactWorkflowState,
    EmailVerificationResult,
    PipelineStageStatus,
    SuppressionReason,
    SuppressionType,
    UserRole,
    UserState,
)
from app.models.pipeline import (
    CampaignContactAgentState,
    CampaignContactSource,
    PipelineEvent,
)
from app.models.suppression import Suppression
from app.models.user import User
from app.models.verification_job import AgentJob
from app.services import campaign_contacts
from app.services.enrichment import logodev
from app.services.integrations.sheets import submit as sheet_submit
from app.services.integrations.sheets.contract import (
    RowStatus,
    SheetLocation,
    batch_id,
    parse_row,
    row_idempotency_key,
)
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.gmail_factory import build_sequence

INSTALLATION = "install-0001"
SPREADSHEET = "sheet-abc"
TAB = "0"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class StubVerifier:
    """A Google assertion verifier that knows a fixed set of tokens."""

    def __init__(self, tokens: dict[str, VerifiedAssertion]) -> None:
        self._tokens = tokens

    async def verify(self, token: str) -> VerifiedAssertion:
        try:
            return self._tokens[token]
        except KeyError as exc:
            raise IdentityAssertionError("identity assertion does not verify") from exc


def assertion_for(user: User, *, token_subject: str | None = None) -> VerifiedAssertion:
    return VerifiedAssertion(
        subject=token_subject or f"google-{user.email_normalized}",
        email=user.email_normalized,
        display_name=user.display_name or "Operator",
        audience="add-on-client-id.apps.googleusercontent.com",
    )


@pytest.fixture()
def enable_sheets(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("FEATURES__GOOGLE_SHEETS_INTEGRATION", "true")
    # Required by the surface's own capability gate: a Ready row is a verified
    # address *and* a validated seven-message sequence, so the integration is
    # unavailable — not merely limited — while sequences are off.
    monkeypatch.setenv("FEATURES__EMAIL_SEQUENCES", "true")
    monkeypatch.setenv(
        "SHEETS__ALLOWED_AUDIENCES", '["add-on-client-id.apps.googleusercontent.com"]'
    )
    monkeypatch.setenv("SHEETS__MAX_BATCH_ROWS", "5")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def make_user(
    db: Session,
    *,
    email: str,
    role: UserRole = UserRole.USER,
    state: UserState = UserState.ACTIVE,
) -> User:
    user = User(
        email=email,
        email_normalized=email.lower(),
        display_name=email.split("@")[0],
        role=role,
        state=state,
        google_subject=f"google-{email.lower()}",
    )
    db.add(user)
    db.flush()
    return user


def make_campaign(db: Session, *, name: str, owner: User | None = None) -> Campaign:
    campaign = Campaign(
        name=name,
        description="Sheets integration",
        status=CampaignStatus.ACTIVE,
        created_by_user_id=owner.id if owner is not None else None,
    )
    db.add(campaign)
    db.flush()
    return campaign


def make_client(db: Session, tokens: dict[str, VerifiedAssertion]) -> TestClient:
    app = create_app(sheets_assertion_verifier=StubVerifier(tokens))

    def _override() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


def row(
    client_row_id: str,
    *,
    first: str = "Ada",
    last: str = "Lovelace",
    company: str = "Kiln Systems",
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "client_row_id": client_row_id,
        "first_name": first,
        "last_name": last,
        "company_name": company,
    }
    payload.update(extra)
    return payload


def batch_payload(campaign: Campaign, rows: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "campaign_id": str(campaign.id),
        "installation_id": INSTALLATION,
        "spreadsheet_id": SPREADSHEET,
        "sheet_id": TAB,
        "rows": rows,
    }
    payload.update(extra)
    return payload


def seed_company(db: Session, *, name: str, domain: str) -> Company:
    """An established permanent Company: the evidence a name resolves against.

    Created without a domain-resolution decision, which is exactly what an
    imported or hand-entered Company looks like, and is what the policy treats as
    established evidence.
    """

    company = Company(name=name, domain=domain)
    db.add(company)
    db.flush()
    return company


# ---------------------------------------------------------------------------
# 1-2. Campaign visibility and authorization
# ---------------------------------------------------------------------------


def test_an_account_sees_only_the_campaigns_it_may_reach(
    db_session: Session, enable_sheets: None
) -> None:
    mine = make_user(db_session, email="mine@vmr.example")
    theirs = make_user(db_session, email="theirs@vmr.example")
    ours = make_campaign(db_session, name="Mine", owner=mine)
    make_campaign(db_session, name="Theirs", owner=theirs)

    client = make_client(db_session, {"tok": assertion_for(mine)})
    response = client.get("/integrations/sheets/campaigns", headers={"Authorization": "Bearer tok"})

    assert response.status_code == 200
    body = response.json()
    assert [item["name"] for item in body["campaigns"]] == ["Mine"]
    assert body["campaigns"][0]["id"] == str(ours.id)
    assert body["limits"]["max_batch_rows"] == 5


def test_submitting_into_another_accounts_campaign_is_refused(
    db_session: Session, enable_sheets: None
) -> None:
    mine = make_user(db_session, email="mine@vmr.example")
    theirs = make_user(db_session, email="theirs@vmr.example")
    not_mine = make_campaign(db_session, name="Theirs", owner=theirs)
    seed_company(db_session, name="Kiln Systems", domain="kiln.example")

    client = make_client(db_session, {"tok": assertion_for(mine)})
    response = client.post(
        "/integrations/sheets/batches",
        json=batch_payload(not_mine, [row("r1")]),
        headers={"Authorization": "Bearer tok"},
    )

    assert response.status_code == 403
    assert db_session.query(CampaignContact).count() == 0


def test_an_unknown_campaign_answers_the_same_as_someone_elses(
    db_session: Session, enable_sheets: None
) -> None:
    """Refusals must not distinguish "no such Campaign" from "not yours"."""

    mine = make_user(db_session, email="mine@vmr.example")
    client = make_client(db_session, {"tok": assertion_for(mine)})
    payload = {
        "campaign_id": str(uuid.uuid4()),
        "installation_id": INSTALLATION,
        "spreadsheet_id": SPREADSHEET,
        "sheet_id": TAB,
        "rows": [row("r1")],
    }
    response = client.post(
        "/integrations/sheets/batches", json=payload, headers={"Authorization": "Bearer tok"}
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# 3-5. Accepting rows
# ---------------------------------------------------------------------------


def test_a_valid_batch_creates_a_contact_and_a_campaign_membership(
    db_session: Session, enable_sheets: None
) -> None:
    user = make_user(db_session, email="mine@vmr.example")
    campaign = make_campaign(db_session, name="Mine", owner=user)
    seed_company(db_session, name="Kiln Systems", domain="kiln.example")

    client = make_client(db_session, {"tok": assertion_for(user)})
    response = client.post(
        "/integrations/sheets/batches",
        json=batch_payload(campaign, [row("r1")]),
        headers={"Authorization": "Bearer tok"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["counts"] == {"submitted": 1, "accepted": 1, "could_not_prepare": 0}
    entry = body["rows"][0]
    assert entry["client_row_id"] == "r1"
    assert entry["status"] == RowStatus.PENDING.value
    assert entry["submission_id"]

    membership = db_session.get(CampaignContact, uuid.UUID(entry["submission_id"]))
    assert membership is not None
    assert membership.campaign_id == campaign.id
    contact = db_session.get(Contact, membership.contact_id)
    assert contact is not None
    assert (contact.first_name, contact.last_name) == ("Ada", "Lovelace")
    assert contact.company_domain == "kiln.example"
    assert contact.company_id is not None


@pytest.mark.parametrize(
    "missing",
    ["first_name", "last_name", "company_name"],
)
def test_a_row_missing_a_required_field_is_refused_without_stopping_the_batch(
    db_session: Session, enable_sheets: None, missing: str
) -> None:
    user = make_user(db_session, email="mine@vmr.example")
    campaign = make_campaign(db_session, name="Mine", owner=user)
    seed_company(db_session, name="Kiln Systems", domain="kiln.example")

    broken = row("bad")
    broken[missing] = "   "

    client = make_client(db_session, {"tok": assertion_for(user)})
    response = client.post(
        "/integrations/sheets/batches",
        json=batch_payload(campaign, [broken, row("good")]),
        headers={"Authorization": "Bearer tok"},
    )

    assert response.status_code == 200
    rows = {entry["client_row_id"]: entry for entry in response.json()["rows"]}
    assert rows["bad"]["status"] == RowStatus.COULD_NOT_PREPARE.value
    assert rows["bad"]["failure_code"] == "missing_required_field"
    assert rows["bad"]["submission_id"] is None
    # The healthy row in the same request is unaffected: one bad cell must never
    # cost the operator the rest of their selection.
    assert rows["good"]["status"] == RowStatus.PENDING.value
    assert rows["good"]["submission_id"]


def test_optional_fields_are_accepted_and_recorded_as_operator_supplied(
    db_session: Session, enable_sheets: None
) -> None:
    user = make_user(db_session, email="mine@vmr.example")
    campaign = make_campaign(db_session, name="Mine", owner=user)
    seed_company(db_session, name="Kiln Systems", domain="kiln.example")

    client = make_client(db_session, {"tok": assertion_for(user)})
    response = client.post(
        "/integrations/sheets/batches",
        json=batch_payload(
            campaign,
            [
                row(
                    "r1",
                    job_title="Head of Research",
                    linkedin_url="https://www.linkedin.com/in/ada-lovelace/",
                    context="Spoke at the process-control summit in March.",
                )
            ],
        ),
        headers={"Authorization": "Bearer tok"},
    )

    assert response.status_code == 200
    entry = response.json()["rows"][0]
    assert entry["status"] == RowStatus.PENDING.value

    membership = db_session.get(CampaignContact, uuid.UUID(entry["submission_id"]))
    assert membership is not None
    contact = db_session.get(Contact, membership.contact_id)
    assert contact is not None
    assert contact.title == "Head of Research"
    assert contact.linkedin_url == "https://www.linkedin.com/in/ada-lovelace/"

    source = membership.sources[0] if getattr(membership, "sources", None) else None
    if source is None:
        from app.models.pipeline import CampaignContactSource

        source = (
            db_session.query(CampaignContactSource)
            .filter(CampaignContactSource.campaign_contact_id == membership.id)
            .one()
        )
    context = source.source_context["operator_supplied_context"]
    # The label travels with the value. A sentence typed into a spreadsheet must
    # never be readable later as something the system established.
    assert context["kind"] == "operator_supplied"
    assert context["verified"] is False
    assert context["text"].startswith("Spoke at the process-control summit")


def test_an_empty_optional_field_never_stops_a_row(
    db_session: Session, enable_sheets: None
) -> None:
    user = make_user(db_session, email="mine@vmr.example")
    campaign = make_campaign(db_session, name="Mine", owner=user)
    seed_company(db_session, name="Kiln Systems", domain="kiln.example")

    client = make_client(db_session, {"tok": assertion_for(user)})
    response = client.post(
        "/integrations/sheets/batches",
        json=batch_payload(
            campaign,
            [row("r1", job_title="", linkedin_url="", context="")],
        ),
        headers={"Authorization": "Bearer tok"},
    )
    assert response.json()["rows"][0]["status"] == RowStatus.PENDING.value


# ---------------------------------------------------------------------------
# 6-9. Idempotency, row mapping, reuse
# ---------------------------------------------------------------------------


def test_submitting_the_same_rows_twice_creates_nothing_the_second_time(
    db_session: Session, enable_sheets: None
) -> None:
    user = make_user(db_session, email="mine@vmr.example")
    campaign = make_campaign(db_session, name="Mine", owner=user)
    seed_company(db_session, name="Kiln Systems", domain="kiln.example")

    client = make_client(db_session, {"tok": assertion_for(user)})
    payload = batch_payload(campaign, [row("r1"), row("r2", first="Grace", last="Hopper")])
    first = client.post(
        "/integrations/sheets/batches", json=payload, headers={"Authorization": "Bearer tok"}
    ).json()
    second = client.post(
        "/integrations/sheets/batches", json=payload, headers={"Authorization": "Bearer tok"}
    ).json()

    assert first["batch_id"] == second["batch_id"]
    assert [r["submission_id"] for r in first["rows"]] == [
        r["submission_id"] for r in second["rows"]
    ]
    assert all(entry["already_submitted"] for entry in second["rows"])
    assert db_session.query(CampaignContact).count() == 2
    assert db_session.query(Contact).count() == 2


def test_a_row_keeps_its_identifier_when_the_sheet_is_reordered(
    db_session: Session, enable_sheets: None
) -> None:
    """Position is not identity. Sorting the sheet must change nothing."""

    user = make_user(db_session, email="mine@vmr.example")
    campaign = make_campaign(db_session, name="Mine", owner=user)
    seed_company(db_session, name="Kiln Systems", domain="kiln.example")

    client = make_client(db_session, {"tok": assertion_for(user)})
    ordered = [row("r1"), row("r2", first="Grace", last="Hopper")]
    first = client.post(
        "/integrations/sheets/batches",
        json=batch_payload(campaign, ordered),
        headers={"Authorization": "Bearer tok"},
    ).json()
    mapping = {entry["client_row_id"]: entry["submission_id"] for entry in first["rows"]}

    reversed_rows = list(reversed(ordered))
    second = client.post(
        "/integrations/sheets/batches",
        json=batch_payload(campaign, reversed_rows),
        headers={"Authorization": "Bearer tok"},
    ).json()

    assert [entry["client_row_id"] for entry in second["rows"]] == ["r2", "r1"]
    for entry in second["rows"]:
        assert entry["submission_id"] == mapping[entry["client_row_id"]]


def test_a_new_generation_is_a_deliberate_second_submission(
    db_session: Session, enable_sheets: None
) -> None:
    """The escape hatch, and the proof that it is not the default.

    Generation is how an operator asks for the same row again on purpose. It
    changes every derived key, so the same row in the same tab reaches the same
    membership through a second provenance record rather than a second Contact.
    """

    user = make_user(db_session, email="mine@vmr.example")
    campaign = make_campaign(db_session, name="Mine", owner=user)
    seed_company(db_session, name="Kiln Systems", domain="kiln.example")

    client = make_client(db_session, {"tok": assertion_for(user)})
    first = client.post(
        "/integrations/sheets/batches",
        json=batch_payload(campaign, [row("r1")]),
        headers={"Authorization": "Bearer tok"},
    ).json()
    second = client.post(
        "/integrations/sheets/batches",
        json=batch_payload(campaign, [row("r1")], generation=2),
        headers={"Authorization": "Bearer tok"},
    ).json()

    assert first["batch_id"] != second["batch_id"]
    assert first["rows"][0]["submission_id"] == second["rows"][0]["submission_id"]
    assert db_session.query(Contact).count() == 1


def test_an_existing_permanent_contact_is_reused_and_never_overwritten(
    db_session: Session, enable_sheets: None
) -> None:
    user = make_user(db_session, email="mine@vmr.example")
    campaign = make_campaign(db_session, name="Mine", owner=user)
    company = seed_company(db_session, name="Kiln Systems", domain="kiln.example")
    existing = Contact(
        first_name="Ada",
        last_name="Lovelace",
        company_name="Kiln Systems",
        company_domain="kiln.example",
        company_id=company.id,
        title="Chief Scientist",
        natural_key="ada|lovelace|kiln.example",
    )
    db_session.add(existing)
    db_session.flush()

    client = make_client(db_session, {"tok": assertion_for(user)})
    response = client.post(
        "/integrations/sheets/batches",
        json=batch_payload(campaign, [row("r1", job_title="Head of Research")]),
        headers={"Authorization": "Bearer tok"},
    )

    entry = response.json()["rows"][0]
    assert entry["contact_id"] == str(existing.id)
    assert db_session.query(Contact).count() == 1
    db_session.refresh(existing)
    # A spreadsheet fills a blank and replaces nothing.
    assert existing.title == "Chief Scientist"


def test_an_existing_campaign_membership_is_reused_rather_than_duplicated(
    db_session: Session, enable_sheets: None
) -> None:
    user = make_user(db_session, email="mine@vmr.example")
    campaign = make_campaign(db_session, name="Mine", owner=user)
    company = seed_company(db_session, name="Kiln Systems", domain="kiln.example")
    contact = Contact(
        first_name="Ada",
        last_name="Lovelace",
        company_name="Kiln Systems",
        company_domain="kiln.example",
        company_id=company.id,
        natural_key="ada|lovelace|kiln.example",
    )
    db_session.add(contact)
    db_session.flush()
    # Enrolled the way the operator product enrols, so the membership carries the
    # product's own desired stage rather than one this test invented.
    membership = campaign_contacts.enrol_contact(
        db_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        actor="operator",
        enqueue=False,
    ).membership

    client = make_client(db_session, {"tok": assertion_for(user)})
    response = client.post(
        "/integrations/sheets/batches",
        json=batch_payload(campaign, [row("r1")]),
        headers={"Authorization": "Bearer tok"},
    )

    entry = response.json()["rows"][0]
    assert entry["submission_id"] == str(membership.id)
    assert db_session.query(CampaignContact).count() == 1


# ---------------------------------------------------------------------------
# 10. No domain in the sheet
# ---------------------------------------------------------------------------


def test_a_row_with_no_domain_resolves_through_the_normal_company_path(
    db_session: Session, enable_sheets: None
) -> None:
    """The sheet never supplies a domain, and never has to."""

    user = make_user(db_session, email="mine@vmr.example")
    campaign = make_campaign(db_session, name="Mine", owner=user)
    company = seed_company(db_session, name="Kiln Systems", domain="kiln.example")

    client = make_client(db_session, {"tok": assertion_for(user)})
    response = client.post(
        "/integrations/sheets/batches",
        json=batch_payload(campaign, [row("r1", company="  kiln   systems ")]),
        headers={"Authorization": "Bearer tok"},
    )

    entry = response.json()["rows"][0]
    membership = db_session.get(CampaignContact, uuid.UUID(entry["submission_id"]))
    assert membership is not None
    contact = db_session.get(Contact, membership.contact_id)
    assert contact is not None
    assert contact.company_id == company.id
    assert contact.company_domain == "kiln.example"


def test_an_unseen_company_enters_the_pipeline_instead_of_being_refused(
    db_session: Session, enable_sheets: None
) -> None:
    """The repair, stated as the behaviour that used to be impossible.

    A company this deployment has never established is not a reason to refuse a
    row. The Contact becomes permanent carrying the name it was given, the
    membership is created, and the pipeline owns the rest. This is the exact row
    shape that came back "could not prepare" in hosted UAT.
    """

    user = make_user(db_session, email="mine@vmr.example")
    campaign = make_campaign(db_session, name="Mine", owner=user)

    client = make_client(db_session, {"tok": assertion_for(user)})
    response = client.post(
        "/integrations/sheets/batches",
        json=batch_payload(campaign, [row("r1", company="A Company Nobody Has Heard Of")]),
        headers={"Authorization": "Bearer tok"},
    )

    entry = response.json()["rows"][0]
    assert entry["status"] == RowStatus.PENDING.value
    assert entry["failure_code"] is None
    assert entry["safe_failure_reason"] is None
    assert entry["submission_id"] is not None
    assert entry["contact_id"] is not None
    assert response.json()["counts"]["accepted"] == 1
    assert response.json()["counts"]["could_not_prepare"] == 0

    contact = db_session.get(Contact, uuid.UUID(entry["contact_id"]))
    assert contact is not None
    # The evidence the sheet supplied is preserved verbatim...
    assert contact.company_name == "A Company Nobody Has Heard Of"
    # ...and the link the sheet could not honestly establish is left NULL, which
    # the Contact model documents as "not linked yet" rather than guessed.
    assert contact.company_domain is None
    assert contact.company_id is None
    assert contact.natural_key is None

    membership = db_session.get(CampaignContact, uuid.UUID(entry["submission_id"]))
    assert membership is not None
    assert membership.contact_id == contact.id


def test_an_unseen_company_makes_no_provider_call_at_intake(
    db_session: Session, enable_sheets: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deciding whether a row may enter must never spend money.

    The brand matcher is replaced with a landmine rather than a spy: a call is
    not merely counted, it fails the test where it happens. Any provider work an
    unseen company needs belongs to the Agent that owns it, long after this
    request has returned.
    """

    def _explode(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("intake asked a provider whether a row may enter")

    monkeypatch.setattr(logodev, "search_brands", _explode)

    user = make_user(db_session, email="mine@vmr.example")
    campaign = make_campaign(db_session, name="Mine", owner=user)

    client = make_client(db_session, {"tok": assertion_for(user)})
    response = client.post(
        "/integrations/sheets/batches",
        json=batch_payload(campaign, [row("r1", company="Still Nobody Has Heard Of It")]),
        headers={"Authorization": "Bearer tok"},
    )

    assert response.json()["rows"][0]["status"] == RowStatus.PENDING.value


def test_an_established_company_also_makes_no_provider_call(
    db_session: Session, enable_sheets: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The known-company path stays free, and stays linked."""

    def _explode(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("intake asked a provider about an established company")

    monkeypatch.setattr(logodev, "search_brands", _explode)

    user = make_user(db_session, email="mine@vmr.example")
    campaign = make_campaign(db_session, name="Mine", owner=user)
    company = seed_company(db_session, name="Kiln Systems", domain="kiln.example")

    client = make_client(db_session, {"tok": assertion_for(user)})
    response = client.post(
        "/integrations/sheets/batches",
        json=batch_payload(campaign, [row("r1", company="  kiln   systems ")]),
        headers={"Authorization": "Bearer tok"},
    )

    entry = response.json()["rows"][0]
    assert entry["status"] == RowStatus.PENDING.value
    contact = db_session.get(Contact, uuid.UUID(entry["contact_id"]))
    assert contact is not None
    assert contact.company_id == company.id
    assert contact.company_domain == "kiln.example"
    assert contact.natural_key is not None


def test_an_unseen_company_starts_at_the_canonical_first_stage(
    db_session: Session, enable_sheets: None
) -> None:
    """The precise existing invariant, not "a job exists".

    ``initialize_pipeline`` is documented as "Capture as complete and Identity as
    the first queued stage". A Sheets enrolment must land exactly there — the
    same place a direct enrolment lands — so the assertion below is made against
    a non-Sheets membership rather than against a literal.
    """

    user = make_user(db_session, email="mine@vmr.example")
    campaign = make_campaign(db_session, name="Mine", owner=user)

    client = make_client(db_session, {"tok": assertion_for(user)})
    response = client.post(
        "/integrations/sheets/batches",
        json=batch_payload(campaign, [row("r1", company="An Unseen Company")]),
        headers={"Authorization": "Bearer tok"},
    )
    entry = response.json()["rows"][0]
    sheets_membership = db_session.get(CampaignContact, uuid.UUID(entry["submission_id"]))
    assert sheets_membership is not None

    # An equivalent enrolment that never went near a spreadsheet.
    reference = Contact(first_name="Grace", last_name="Hopper", company_name="An Unseen Company")
    db_session.add(reference)
    db_session.flush()
    direct = campaign_contacts.enrol_contact(
        db_session,
        campaign_id=campaign.id,
        contact_id=reference.id,
        source_type="manual",
    ).membership

    assert sheets_membership.next_stage is direct.next_stage
    assert sheets_membership.current_stage is direct.current_stage
    assert sheets_membership.pipeline_status is direct.pipeline_status
    # Stated absolutely as well, so a change to both paths at once is still caught.
    assert sheets_membership.next_stage is AgentIdentifier.IDENTITY

    capture = db_session.scalars(
        select(CampaignContactAgentState).where(
            CampaignContactAgentState.campaign_contact_id == sheets_membership.id,
            CampaignContactAgentState.agent_id == AgentIdentifier.CAPTURE,
        )
    ).one()
    assert capture.status is PipelineStageStatus.COMPLETED


def test_an_unseen_company_launders_no_domain(db_session: Session, enable_sheets: None) -> None:
    """Accepting the row must not invent, confirm or record a company identity.

    The old refusal existed to prevent exactly this. The repair honours it by
    obtaining nothing uncertain rather than by grading it: no Company row, no
    domain on the Contact, and no resolution decision claiming otherwise.
    """

    user = make_user(db_session, email="mine@vmr.example")
    campaign = make_campaign(db_session, name="Mine", owner=user)
    companies_before = db_session.query(Company).count()

    client = make_client(db_session, {"tok": assertion_for(user)})
    response = client.post(
        "/integrations/sheets/batches",
        json=batch_payload(campaign, [row("r1", company="Nobody Established This")]),
        headers={"Authorization": "Bearer tok"},
    )

    entry = response.json()["rows"][0]
    assert entry["status"] == RowStatus.PENDING.value
    assert db_session.query(Company).count() == companies_before
    assert db_session.query(CompanyDomainResolution).count() == 0
    contact = db_session.get(Contact, uuid.UUID(entry["contact_id"]))
    assert contact is not None
    assert contact.company_domain is None


def test_retrying_an_unseen_company_row_is_idempotent(
    db_session: Session, enable_sheets: None
) -> None:
    """The new accepted path keeps the old idempotency contract."""

    user = make_user(db_session, email="mine@vmr.example")
    campaign = make_campaign(db_session, name="Mine", owner=user)
    client = make_client(db_session, {"tok": assertion_for(user)})
    payload = batch_payload(campaign, [row("r1", company="Unseen And Retried")])

    first = client.post(
        "/integrations/sheets/batches", json=payload, headers={"Authorization": "Bearer tok"}
    ).json()["rows"][0]
    second = client.post(
        "/integrations/sheets/batches", json=payload, headers={"Authorization": "Bearer tok"}
    ).json()["rows"][0]

    assert first["submission_id"] == second["submission_id"]
    assert second["already_submitted"] is True
    assert second["status"] == RowStatus.PENDING.value
    assert db_session.query(Contact).count() == 1
    assert db_session.query(CampaignContact).count() == 1


def test_an_unseen_company_row_reads_back_as_processing(
    db_session: Session, enable_sheets: None
) -> None:
    """The results contract is unchanged: an accepted row is in progress.

    It is not Ready — no address and no sequence exist yet — and it is not a
    failure. That is the state the sheet should show while the pipeline works.
    """

    user = make_user(db_session, email="mine@vmr.example")
    campaign = make_campaign(db_session, name="Mine", owner=user)
    client = make_client(db_session, {"tok": assertion_for(user)})

    submitted = client.post(
        "/integrations/sheets/batches",
        json=batch_payload(campaign, [row("r1", company="Unseen But Readable")]),
        headers={"Authorization": "Bearer tok"},
    ).json()["rows"][0]

    results = client.post(
        "/integrations/sheets/results",
        json={"submission_ids": [submitted["submission_id"]]},
        headers={"Authorization": "Bearer tok"},
    ).json()["rows"][0]

    assert results["status"] in {RowStatus.PENDING.value, RowStatus.PROCESSING.value}
    assert results["email_address"] is None
    assert results.get("messages") in (None, [])


def test_a_suppressed_identity_creates_nothing(db_session: Session, enable_sheets: None) -> None:
    user = make_user(db_session, email="mine@vmr.example")
    campaign = make_campaign(db_session, name="Mine", owner=user)
    seed_company(db_session, name="Kiln Systems", domain="kiln.example")
    db_session.add(
        Suppression(
            suppression_type=SuppressionType.DOMAIN,
            value="kiln.example",
            reason=SuppressionReason.MANUAL,
            source="test",
        )
    )
    db_session.flush()

    client = make_client(db_session, {"tok": assertion_for(user)})
    response = client.post(
        "/integrations/sheets/batches",
        json=batch_payload(campaign, [row("r1")]),
        headers={"Authorization": "Bearer tok"},
    )

    entry = response.json()["rows"][0]
    assert entry["status"] == RowStatus.COULD_NOT_PREPARE.value
    assert entry["failure_code"] == "suppressed"
    assert db_session.query(Contact).count() == 0
    assert db_session.query(Suppression).count() == 1


# ---------------------------------------------------------------------------
# 11-14. Reading results
# ---------------------------------------------------------------------------


def test_results_report_processing_before_the_pipeline_finishes(
    db_session: Session, enable_sheets: None
) -> None:
    user = make_user(db_session, email="mine@vmr.example")
    fixture = build_sequence(db_session, owner_user_id=user.id)
    # A live sequence but no verification evidence: the address is not usable, so
    # the row is not ready however finished the messages look.
    client = make_client(db_session, {"tok": assertion_for(user)})
    response = client.post(
        "/integrations/sheets/results",
        json={"submission_ids": [str(fixture.membership.id)]},
        headers={"Authorization": "Bearer tok"},
    )

    assert response.status_code == 200
    entry = response.json()["rows"][0]
    assert entry["status"] in {RowStatus.PENDING.value, RowStatus.PROCESSING.value}
    assert entry["email_address"] is None
    assert "messages" not in entry


def test_a_ready_row_returns_the_verified_address_and_exactly_seven_messages(
    db_session: Session, enable_sheets: None
) -> None:
    user = make_user(db_session, email="mine@vmr.example")
    fixture = build_sequence(db_session, email="ada@kiln.example", owner_user_id=user.id)
    db_session.add(
        ExactEmailVerification(
            email="ada@kiln.example",
            result=EmailVerificationResult.VALID,
            provider="millionverifier",
            policy_version="ver-1",
            checked_at=datetime.now(UTC) - timedelta(hours=1),
            contact_id=fixture.contact.id,
        )
    )
    db_session.flush()

    client = make_client(db_session, {"tok": assertion_for(user)})
    entry = client.post(
        "/integrations/sheets/results",
        json={"submission_ids": [str(fixture.membership.id)]},
        headers={"Authorization": "Bearer tok"},
    ).json()["rows"][0]

    assert entry["status"] == RowStatus.READY.value
    assert entry["email_address"] == "ada@kiln.example"
    assert len(entry["messages"]) == 7
    assert [m["sequence_index"] for m in entry["messages"]] == [1, 2, 3, 4, 5, 6, 7]
    assert [m["elapsed_day"] for m in entry["messages"]] == [0, 3, 7, 12, 18, 25, 35]
    assert all(m["subject"] and m["body"] for m in entry["messages"])


def test_a_ready_row_needs_no_human_approval(db_session: Session, enable_sheets: None) -> None:
    """No review decision exists on this fixture, and the row is still ready.

    Stated as its own test because the operator product deliberately requires an
    approval before a Gmail draft. This surface produces text for a person to use
    and creates no draft, no schedule and no send, so gating it on an approval
    would add a ceremony that protects nothing.
    """

    from app.models.email_sequence import EmailSequenceMessageReview

    user = make_user(db_session, email="mine@vmr.example")
    fixture = build_sequence(db_session, email="ada@kiln.example", owner_user_id=user.id)
    db_session.add(
        ExactEmailVerification(
            email="ada@kiln.example",
            result=EmailVerificationResult.VALID,
            provider="millionverifier",
            policy_version="ver-1",
            checked_at=datetime.now(UTC),
            contact_id=fixture.contact.id,
        )
    )
    db_session.flush()

    assert db_session.query(EmailSequenceMessageReview).count() == 0
    client = make_client(db_session, {"tok": assertion_for(user)})
    entry = client.post(
        "/integrations/sheets/results",
        json={"submission_ids": [str(fixture.membership.id)]},
        headers={"Authorization": "Bearer tok"},
    ).json()["rows"][0]
    assert entry["status"] == RowStatus.READY.value


def test_a_stopped_row_returns_a_safe_reason_and_no_messages(
    db_session: Session, enable_sheets: None
) -> None:
    user = make_user(db_session, email="mine@vmr.example")
    fixture = build_sequence(db_session, email="ada@kiln.example", owner_user_id=user.id)
    fixture.membership.state = ContactWorkflowState.SUPPRESSED
    fixture.membership.pipeline_status = PipelineStageStatus.BLOCKED
    db_session.flush()

    client = make_client(db_session, {"tok": assertion_for(user)})
    entry = client.post(
        "/integrations/sheets/results",
        json={"submission_ids": [str(fixture.membership.id)]},
        headers={"Authorization": "Bearer tok"},
    ).json()["rows"][0]

    assert entry["status"] == RowStatus.COULD_NOT_PREPARE.value
    assert "suppression list" in entry["safe_failure_reason"]
    assert "messages" not in entry


def test_a_provider_secret_can_never_reach_a_spreadsheet_cell(
    db_session: Session, enable_sheets: None
) -> None:
    """The failure text is sanitized, because it is written into a shared file."""

    user = make_user(db_session, email="mine@vmr.example")
    fixture = build_sequence(db_session, owner_user_id=user.id)
    fixture.membership.state = ContactWorkflowState.EXCLUDED
    fixture.membership.blocking_reasons = [
        {"code": "provider_error", "detail": "call failed with api_key=sk_live_abcdef123456"}
    ]
    db_session.flush()

    client = make_client(db_session, {"tok": assertion_for(user)})
    entry = client.post(
        "/integrations/sheets/results",
        json={"submission_ids": [str(fixture.membership.id)]},
        headers={"Authorization": "Bearer tok"},
    ).json()["rows"][0]

    assert entry["status"] == RowStatus.COULD_NOT_PREPARE.value
    assert "sk_live" not in entry["safe_failure_reason"]
    assert "[redacted]" in entry["safe_failure_reason"]


# ---------------------------------------------------------------------------
# 15-17. Credential and ceiling boundaries
# ---------------------------------------------------------------------------


def test_one_account_cannot_read_another_accounts_rows(
    db_session: Session, enable_sheets: None
) -> None:
    owner = make_user(db_session, email="owner@vmr.example")
    intruder = make_user(db_session, email="intruder@vmr.example")
    fixture = build_sequence(db_session, email="ada@kiln.example", owner_user_id=owner.id)

    client = make_client(db_session, {"tok": assertion_for(intruder)})
    body = client.post(
        "/integrations/sheets/results",
        json={"submission_ids": [str(fixture.membership.id)]},
        headers={"Authorization": "Bearer tok"},
    ).json()

    # Silence, not a 404: an id that exists and an id that does not must be
    # indistinguishable, or this endpoint enumerates other people's work.
    assert body["rows"] == []


def test_an_administrator_reads_across_campaigns(db_session: Session, enable_sheets: None) -> None:
    owner = make_user(db_session, email="owner@vmr.example")
    admin = make_user(db_session, email="admin@vmr.example", role=UserRole.ADMIN)
    fixture = build_sequence(db_session, email="ada@kiln.example", owner_user_id=owner.id)

    client = make_client(db_session, {"tok": assertion_for(admin)})
    body = client.post(
        "/integrations/sheets/results",
        json={"submission_ids": [str(fixture.membership.id)]},
        headers={"Authorization": "Bearer tok"},
    ).json()
    assert len(body["rows"]) == 1


def test_a_disabled_account_is_refused(db_session: Session, enable_sheets: None) -> None:
    user = make_user(db_session, email="mine@vmr.example", state=UserState.DISABLED)
    client = make_client(db_session, {"tok": assertion_for(user)})
    response = client.get("/integrations/sheets/campaigns", headers={"Authorization": "Bearer tok"})
    assert response.status_code == 401


def test_a_google_identity_with_no_vmr_account_is_refused(
    db_session: Session, enable_sheets: None
) -> None:
    stranger = VerifiedAssertion(
        subject="google-stranger",
        email="stranger@example.com",
        display_name="Stranger",
        audience="add-on-client-id.apps.googleusercontent.com",
    )
    client = make_client(db_session, {"tok": stranger})
    response = client.get("/integrations/sheets/campaigns", headers={"Authorization": "Bearer tok"})
    assert response.status_code == 401


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": "Basic dXNlcjpwYXNz"},
        {"Authorization": "Bearer "},
    ],
)
def test_every_bad_credential_is_refused_identically(
    db_session: Session, enable_sheets: None, headers: dict[str, str]
) -> None:
    make_user(db_session, email="mine@vmr.example")
    client = make_client(db_session, {"tok": assertion_for(make_user(db_session, email="x@y.z"))})
    response = client.get("/integrations/sheets/campaigns", headers=headers)
    assert response.status_code == 401
    assert response.json()["detail"] == "unauthorized"


def test_an_oversized_batch_is_refused_whole(db_session: Session, enable_sheets: None) -> None:
    user = make_user(db_session, email="mine@vmr.example")
    campaign = make_campaign(db_session, name="Mine", owner=user)
    seed_company(db_session, name="Kiln Systems", domain="kiln.example")

    client = make_client(db_session, {"tok": assertion_for(user)})
    rows = [row(f"r{index}") for index in range(6)]  # ceiling is 5 in this fixture
    response = client.post(
        "/integrations/sheets/batches",
        json=batch_payload(campaign, rows),
        headers={"Authorization": "Bearer tok"},
    )

    assert response.status_code == 400
    assert "the maximum is 5" in response.json()["detail"]
    # Nothing was processed. A prefix would look like success.
    assert db_session.query(CampaignContact).count() == 0


def test_a_duplicate_client_row_id_in_one_request_is_refused(
    db_session: Session, enable_sheets: None
) -> None:
    user = make_user(db_session, email="mine@vmr.example")
    campaign = make_campaign(db_session, name="Mine", owner=user)
    client = make_client(db_session, {"tok": assertion_for(user)})
    response = client.post(
        "/integrations/sheets/batches",
        json=batch_payload(campaign, [row("r1"), row("r1", first="Grace")]),
        headers={"Authorization": "Bearer tok"},
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# The shipped ceiling, proved at the number a deployment actually runs with
#
# Every test above pins the ceiling to five so a refusal is cheap to state. That
# proves the *mechanism* and says nothing about the *number*, and the number is
# what an operator meets: a sheet of five hundred prospects is one cohort of
# work, and refusing it at fifty made the surface unusable for the campaign size
# this product exists for. So the tests below deliberately unset the override and
# run against the value the deployment ships, end to end, all the way to the
# pipeline state each row lands in.
# ---------------------------------------------------------------------------

#: The supported maximum for one Google Sheets ingestion, stated here as the
#: contract rather than read from configuration — a test that asks the code what
#: its own limit is cannot notice the limit changing.
SUPPORTED_COHORT = 500


@pytest.fixture()
def sheets_at_shipped_limits(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The integration switched on with the ceilings a deployment actually gets."""

    monkeypatch.setenv("FEATURES__GOOGLE_SHEETS_INTEGRATION", "true")
    monkeypatch.setenv("FEATURES__EMAIL_SEQUENCES", "true")
    monkeypatch.setenv(
        "SHEETS__ALLOWED_AUDIENCES", '["add-on-client-id.apps.googleusercontent.com"]'
    )
    # Removed rather than set: the point of these tests is the default.
    monkeypatch.delenv("SHEETS__MAX_BATCH_ROWS", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


#: Five established Companies, so a cohort spreads across companies the way a
#: real sheet does — exercising the per-name cache, the domain link and the
#: name-plus-domain natural key rather than a single repeated lookup.
COHORT_COMPANIES = [
    ("Kiln Systems", "kiln.example"),
    ("Harbour Analytics", "harbour.example"),
    ("Meridian Foods", "meridian.example"),
    ("Northwind Optics", "northwind.example"),
    ("Ardent Polymers", "ardent.example"),
]


def seed_cohort_companies(db: Session) -> None:
    for name, domain in COHORT_COMPANIES:
        seed_company(db, name=name, domain=domain)


def cohort(count: int) -> list[dict[str, Any]]:
    """``count`` distinct prospect rows, numbered from one as a sheet would be."""

    rows: list[dict[str, Any]] = []
    for number in range(1, count + 1):
        name, _domain = COHORT_COMPANIES[(number - 1) % len(COHORT_COMPANIES)]
        rows.append(
            row(
                f"r{number:04d}",
                first=f"Person{number:04d}",
                last="Prospect",
                company=name,
            )
        )
    return rows


def submit_cohort(
    client: TestClient, campaign: Campaign, rows: list[dict[str, Any]]
) -> httpx.Response:
    return client.post(
        "/integrations/sheets/batches",
        json=batch_payload(campaign, rows),
        headers={"Authorization": "Bearer tok"},
    )


def test_the_shipped_ceiling_the_add_on_is_told_is_five_hundred_rows(
    db_session: Session, sheets_at_shipped_limits: None
) -> None:
    """The add-on chunks against this number, so it is part of the contract."""

    user = make_user(db_session, email="mine@vmr.example")
    make_campaign(db_session, name="Mine", owner=user)

    client = make_client(db_session, {"tok": assertion_for(user)})
    body = client.get(
        "/integrations/sheets/campaigns", headers={"Authorization": "Bearer tok"}
    ).json()
    assert body["limits"]["max_batch_rows"] == SUPPORTED_COHORT


@pytest.mark.parametrize("size", [50, 51, 499, SUPPORTED_COHORT])
def test_a_cohort_up_to_the_supported_maximum_is_accepted_whole(
    db_session: Session, sheets_at_shipped_limits: None, size: int
) -> None:
    """Fifty was the old ceiling; fifty-one is the row that used to be refused.

    Stated as one parametrized fact rather than four near-identical tests,
    because the interesting property is that nothing changes across the old
    boundary: the fifty-first row is accepted on exactly the terms the fiftieth
    is, and so is the five-hundredth.
    """

    user = make_user(db_session, email="mine@vmr.example")
    campaign = make_campaign(db_session, name="Mine", owner=user)
    seed_cohort_companies(db_session)

    client = make_client(db_session, {"tok": assertion_for(user)})
    response = submit_cohort(client, campaign, cohort(size))

    assert response.status_code == 200
    body = response.json()
    assert body["counts"] == {"submitted": size, "accepted": size, "could_not_prepare": 0}
    # Order preserved: the add-on writes by identifier, but an operator reads the
    # response against their own sheet.
    assert [entry["client_row_id"] for entry in body["rows"]] == [
        f"r{number:04d}" for number in range(1, size + 1)
    ]
    assert db_session.query(CampaignContact).count() == size


def test_the_whole_five_hundred_row_cohort_reaches_the_pipeline(
    db_session: Session, sheets_at_shipped_limits: None
) -> None:
    """The proof that matters: not "accepted", but *arrived*.

    A validator that stops refusing is not the repair. Every one of the five
    hundred rows has to become a permanent Contact, a Campaign membership, a
    provenance record naming its own sheet row, and a pipeline that starts where
    every other enrolment path starts — and the four hundred and fifty rows past
    the old ceiling have to be indistinguishable from the fifty that were always
    allowed through.
    """

    user = make_user(db_session, email="mine@vmr.example")
    campaign = make_campaign(db_session, name="Mine", owner=user)
    seed_cohort_companies(db_session)

    client = make_client(db_session, {"tok": assertion_for(user)})
    body = submit_cohort(client, campaign, cohort(SUPPORTED_COHORT)).json()

    assert body["counts"]["accepted"] == SUPPORTED_COHORT
    entries = body["rows"]

    # Nothing was processed twice and nothing was answered with somebody else's
    # identifier — the failure a chunked or repeated pass would produce.
    submission_ids = [entry["submission_id"] for entry in entries]
    contact_ids = [entry["contact_id"] for entry in entries]
    assert len(set(submission_ids)) == SUPPORTED_COHORT
    assert len(set(contact_ids)) == SUPPORTED_COHORT

    memberships = db_session.scalars(
        select(CampaignContact).where(CampaignContact.campaign_id == campaign.id)
    ).all()
    assert len(memberships) == SUPPORTED_COHORT
    assert db_session.query(Contact).count() == SUPPORTED_COHORT

    # Every membership carries the sheet's own provenance, and every one of them
    # starts at the canonical first stage with Capture already complete.
    sources = db_session.scalars(
        select(CampaignContactSource).where(
            CampaignContactSource.campaign_contact_id.in_([m.id for m in memberships])
        )
    ).all()
    assert len(sources) == SUPPORTED_COHORT
    assert {source.source_type for source in sources} == {sheet_submit.SOURCE_TYPE}
    assert {source.source_reference for source in sources} == {
        f"r{number:04d}" for number in range(1, SUPPORTED_COHORT + 1)
    }
    assert len({source.idempotency_key for source in sources}) == SUPPORTED_COHORT

    assert {membership.next_stage for membership in memberships} == {AgentIdentifier.IDENTITY}
    completed_capture = db_session.scalars(
        select(CampaignContactAgentState).where(
            CampaignContactAgentState.campaign_contact_id.in_([m.id for m in memberships]),
            CampaignContactAgentState.agent_id == AgentIdentifier.CAPTURE,
            CampaignContactAgentState.status == PipelineStageStatus.COMPLETED,
        )
    ).all()
    assert len(completed_capture) == SUPPORTED_COHORT

    # The last row of the sheet, read the way an operator would a year later.
    last = entries[-1]
    assert last["client_row_id"] == f"r{SUPPORTED_COHORT:04d}"
    last_source = next(
        source for source in sources if source.source_reference == last["client_row_id"]
    )
    context = last_source.source_context
    assert context["surface"] == "google_sheets_addon"
    assert context["spreadsheet_id"] == SPREADSHEET
    assert context["sheet_id"] == TAB
    assert context["client_row_id"] == last["client_row_id"]
    last_contact = db_session.get(Contact, uuid.UUID(last["contact_id"]))
    assert last_contact is not None
    assert last_contact.first_name == f"Person{SUPPORTED_COHORT:04d}"
    expected_name, expected_domain = COHORT_COMPANIES[(SUPPORTED_COHORT - 1) % 5]
    assert last_contact.company_name == expected_name
    assert last_contact.company_domain == expected_domain
    assert last_contact.company_id is not None

    # And the rows past the old ceiling were handed to the pipeline on exactly
    # the terms the first fifty were. Stated as a comparison rather than as a
    # literal, because the point is not how many artefacts one enrolment
    # produces — it is that the four hundred and fiftieth row past the ceiling
    # produces the same ones as the first row before it.
    below = [entry["submission_id"] for entry in entries[:50]]
    above = [entry["submission_id"] for entry in entries[50:]]
    assert len(below) == 50 and len(above) == 450

    def per_membership(submission_ids: list[str]) -> set[tuple[int, int, int]]:
        keys = [uuid.UUID(value) for value in submission_ids]
        states = db_session.scalars(
            select(CampaignContactAgentState).where(
                CampaignContactAgentState.campaign_contact_id.in_(keys)
            )
        ).all()
        events = db_session.scalars(
            select(PipelineEvent).where(PipelineEvent.campaign_contact_id.in_(keys))
        ).all()
        jobs = db_session.scalars(
            select(AgentJob).where(AgentJob.campaign_contact_id.in_(keys))
        ).all()
        return {
            (
                sum(1 for state in states if state.campaign_contact_id == key),
                sum(1 for event in events if event.campaign_contact_id == key),
                sum(1 for job in jobs if job.campaign_contact_id == key),
            )
            for key in keys
        }

    shape = per_membership(below)
    # One shape, not a range: every row of the first fifty was treated alike...
    assert len(shape) == 1
    # ...it is a real amount of downstream work, not an empty set of nothing...
    states_each, events_each, _jobs_each = next(iter(shape))
    assert states_each > 0 and events_each > 0
    # ...and rows 51-500 carry precisely the same shape.
    assert per_membership(above) == shape


def test_one_row_past_the_supported_maximum_is_refused_whole(
    db_session: Session, sheets_at_shipped_limits: None
) -> None:
    """The cap is still a cap, and it still refuses rather than truncates.

    Silently keeping the first five hundred would read as success in the sheet
    and leave the operator to discover the missing rows themselves.
    """

    user = make_user(db_session, email="mine@vmr.example")
    campaign = make_campaign(db_session, name="Mine", owner=user)
    seed_cohort_companies(db_session)

    client = make_client(db_session, {"tok": assertion_for(user)})
    response = submit_cohort(client, campaign, cohort(SUPPORTED_COHORT + 1))

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert f"this request carries {SUPPORTED_COHORT + 1} rows" in detail
    assert f"the maximum is {SUPPORTED_COHORT}" in detail
    assert db_session.query(CampaignContact).count() == 0
    assert db_session.query(Contact).count() == 0


def test_a_bad_row_inside_a_full_cohort_still_costs_only_itself(
    db_session: Session, sheets_at_shipped_limits: None
) -> None:
    """Row-level partial success is unchanged across the whole five hundred."""

    user = make_user(db_session, email="mine@vmr.example")
    campaign = make_campaign(db_session, name="Mine", owner=user)
    seed_cohort_companies(db_session)

    rows = cohort(SUPPORTED_COHORT)
    broken = {"r0001", "r0250", f"r{SUPPORTED_COHORT:04d}"}
    for item in rows:
        if item["client_row_id"] in broken:
            item["last_name"] = "   "

    client = make_client(db_session, {"tok": assertion_for(user)})
    body = submit_cohort(client, campaign, rows).json()

    healthy = SUPPORTED_COHORT - len(broken)
    assert body["counts"] == {
        "submitted": SUPPORTED_COHORT,
        "accepted": healthy,
        "could_not_prepare": len(broken),
    }
    by_id = {entry["client_row_id"]: entry for entry in body["rows"]}
    for client_row_id in broken:
        assert by_id[client_row_id]["status"] == RowStatus.COULD_NOT_PREPARE.value
        assert by_id[client_row_id]["failure_code"] == "missing_required_field"
        assert by_id[client_row_id]["submission_id"] is None
    assert db_session.query(CampaignContact).count() == healthy
    assert db_session.query(Contact).count() == healthy


# ---------------------------------------------------------------------------
# 18. No sending side effect, and the switch
# ---------------------------------------------------------------------------


def test_nothing_in_this_surface_creates_a_gmail_draft_or_a_send(
    db_session: Session, enable_sheets: None
) -> None:
    from app.models.gmail import GmailDraftRecord

    user = make_user(db_session, email="mine@vmr.example")
    campaign = make_campaign(db_session, name="Mine", owner=user)
    seed_company(db_session, name="Kiln Systems", domain="kiln.example")

    client = make_client(db_session, {"tok": assertion_for(user)})
    entry = client.post(
        "/integrations/sheets/batches",
        json=batch_payload(campaign, [row("r1")]),
        headers={"Authorization": "Bearer tok"},
    ).json()["rows"][0]
    client.post(
        "/integrations/sheets/results",
        json={"submission_ids": [entry["submission_id"]]},
        headers={"Authorization": "Bearer tok"},
    )

    assert db_session.query(GmailDraftRecord).count() == 0
    membership = db_session.get(CampaignContact, uuid.UUID(entry["submission_id"]))
    assert membership is not None
    # Nothing was sent, scheduled or drafted, and the Sending Agent has no
    # production adapter to do any of the three with.
    assert membership.sending_state == "not_started"
    from app.services.agents.registry import get_agent_spec

    assert get_agent_spec(AgentIdentifier.SENDING).implemented is False


@pytest.mark.parametrize(
    "path,method",
    [
        ("/integrations/sheets/campaigns", "get"),
        ("/integrations/sheets/batches", "post"),
        ("/integrations/sheets/results", "post"),
    ],
)
def test_the_surface_does_not_exist_while_the_feature_is_off(
    db_session: Session, path: str, method: str
) -> None:
    get_settings.cache_clear()
    user = make_user(db_session, email="mine@vmr.example")
    client = make_client(db_session, {"tok": assertion_for(user)})
    call = getattr(client, method)
    response = (
        call(path, headers={"Authorization": "Bearer tok"})
        if method == "get"
        else call(path, headers={"Authorization": "Bearer tok"}, json={})
    )
    assert response.status_code == 404


def test_the_surface_is_unavailable_while_email_sequences_are_off(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The capability gate, proved rather than described.

    Without sequences no row could ever reach Ready, so the honest answer is that
    the integration is unavailable — not that it accepts work it cannot finish.
    """

    monkeypatch.setenv("FEATURES__GOOGLE_SHEETS_INTEGRATION", "true")
    monkeypatch.setenv("FEATURES__EMAIL_SEQUENCES", "false")
    monkeypatch.setenv(
        "SHEETS__ALLOWED_AUDIENCES", '["add-on-client-id.apps.googleusercontent.com"]'
    )
    get_settings.cache_clear()
    user = make_user(db_session, email="mine@vmr.example")
    client = make_client(db_session, {"tok": assertion_for(user)})
    response = client.get("/integrations/sheets/campaigns", headers={"Authorization": "Bearer tok"})
    get_settings.cache_clear()
    assert response.status_code == 404


def test_an_administrator_can_switch_the_surface_off_without_a_deploy(
    db_session: Session, enable_sheets: None
) -> None:
    """The control on the Admin screen is the one the routes actually read."""

    from app.services.operations import settings as operational

    user = make_user(db_session, email="admin@vmr.example", role=UserRole.ADMIN)
    client = make_client(db_session, {"tok": assertion_for(user)})
    assert (
        client.get(
            "/integrations/sheets/campaigns", headers={"Authorization": "Bearer tok"}
        ).status_code
        == 200
    )

    operational.set_control(
        db_session,
        key="google_sheets_integration",
        enabled_value=False,
        actor="admin@vmr.example",
        reason="pausing the add-on",
    )
    db_session.flush()

    assert (
        client.get(
            "/integrations/sheets/campaigns", headers={"Authorization": "Bearer tok"}
        ).status_code
        == 404
    )


def test_the_credential_dependency_is_declared_on_the_router_not_the_handlers() -> None:
    """The invariant that makes a future route safe by default."""

    declared = [
        dependency.dependency  # type: ignore[attr-defined]
        for dependency in sheets_router.dependencies
    ]
    assert require_account in declared
    assert sheets_router.routes, "the router has no routes — the walk is broken"


# ---------------------------------------------------------------------------
# Supplied address and website, over the wire
# ---------------------------------------------------------------------------


def test_a_row_may_supply_the_address_and_the_website(
    db_session: Session, enable_sheets: None
) -> None:
    """The two optional columns, accepted through the real request.

    Proved at the HTTP boundary rather than against the service, because these
    values arrive from a spreadsheet cell an operator typed — the request is where
    an unusable one has to be refused, and where an accepted one has to reach
    durable provenance rather than a variable.
    """

    user = make_user(db_session, email="mine@vmr.example")
    campaign = make_campaign(db_session, name="Supplied", owner=user)
    client = make_client(db_session, {"tok": assertion_for(user)})

    response = client.post(
        "/integrations/sheets/batches",
        json=batch_payload(
            campaign,
            [
                row(
                    "r1",
                    email="Ada.Lovelace@Kiln.example",
                    website="https://www.kiln.example/about",
                )
            ],
        ),
        headers={"Authorization": "Bearer tok"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["counts"] == {"submitted": 1, "accepted": 1, "could_not_prepare": 0}

    contact = db_session.scalars(select(Contact)).one()
    assert contact.email == "ada.lovelace@kiln.example"
    assert contact.company_domain == "kiln.example"
    # The supplied website established the permanent Company. Without it the
    # Company Agent would block on `company_missing`, leaving a row that supplied
    # a website worse off than one that supplied nothing.
    assert contact.company_id is not None
    company = db_session.get(Company, contact.company_id)
    assert company is not None and company.domain == "kiln.example"
    # And it did so without inventing a resolution decision about the company:
    # nothing was asked, so nothing may be recorded as having answered.
    assert db_session.scalars(select(CompanyDomainResolution)).all() == []

    membership = db_session.scalars(select(CampaignContact)).one()
    source = db_session.scalars(
        select(CampaignContactSource).where(
            CampaignContactSource.campaign_contact_id == membership.id
        )
    ).one()
    supplied = source.source_context["operator_supplied_inputs"]
    assert supplied["email"]["normalized"] == "ada.lovelace@kiln.example"
    assert supplied["email"]["raw"] == "Ada.Lovelace@Kiln.example"
    assert supplied["email"]["discovered"] is False
    assert supplied["email"]["verified"] is False
    assert supplied["company_domain"]["normalized"] == "kiln.example"

    # Nothing was verified, and nothing anywhere says it was.
    assert db_session.scalars(select(ExactEmailVerification)).all() == []


def test_a_malformed_supplied_address_refuses_only_that_row(
    db_session: Session, enable_sheets: None
) -> None:
    """One bad cell costs one row, and the answer names the cell to fix."""

    user = make_user(db_session, email="mine@vmr.example")
    campaign = make_campaign(db_session, name="Supplied", owner=user)
    seed_company(db_session, name="Kiln Systems", domain="kiln.example")
    client = make_client(db_session, {"tok": assertion_for(user)})

    response = client.post(
        "/integrations/sheets/batches",
        json=batch_payload(
            campaign,
            [
                row("bad", email="not-an-address"),
                row("good", first="Grace", last="Hopper"),
            ],
        ),
        headers={"Authorization": "Bearer tok"},
    )

    assert response.status_code == 200
    rows = {item["client_row_id"]: item for item in response.json()["rows"]}
    assert rows["bad"]["status"] == RowStatus.COULD_NOT_PREPARE.value
    assert rows["bad"]["failure_code"] == "email_unusable"
    assert "email address" in rows["bad"]["safe_failure_reason"]
    assert rows["good"]["status"] == RowStatus.PENDING.value

    # The refused row created nothing, and cost the other row nothing.
    contacts = db_session.scalars(select(Contact)).all()
    assert [contact.last_name for contact in contacts] == ["Hopper"]


def test_an_unreadable_website_still_enrols_the_row(
    db_session: Session, enable_sheets: None
) -> None:
    """A website has a fallback the address does not: the Company Agent.

    So an unreadable one is dropped and recorded rather than refused. Refusing
    would cost the operator a contact the product can prepare perfectly well.
    """

    user = make_user(db_session, email="mine@vmr.example")
    campaign = make_campaign(db_session, name="Supplied", owner=user)
    client = make_client(db_session, {"tok": assertion_for(user)})

    response = client.post(
        "/integrations/sheets/batches",
        json=batch_payload(campaign, [row("r1", website="not a hostname")]),
        headers={"Authorization": "Bearer tok"},
    )

    assert response.status_code == 200
    assert response.json()["rows"][0]["status"] == RowStatus.PENDING.value

    contact = db_session.scalars(select(Contact)).one()
    assert contact.company_domain is None

    membership = db_session.scalars(select(CampaignContact)).one()
    source = db_session.scalars(
        select(CampaignContactSource).where(
            CampaignContactSource.campaign_contact_id == membership.id
        )
    ).one()
    record = source.source_context["operator_supplied_inputs"]["company_domain"]
    assert record["usable"] is False
    assert record["raw"] == "not a hostname"


def test_two_rows_supplying_the_same_address_resolve_to_one_person(
    db_session: Session, enable_sheets: None
) -> None:
    """An exact normalized address is the strongest dedup key this system has.

    It is also uniquely indexed, so a second row carrying the same address has to
    match rather than insert — otherwise the request fails against the index.
    """

    user = make_user(db_session, email="mine@vmr.example")
    campaign = make_campaign(db_session, name="Supplied", owner=user)
    client = make_client(db_session, {"tok": assertion_for(user)})

    response = client.post(
        "/integrations/sheets/batches",
        json=batch_payload(
            campaign,
            [
                row("r1", email="ada@kiln.example", website="https://kiln.example"),
                row("r2", first="A.", email="ADA@kiln.example"),
            ],
        ),
        headers={"Authorization": "Bearer tok"},
    )

    assert response.status_code == 200
    assert [item["status"] for item in response.json()["rows"]] == [
        RowStatus.PENDING.value,
        RowStatus.PENDING.value,
    ]
    assert len(db_session.scalars(select(Contact)).all()) == 1
    assert len(db_session.scalars(select(CampaignContact)).all()) == 1


def test_a_supplied_address_another_person_already_owns_is_not_written(
    db_session: Session, enable_sheets: None
) -> None:
    """The blank stays blank rather than colliding with a unique index.

    This row matches its person by LinkedIn URL, so nothing has asked whether
    somebody else already holds the address the sheet supplied — and somebody
    does. Writing it would raise an integrity error out of the flush and take the
    whole request down, for a value that was only ever filling a blank.

    Two people cannot share a mailbox and the sheet has not said which of them is
    wrong, so the address is left alone and the pipeline discovers one for this
    person exactly as it would have.
    """

    user = make_user(db_session, email="mine@vmr.example")
    campaign = make_campaign(db_session, name="Supplied", owner=user)
    company = seed_company(db_session, name="Kiln Systems", domain="kiln.example")

    owner = Contact(
        first_name="Grace",
        last_name="Hopper",
        company_name="Kiln Systems",
        company_domain="kiln.example",
        company_id=company.id,
        email="shared@kiln.example",
    )
    matched = Contact(
        first_name="Ada",
        last_name="Lovelace",
        company_name="Kiln Systems",
        company_domain="kiln.example",
        company_id=company.id,
        linkedin_url="https://www.linkedin.com/in/ada-lovelace",
    )
    db_session.add_all([owner, matched])
    db_session.flush()

    client = make_client(db_session, {"tok": assertion_for(user)})
    response = client.post(
        "/integrations/sheets/batches",
        json=batch_payload(
            campaign,
            [
                row(
                    "r1",
                    email="shared@kiln.example",
                    linkedin_url="https://www.linkedin.com/in/ada-lovelace",
                )
            ],
        ),
        headers={"Authorization": "Bearer tok"},
    )

    assert response.status_code == 200
    assert response.json()["rows"][0]["status"] == RowStatus.PENDING.value

    db_session.refresh(matched)
    db_session.refresh(owner)
    assert matched.email is None
    assert owner.email == "shared@kiln.example"
    assert len(db_session.scalars(select(Contact)).all()) == 2


def test_a_suppressed_supplied_address_leaves_nothing_behind(
    db_session: Session, enable_sheets: None
) -> None:
    """The ledger is asked before anything is created, using the supplied value."""

    user = make_user(db_session, email="mine@vmr.example")
    campaign = make_campaign(db_session, name="Supplied", owner=user)
    db_session.add(
        Suppression(
            suppression_type=SuppressionType.EMAIL,
            value="ada@kiln.example",
            reason=SuppressionReason.MANUAL,
            created_by="operator",
        )
    )
    db_session.flush()
    client = make_client(db_session, {"tok": assertion_for(user)})

    response = client.post(
        "/integrations/sheets/batches",
        json=batch_payload(campaign, [row("r1", email="ada@kiln.example")]),
        headers={"Authorization": "Bearer tok"},
    )

    assert response.status_code == 200
    entry = response.json()["rows"][0]
    assert entry["status"] == RowStatus.COULD_NOT_PREPARE.value
    assert entry["failure_code"] == "suppressed"
    assert db_session.scalars(select(Contact)).all() == []
    assert db_session.scalars(select(CampaignContact)).all() == []


# ---------------------------------------------------------------------------
# The pure contract, proved without a database
# ---------------------------------------------------------------------------


def test_the_row_key_changes_with_every_part_of_the_row_identity() -> None:
    base = SheetLocation(installation_id="i", spreadsheet_id="s", sheet_id="t")
    key = row_idempotency_key(base, campaign_id="c", client_row_id="r", generation=1)
    variants = [
        row_idempotency_key(
            SheetLocation(installation_id="i2", spreadsheet_id="s", sheet_id="t"),
            campaign_id="c",
            client_row_id="r",
            generation=1,
        ),
        row_idempotency_key(
            SheetLocation(installation_id="i", spreadsheet_id="s2", sheet_id="t"),
            campaign_id="c",
            client_row_id="r",
            generation=1,
        ),
        row_idempotency_key(
            SheetLocation(installation_id="i", spreadsheet_id="s", sheet_id="t2"),
            campaign_id="c",
            client_row_id="r",
            generation=1,
        ),
        row_idempotency_key(base, campaign_id="c2", client_row_id="r", generation=1),
        row_idempotency_key(base, campaign_id="c", client_row_id="r2", generation=1),
        row_idempotency_key(base, campaign_id="c", client_row_id="r", generation=2),
    ]
    assert len(set(variants)) == len(variants)
    assert key not in variants


def test_two_different_identities_cannot_collide_by_shifting_a_separator() -> None:
    """The length-prefix rule, stated as the attack it prevents."""

    left = SheetLocation(installation_id="ab", spreadsheet_id="c", sheet_id="t")
    right = SheetLocation(installation_id="a", spreadsheet_id="bc", sheet_id="t")
    assert batch_id(left, campaign_id="x", generation=1) != batch_id(
        right, campaign_id="x", generation=1
    )


def test_a_cell_is_collapsed_before_it_becomes_a_name() -> None:
    parsed = parse_row(
        {
            "client_row_id": "r1",
            "first_name": "  Ada\n",
            "last_name": "Lovelace ",
            "company_name": "Kiln   Systems",
        },
        max_context_chars=100,
    )
    assert (parsed.first_name, parsed.last_name) == ("Ada", "Lovelace")
    assert parsed.company_name == "Kiln Systems"


def test_a_non_linkedin_url_is_refused_rather_than_stored() -> None:
    from app.services.integrations.sheets.contract import RowContractError

    with pytest.raises(RowContractError):
        parse_row(
            {
                "client_row_id": "r1",
                "first_name": "Ada",
                "last_name": "Lovelace",
                "company_name": "Kiln",
                "linkedin_url": "https://example.com/in/ada",
            },
            max_context_chars=100,
        )


def test_the_assertion_rules_refuse_every_way_a_token_can_be_wrong() -> None:
    from app.core.auth.identity import IdentityClaims

    def claims(**overrides: Any) -> IdentityClaims:
        base = {
            "subject": "google-1",
            "email": "mine@vmr.example",
            "email_verified": True,
            "display_name": "Operator",
            "issuer": "https://accounts.google.com",
            "audience": "ours",
            "expires_at": 2_000_000_000,
            "issued_at": 1_000_000_000,
        }
        base.update(overrides)
        return IdentityClaims(**base)  # type: ignore[arg-type]

    now = 1_000_000_100
    ok = validate_assertion_claims(
        claims(), allowed_audiences=("ours",), now=now, accepted_issuers=DEFAULT_ACCEPTED_ISSUERS
    )
    assert ok.email == "mine@vmr.example"

    for bad in (
        claims(issuer="https://evil.example"),
        claims(audience="somebody-elses-client"),
        claims(expires_at=now - 120),
        claims(issued_at=now + 3600),
        claims(email_verified=False),
        claims(subject=""),
    ):
        with pytest.raises(IdentityAssertionError):
            validate_assertion_claims(bad, allowed_audiences=("ours",), now=now)

    # An unconfigured deployment accepts nobody rather than everybody.
    with pytest.raises(IdentityAssertionError):
        validate_assertion_claims(claims(), allowed_audiences=(), now=now)


@pytest.mark.parametrize(
    "header",
    [None, "", "Bearer", "Basic abc", "Bearer   ", "bearer"],
)
def test_a_malformed_authorization_header_is_refused(header: str | None) -> None:
    with pytest.raises(IdentityAssertionError):
        bearer_token(header)


def test_a_well_formed_header_yields_the_token() -> None:
    assert bearer_token("Bearer abc.def.ghi") == "abc.def.ghi"
    assert bearer_token("bearer abc.def.ghi") == "abc.def.ghi"
