"""Knowledge Base workbench page tests (KB-001).

Covers the authorization pattern the app already uses (a default-off feature
switch inside the local-only workbench), the edit flows, the empty states, and
the promise that the campaign page is unchanged while the switch is off.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from app.core.config import get_settings
from app.main import create_app
from app.models.campaign import Campaign
from app.models.enums import SellerRecordState
from app.models.seller_knowledge import CampaignOffering, SellerOffering
from app.models.seller_profile import SellerProfile
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The Knowledge Base is off by default (FND-007); this suite opts in."""

    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__SELLER_KNOWLEDGE_BASE", "true")
    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


@pytest.fixture()
def workbench_only(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The workbench with the Knowledge Base switch left off."""

    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.delenv("FEATURES__SELLER_KNOWLEDGE_BASE", raising=False)
    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


KB_PAGES = (
    "/knowledge-base",
    "/knowledge-base/company",
    "/knowledge-base/offerings",
    "/knowledge-base/proof-points",
    "/knowledge-base/restricted-claims",
    "/knowledge-base/personas",
)


def _create_offering(client: TestClient, name: str = "Cement outlook") -> str:
    response = client.post(
        "/knowledge-base/offerings",
        data={"name": name, "offering_type": "research_report"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response.headers["location"].split("/")[3].split("?")[0]


def _create_campaign(client: TestClient, name: str = "Cement EU pilot") -> str:
    response = client.post("/campaigns/create", data={"name": name}, follow_redirects=False)
    assert response.status_code == 303
    return response.headers["location"].split("/")[2].split("?")[0]


# --- 1. Authorization: the default-off switch --------------------------------


@pytest.mark.parametrize("path", KB_PAGES)
def test_every_page_is_absent_while_the_switch_is_off(
    workbench_only: TestClient, path: str
) -> None:
    """Off means the area does not exist, not that it exists and refuses."""

    assert workbench_only.get(path).status_code == 404


# Every gated write, so "the whole area 404s while the switch is off" is
# evidenced rather than asserted about a sample. {id} is a syntactically valid
# UUID that does not exist: a route that answered 404 only because the record
# was missing would still be indistinguishable from one that is switched off,
# so each of these is also exercised with the switch ON elsewhere in this file.
_DEAD_ID = "00000000-0000-4000-8000-000000000000"
GATED_WRITES = (
    "/knowledge-base/company",
    "/knowledge-base/offerings",
    f"/knowledge-base/offerings/{_DEAD_ID}",
    f"/knowledge-base/offerings/{_DEAD_ID}/state",
    f"/knowledge-base/offerings/{_DEAD_ID}/links",
    "/knowledge-base/proof-points",
    f"/knowledge-base/proof-points/{_DEAD_ID}",
    f"/knowledge-base/proof-points/{_DEAD_ID}/state",
    "/knowledge-base/restricted-claims",
    f"/knowledge-base/restricted-claims/{_DEAD_ID}",
    f"/knowledge-base/restricted-claims/{_DEAD_ID}/state",
    "/knowledge-base/personas",
    f"/knowledge-base/personas/{_DEAD_ID}",
    f"/knowledge-base/personas/{_DEAD_ID}/state",
)


@pytest.mark.parametrize("path", GATED_WRITES)
def test_every_write_is_absent_while_the_switch_is_off(
    workbench_only: TestClient, path: str
) -> None:
    assert workbench_only.post(path, data={}).status_code == 404


@pytest.mark.parametrize("path", [f"/knowledge-base/offerings/{_DEAD_ID}"])
def test_the_offering_detail_page_is_absent_while_the_switch_is_off(
    workbench_only: TestClient, path: str
) -> None:
    assert workbench_only.get(path).status_code == 404


def test_the_nav_entry_appears_when_the_switch_is_on(client: TestClient) -> None:
    page = client.get("/admin").text
    assert 'href="/knowledge-base"' in page
    assert "Seller context" in page


def test_the_nav_entry_is_absent_when_the_switch_is_off(workbench_only: TestClient) -> None:
    # The two clients are deliberately in separate tests: both fixtures drive the
    # same cached settings object, so holding one of each at once would leave
    # whichever fixture resolved last speaking for both.
    page = workbench_only.get("/admin").text
    assert 'href="/knowledge-base"' not in page
    assert "Seller context" not in page


def test_the_campaign_page_is_untouched_while_the_switch_is_off(
    workbench_only: TestClient, committed_session: Session
) -> None:
    campaign = _create_campaign(workbench_only)
    page = workbench_only.get(f"/campaigns/{campaign}")
    assert page.status_code == 200
    assert "Offerings this campaign concerns" not in page.text
    assert workbench_only.post(f"/campaigns/{campaign}/offerings").status_code == 404


# --- 2. Empty states ---------------------------------------------------------


def test_the_overview_reports_an_empty_knowledge_base_without_alarm(client: TestClient) -> None:
    page = client.get("/knowledge-base")
    assert page.status_code == 200
    assert "Nothing entered yet" in page.text
    assert "not configured" in page.text


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/knowledge-base/offerings", "No offerings yet"),
        ("/knowledge-base/proof-points", "No proof points yet"),
        ("/knowledge-base/restricted-claims", "No restrictions yet"),
        ("/knowledge-base/personas", "No personas yet"),
    ],
)
def test_each_section_has_its_own_empty_state(client: TestClient, path: str, expected: str) -> None:
    page = client.get(path)
    assert page.status_code == 200
    assert expected in page.text


# --- 3. The company profile --------------------------------------------------


def test_the_profile_saves_and_reads_back(client: TestClient, committed_session: Session) -> None:
    response = client.post(
        "/knowledge-base/company",
        data={
            "name": "Verified Market Research",
            "short_description": "Research firm.",
            "description": "We publish market research.",
            "positioning": "Depth over breadth.",
            "industries_served": "Cement\nChemicals",
            "geographies_served": "EU",
            "capabilities": "Custom research",
            "differentiators": "Analyst access",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = client.get("/knowledge-base/company")
    assert "Verified Market Research" in page.text
    assert "Cement\nChemicals" in page.text
    stored = committed_session.scalar(select(SellerProfile))
    assert stored is not None
    assert stored.industries_served == ["Cement", "Chemicals"]


def test_a_blank_name_is_refused_with_a_readable_message(client: TestClient) -> None:
    response = client.post("/knowledge-base/company", data={"name": "   "}, follow_redirects=False)
    assert response.status_code == 303
    assert "err=" in response.headers["location"]
    assert "Company+name+is+required" in response.headers["location"]


def test_saving_the_profile_twice_never_makes_a_second_one(
    client: TestClient, committed_session: Session
) -> None:
    client.post("/knowledge-base/company", data={"name": "First"}, follow_redirects=False)
    client.post("/knowledge-base/company", data={"name": "Second"}, follow_redirects=False)
    assert len(list(committed_session.scalars(select(SellerProfile)).all())) == 1


# --- 4. Offerings and their associations -------------------------------------


def test_an_offering_can_be_created_edited_and_archived(
    client: TestClient, committed_session: Session
) -> None:
    offering_id = _create_offering(client)

    detail = client.get(f"/knowledge-base/offerings/{offering_id}")
    assert detail.status_code == 200
    assert "Cement outlook" in detail.text

    client.post(
        f"/knowledge-base/offerings/{offering_id}",
        data={
            "name": "Cement outlook, annual",
            "offering_type": "subscription",
            "problems_addressed": "No current price view",
        },
        follow_redirects=False,
    )
    edited = client.get(f"/knowledge-base/offerings/{offering_id}")
    assert "Cement outlook, annual" in edited.text
    assert "No current price view" in edited.text

    client.post(
        f"/knowledge-base/offerings/{offering_id}/state",
        data={"action": "archive"},
        follow_redirects=False,
    )
    archived = client.get(f"/knowledge-base/offerings/{offering_id}")
    assert "This offering is archived" in archived.text
    # Hidden from the default list, visible on request.
    assert "Cement outlook, annual" not in client.get("/knowledge-base/offerings").text
    assert "Cement outlook, annual" in client.get("/knowledge-base/offerings?archived=1").text


def test_a_duplicate_active_name_is_refused_in_the_operators_words(
    client: TestClient,
) -> None:
    _create_offering(client)
    response = client.post(
        "/knowledge-base/offerings",
        data={"name": "Cement outlook", "offering_type": "other"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "already+exists" in response.headers["location"]


def test_a_proof_point_can_be_associated_and_removed_from_the_page(
    client: TestClient, committed_session: Session
) -> None:
    offering_id = _create_offering(client)
    client.post(
        "/knowledge-base/proof-points",
        data={"statement": "Covering cement since 2009."},
        follow_redirects=False,
    )
    from app.models.seller_knowledge import SellerProofPoint

    proof_point = committed_session.scalar(select(SellerProofPoint))
    assert proof_point is not None

    client.post(
        f"/knowledge-base/offerings/{offering_id}/links",
        data={"kind": "proof_point", "related_id": str(proof_point.id)},
        follow_redirects=False,
    )
    assert (
        "Covering cement since 2009." in client.get(f"/knowledge-base/offerings/{offering_id}").text
    )

    client.post(
        f"/knowledge-base/offerings/{offering_id}/links",
        data={
            "kind": "proof_point",
            "related_id": str(proof_point.id),
            "action": "remove",
        },
        follow_redirects=False,
    )
    after = client.get(f"/knowledge-base/offerings/{offering_id}")
    assert "No proof points are associated" in after.text
    # The proof point itself survived being unlinked.
    assert "Covering cement since 2009." in client.get("/knowledge-base/proof-points").text


def test_an_unknown_association_type_is_refused(client: TestClient) -> None:
    offering_id = _create_offering(client)
    response = client.post(
        f"/knowledge-base/offerings/{offering_id}/links",
        data={"kind": "contact", "related_id": "x"},
        follow_redirects=False,
    )
    assert "Unknown+association+type" in response.headers["location"]


def test_a_hand_edited_offering_id_gives_a_not_found_page_rather_than_an_error(
    client: TestClient,
) -> None:
    assert client.get("/knowledge-base/offerings/not-a-uuid").status_code == 404


# --- 5. Proof points, restrictions and personas ------------------------------


def test_a_proof_point_can_be_added_archived_and_restored(client: TestClient) -> None:
    client.post(
        "/knowledge-base/proof-points",
        data={"statement": "Covering cement since 2009.", "source_reference": "internal doc"},
        follow_redirects=False,
    )
    listing = client.get("/knowledge-base/proof-points")
    assert "Covering cement since 2009." in listing.text
    assert "unassociated" in listing.text

    from app.db.session import SessionLocal
    from app.models.seller_knowledge import SellerProofPoint

    session = SessionLocal()
    proof_point = session.scalar(select(SellerProofPoint))
    assert proof_point is not None
    record_id = proof_point.id
    session.close()

    client.post(
        f"/knowledge-base/proof-points/{record_id}/state",
        data={"action": "archive"},
        follow_redirects=False,
    )
    assert "Covering cement since 2009." not in client.get("/knowledge-base/proof-points").text
    client.post(
        f"/knowledge-base/proof-points/{record_id}/state",
        data={"action": "restore"},
        follow_redirects=False,
    )
    assert "Covering cement since 2009." in client.get("/knowledge-base/proof-points").text


def test_an_offering_scoped_restriction_says_it_restricts_nothing_yet(
    client: TestClient,
) -> None:
    client.post(
        "/knowledge-base/restricted-claims",
        data={
            "title": "No named clients",
            "explanation": "Never name a client.",
            "scope": "offering",
        },
        follow_redirects=False,
    )
    page = client.get("/knowledge-base/restricted-claims")
    assert "offering-scoped" in page.text
    assert "not linked yet" in page.text


def test_a_global_restriction_reads_as_applying_everywhere(client: TestClient) -> None:
    # Asserted against the stored record and its badge, not the word "global",
    # which the scope <select> renders on an empty page anyway.
    empty = client.get("/knowledge-base/restricted-claims").text
    assert "No guarantees" not in empty
    client.post(
        "/knowledge-base/restricted-claims",
        data={"title": "No guarantees", "explanation": "Never promise an outcome."},
        follow_redirects=False,
    )
    page = client.get("/knowledge-base/restricted-claims").text
    assert "No guarantees" in page
    assert "Applies to everything, whatever a campaign is selling." in page
    assert "not linked yet" not in page


def test_a_persona_page_says_it_is_not_a_contact(client: TestClient) -> None:
    client.post(
        "/knowledge-base/personas",
        data={"name": "Head of Strategy", "role_function": "Strategy"},
        follow_redirects=False,
    )
    page = client.get("/knowledge-base/personas")
    assert "Head of Strategy" in page.text
    assert "not a person" in page.text


def test_a_proof_point_can_be_edited_from_the_list(
    client: TestClient, committed_session: Session
) -> None:
    from app.models.seller_knowledge import SellerProofPoint

    client.post(
        "/knowledge-base/proof-points",
        data={"statement": "Since 2010."},
        follow_redirects=False,
    )
    record = committed_session.scalar(select(SellerProofPoint))
    assert record is not None
    response = client.post(
        f"/knowledge-base/proof-points/{record.id}",
        data={"statement": "Since 2009.", "source_reference": "Publication register"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = client.get("/knowledge-base/proof-points").text
    assert "Since 2009." in page
    assert "Since 2010." not in page


def test_a_persona_can_be_edited_from_the_list(
    client: TestClient, committed_session: Session
) -> None:
    from app.models.seller_knowledge import SellerPersona

    client.post(
        "/knowledge-base/personas", data={"name": "Head of Stategy"}, follow_redirects=False
    )
    persona = committed_session.scalar(select(SellerPersona))
    assert persona is not None
    client.post(
        f"/knowledge-base/personas/{persona.id}",
        data={"name": "Head of Strategy", "seniority": "Director and above"},
        follow_redirects=False,
    )
    page = client.get("/knowledge-base/personas").text
    assert "Head of Strategy" in page
    assert "Head of Stategy" not in page


def test_widening_a_restriction_from_the_page_drops_its_links_and_says_so(
    client: TestClient, committed_session: Session
) -> None:
    from app.models.seller_knowledge import SellerRestrictedClaim

    offering_id = _create_offering(client)
    client.post(
        "/knowledge-base/restricted-claims",
        data={"title": "No named clients", "explanation": "Never name one.", "scope": "offering"},
        follow_redirects=False,
    )
    claim = committed_session.scalar(select(SellerRestrictedClaim))
    assert claim is not None
    client.post(
        f"/knowledge-base/offerings/{offering_id}/links",
        data={"kind": "restricted_claim", "related_id": str(claim.id)},
        follow_redirects=False,
    )
    assert "No named clients" in client.get(f"/knowledge-base/offerings/{offering_id}").text

    response = client.post(
        f"/knowledge-base/restricted-claims/{claim.id}",
        data={"title": "No named clients", "explanation": "Never name one.", "scope": "global"},
        follow_redirects=False,
    )
    assert "associations+were+removed" in response.headers["location"]
    detail = client.get(f"/knowledge-base/offerings/{offering_id}").text
    assert "No offering-scoped restrictions are associated" in detail


def test_a_global_restriction_is_not_offered_for_an_offering(
    client: TestClient, committed_session: Session
) -> None:
    """The picker excludes it, and the service refuses it if the form is forged."""

    from app.models.seller_knowledge import SellerRestrictedClaim

    offering_id = _create_offering(client)
    client.post(
        "/knowledge-base/restricted-claims",
        data={"title": "No guarantees", "explanation": "Never promise an outcome."},
        follow_redirects=False,
    )
    claim = committed_session.scalar(select(SellerRestrictedClaim))
    assert claim is not None
    detail = client.get(f"/knowledge-base/offerings/{offering_id}").text
    assert f'value="{claim.id}"' not in detail
    response = client.post(
        f"/knowledge-base/offerings/{offering_id}/links",
        data={"kind": "restricted_claim", "related_id": str(claim.id)},
        follow_redirects=False,
    )
    assert "applies+to+everything+already" in response.headers["location"]


def test_every_seller_write_records_an_operator(
    client: TestClient, committed_session: Session
) -> None:
    """The author columns are populated, not left permanently NULL."""

    from app.models.seller_knowledge import SellerOffering as Offering

    client.post("/knowledge-base/company", data={"name": "VMR"}, follow_redirects=False)
    _create_offering(client)
    profile = committed_session.scalar(select(SellerProfile))
    offering = committed_session.scalar(select(Offering))
    assert profile is not None and profile.updated_by == "operator"
    assert offering is not None and offering.created_by == "operator"


# --- 6. The campaign association ---------------------------------------------


def test_an_offering_can_be_added_to_and_removed_from_a_campaign(
    client: TestClient, committed_session: Session
) -> None:
    offering_id = _create_offering(client)
    campaign_id = _create_campaign(client)

    response = client.post(
        f"/campaigns/{campaign_id}/offerings",
        data={"offering_id": offering_id},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = client.get(f"/campaigns/{campaign_id}")
    assert "Cement outlook" in page.text
    assert "Offerings this campaign concerns" in page.text

    client.post(f"/campaigns/{campaign_id}/offerings/{offering_id}/remove", follow_redirects=False)
    after = client.get(f"/campaigns/{campaign_id}")
    assert "This campaign names no offerings" in after.text
    assert committed_session.scalar(select(CampaignOffering)) is None
    # The offering itself is untouched.
    assert committed_session.scalar(select(SellerOffering)) is not None


def test_adding_an_offering_never_changes_the_campaign_itself(
    client: TestClient, committed_session: Session
) -> None:
    """The association is organisational; it writes no campaign content."""

    offering_id = _create_offering(client)
    campaign_id = _create_campaign(client)
    before = committed_session.scalar(select(Campaign))
    assert before is not None
    name_before, description_before, status_before = (
        before.name,
        before.description,
        before.status,
    )

    client.post(
        f"/campaigns/{campaign_id}/offerings",
        data={"offering_id": offering_id},
        follow_redirects=False,
    )
    committed_session.expire_all()
    after = committed_session.scalar(select(Campaign))
    assert after is not None
    assert (after.name, after.description, after.status) == (
        name_before,
        description_before,
        status_before,
    )


def test_a_campaign_keeps_showing_an_offering_that_was_archived_afterwards(
    client: TestClient,
) -> None:
    offering_id = _create_offering(client)
    campaign_id = _create_campaign(client)
    client.post(
        f"/campaigns/{campaign_id}/offerings",
        data={"offering_id": offering_id},
        follow_redirects=False,
    )
    client.post(
        f"/knowledge-base/offerings/{offering_id}/state",
        data={"action": "archive"},
        follow_redirects=False,
    )
    page = client.get(f"/campaigns/{campaign_id}")
    assert "Cement outlook" in page.text
    assert "archived" in page.text


def test_submitting_no_offering_asks_for_one_rather_than_failing(client: TestClient) -> None:
    campaign_id = _create_campaign(client)
    response = client.post(
        f"/campaigns/{campaign_id}/offerings", data={"offering_id": ""}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "Choose+an+offering+first" in response.headers["location"]


def test_the_campaign_page_shows_the_deterministic_readiness_rows(client: TestClient) -> None:
    campaign_id = _create_campaign(client)
    page = client.get(f"/campaigns/{campaign_id}")
    assert "Offering associations" in page.text
    assert "Campaign messaging and CTA" in page.text
    # The campaign record has no messaging columns yet, and the page says so.
    assert "not applicable" in page.text


# --- 7. The seller/prospect boundary is visible ------------------------------


def test_the_overview_states_that_this_is_seller_knowledge_not_research(
    client: TestClient,
) -> None:
    page = client.get("/knowledge-base")
    assert "seller knowledge, not prospect research" in page.text


def test_archiving_an_offering_leaves_prospect_records_alone(
    client: TestClient, committed_session: Session
) -> None:
    """Nothing in this area touches contacts, companies, or evidence."""

    from app.models.company import Company
    from app.models.contact import Contact
    from app.models.insight import Insight

    offering_id = _create_offering(client)
    client.post(
        f"/knowledge-base/offerings/{offering_id}/state",
        data={"action": "archive"},
        follow_redirects=False,
    )
    assert committed_session.scalar(select(Contact)) is None
    assert committed_session.scalar(select(Company)) is None
    assert committed_session.scalar(select(Insight)) is None
    stored = committed_session.scalar(select(SellerOffering))
    assert stored is not None
    assert stored.state is SellerRecordState.ARCHIVED
