"""The contact CRM web layer (APP-002).

Route smoke tests plus the behaviour that must not regress: every page and every
mutation works without a campaign, pending captures stay visible and actionable,
suppression is marked rather than hidden, and notes are append-only.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from app.core.config import get_settings
from app.main import create_app
from app.models.campaign import Campaign, CampaignContact
from app.models.contact import Contact
from app.models.contact_capture import ContactCaptureNote, ContactLabel, ContactLabelAssignment
from app.models.enums import (
    ContactWorkflowState,
    LinkedInSnapshotOutcome,
    SuppressionReason,
    SuppressionType,
)
from app.models.linkedin_profile import (
    LinkedInProfileExperienceObservation,
    LinkedInProfileSnapshot,
)
from app.services.crm import annotations as crm_annotations
from app.services.suppressions import add_suppression
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The workbench is off by default (FND-007); this test suite opts in."""

    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


def _contact(
    session: Session, first: str = "Ada", last: str = "Lovelace", **kwargs: Any
) -> Contact:
    domain = kwargs.pop("company_domain", "example.test")
    contact = Contact(
        first_name=first,
        last_name=last,
        company_name=kwargs.pop("company_name", "Example Ltd"),
        company_domain=domain,
        natural_key=f"{first}|{last}|{uuid.uuid4()}".lower(),
        **kwargs,
    )
    session.add(contact)
    session.commit()
    return contact


def _capture(
    session: Session,
    full_name: str = "Grace Hopper",
    *,
    outcome: LinkedInSnapshotOutcome = LinkedInSnapshotOutcome.UNMATCHED_STAGED,
    company: str | None = "US Navy",
    review_candidates: list[dict[str, Any]] | None = None,
) -> LinkedInProfileSnapshot:
    snapshot = LinkedInProfileSnapshot(
        client_capture_id=f"cap-{uuid.uuid4()}",
        content_hash=uuid.uuid4().hex,
        schema_version="linkedin-contact-capture/2.0.0",
        source="extension",
        normalized_profile_url=f"https://www.linkedin.com/in/{uuid.uuid4().hex[:8]}",
        extraction_status="ok",
        payload={},
        profile_fields={"full_name": full_name, "headline": "Headline"},
        outcome=outcome,
        review_candidates=review_candidates,
    )
    if company:
        snapshot.experiences.append(
            LinkedInProfileExperienceObservation(
                position_index=0,
                layout="single",
                company_name=company,
                job_title="Rear Admiral",
                is_current=True,
            )
        )
    session.add(snapshot)
    session.commit()
    return snapshot


# --------------------------------------------------------------------------
# Route smoke
# --------------------------------------------------------------------------


def test_contacts_list_renders_with_no_data(client: TestClient) -> None:
    response = client.get("/contacts")
    assert response.status_code == 200
    assert "No contacts yet" in response.text


@pytest.mark.parametrize("view", ["all", "awaiting_company", "ambiguous", "suppressed"])
def test_every_view_renders(client: TestClient, view: str) -> None:
    response = client.get(f"/contacts?view={view}")
    assert response.status_code == 200


def test_an_unknown_view_or_sort_does_not_error(client: TestClient) -> None:
    """Query strings are operator-editable; a bad one must not produce a 500."""

    response = client.get("/contacts?view=nonsense&sort=nonsense&older_than_days=-3")
    assert response.status_code == 200


def test_contact_detail_renders(client: TestClient, committed_session: Session) -> None:
    contact = _contact(committed_session)
    response = client.get(f"/contacts/{contact.id}")
    assert response.status_code == 200
    assert "Ada Lovelace" in response.text


def test_capture_detail_renders(client: TestClient, committed_session: Session) -> None:
    capture = _capture(committed_session)
    response = client.get(f"/captures/{capture.id}")
    assert response.status_code == 200
    assert "Grace Hopper" in response.text
    assert "company-domain resolution" in response.text


def test_a_missing_record_is_a_clean_not_found(client: TestClient) -> None:
    missing = uuid.uuid4()
    assert client.get(f"/contacts/{missing}").status_code == 404
    assert client.get(f"/captures/{missing}").status_code == 404
    assert client.get("/contacts/not-a-uuid").status_code == 404
    assert client.get("/captures/not-a-uuid").status_code == 404


# --------------------------------------------------------------------------
# Campaign independence — the governing rule
# --------------------------------------------------------------------------


def test_the_whole_crm_works_with_no_campaign_in_the_database(
    client: TestClient, committed_session: Session
) -> None:
    """Not one campaign exists here, and every APP-002 surface still works."""

    contact = _contact(committed_session)
    capture = _capture(committed_session)
    assert committed_session.scalar(select(func.count()).select_from(Campaign)) == 0

    assert client.get("/contacts").status_code == 200
    assert client.get(f"/contacts/{contact.id}").status_code == 200
    assert client.get(f"/captures/{capture.id}").status_code == 200
    assert (
        client.post(
            f"/contacts/{contact.id}/labels", data={"label": "Priority"}, follow_redirects=False
        ).status_code
        == 303
    )
    assert (
        client.post(
            f"/contacts/{contact.id}/notes", data={"note": "A note."}, follow_redirects=False
        ).status_code
        == 303
    )
    assert (
        client.post(
            f"/captures/{capture.id}/labels", data={"label": "Priority"}, follow_redirects=False
        ).status_code
        == 303
    )
    assert (
        client.post(
            f"/captures/{capture.id}/notes", data={"note": "A note."}, follow_redirects=False
        ).status_code
        == 303
    )
    assert committed_session.scalar(select(func.count()).select_from(Campaign)) == 0


def test_no_crm_route_offers_a_campaign_selector(
    client: TestClient, committed_session: Session
) -> None:
    """A campaign control on these pages would reintroduce the coupling."""

    contact = _contact(committed_session)
    capture = _capture(committed_session)
    for url in ("/contacts", f"/contacts/{contact.id}", f"/captures/{capture.id}"):
        body = client.get(url).text
        assert 'name="campaign_id"' not in body, f"{url} offers a campaign selector"


def test_a_contact_with_a_campaign_membership_still_renders(
    client: TestClient, committed_session: Session
) -> None:
    """Membership is shown as history; it is never required and never hides the page."""

    contact = _contact(committed_session)
    campaign = Campaign(name=f"Pilot {uuid.uuid4()}")
    committed_session.add(campaign)
    committed_session.commit()
    committed_session.add(
        CampaignContact(
            campaign_id=campaign.id,
            contact_id=contact.id,
            state=ContactWorkflowState.IMPORTED,
        )
    )
    committed_session.commit()

    body = client.get(f"/contacts/{contact.id}").text
    assert campaign.name in body


def test_a_contact_with_no_membership_says_so_rather_than_looking_broken(
    client: TestClient, committed_session: Session
) -> None:
    """The common case under the contact-first model, and it must read as normal."""

    contact = _contact(committed_session)
    body = client.get(f"/contacts/{contact.id}").text
    assert "Not in any campaign" in body
    assert "never required" in body


# --------------------------------------------------------------------------
# Pending captures stay visible and actionable
# --------------------------------------------------------------------------


def test_a_pending_capture_appears_in_the_list_and_links_to_its_page(
    client: TestClient, committed_session: Session
) -> None:
    capture = _capture(committed_session)
    body = client.get("/contacts").text
    assert "Grace Hopper" in body
    assert f"/captures/{capture.id}" in body


def test_the_awaiting_company_view_isolates_the_queue(
    client: TestClient, committed_session: Session
) -> None:
    _contact(committed_session)
    _capture(committed_session, "Grace Hopper")
    _capture(committed_session, "Alan Turing", outcome=LinkedInSnapshotOutcome.AMBIGUOUS_REVIEW)

    body = client.get("/contacts?view=awaiting_company").text
    assert "Grace Hopper" in body
    assert "Alan Turing" not in body
    assert "Ada Lovelace" not in body


def test_an_ambiguous_capture_shows_its_candidates_without_choosing(
    client: TestClient, committed_session: Session
) -> None:
    """The backend never merges on a name; the operator decides."""

    contact = _contact(committed_session, "Ada", "Lovelace")
    capture = _capture(
        committed_session,
        "Ada Lovelace",
        outcome=LinkedInSnapshotOutcome.AMBIGUOUS_REVIEW,
        review_candidates=[{"contact_id": str(contact.id)}],
    )
    body = client.get(f"/captures/{capture.id}").text
    assert "Possible matches" in body
    assert f"/contacts/{contact.id}" in body
    assert "matches more than one existing contact" in body


def test_company_resolution_is_displayed_as_not_requested_not_fabricated(
    client: TestClient, committed_session: Session
) -> None:
    """APP-002 shows truthful state and introduces no second enrichment path."""

    capture = _capture(committed_session)
    body = client.get(f"/captures/{capture.id}").text
    assert "Company resolution" in body
    assert "not requested" in body
    assert "pending" in body


# --------------------------------------------------------------------------
# Labels
# --------------------------------------------------------------------------


def test_a_label_can_be_added_and_removed_from_a_contact(
    client: TestClient, committed_session: Session
) -> None:
    contact = _contact(committed_session)
    client.post(f"/contacts/{contact.id}/labels", data={"label": "Priority"})
    assert "Priority" in client.get(f"/contacts/{contact.id}").text

    client.post(f"/contacts/{contact.id}/labels/priority/remove")
    subject = crm_annotations.resolve_subject(committed_session, contact_id=contact.id)
    assert crm_annotations.labels_for(committed_session, subject) == []


def test_a_label_can_be_applied_to_a_pending_capture(
    client: TestClient, committed_session: Session
) -> None:
    """The reason the APP-002 migration exists."""

    capture = _capture(committed_session)
    response = client.post(f"/captures/{capture.id}/labels", data={"label": "Priority"})
    assert response.status_code == 200
    subject = crm_annotations.resolve_subject(committed_session, capture_id=capture.id)
    assert [label.name for label in crm_annotations.labels_for(committed_session, subject)] == [
        "Priority"
    ]


def test_removing_a_contact_label_leaves_the_capture_anchor_untouched(
    client: TestClient, committed_session: Session
) -> None:
    """The two anchor spaces are independent — that is what the partial indexes buy."""

    contact = _contact(committed_session)
    capture = _capture(committed_session)
    client.post(f"/contacts/{contact.id}/labels", data={"label": "Priority"})
    client.post(f"/captures/{capture.id}/labels", data={"label": "Priority"})

    client.post(f"/contacts/{contact.id}/labels/priority/remove")

    capture_subject = crm_annotations.resolve_subject(committed_session, capture_id=capture.id)
    assert [
        label.name for label in crm_annotations.labels_for(committed_session, capture_subject)
    ] == ["Priority"]


def test_applying_the_same_label_twice_is_a_no_op_not_an_error(
    client: TestClient, committed_session: Session
) -> None:
    contact = _contact(committed_session)
    client.post(f"/contacts/{contact.id}/labels", data={"label": "Priority"})
    response = client.post(f"/contacts/{contact.id}/labels", data={"label": "Priority"})
    assert response.status_code == 200

    count = committed_session.scalar(
        select(func.count())
        .select_from(ContactLabelAssignment)
        .where(ContactLabelAssignment.contact_id == contact.id)
    )
    assert count == 1


def test_label_names_that_differ_only_in_style_reuse_one_registry_row(
    client: TestClient, committed_session: Session
) -> None:
    """'Venture Capital', 'venture-capital' and 'VENTURE  CAPITAL' are one label."""

    first = _contact(committed_session, "Ada", "Lovelace")
    second = _contact(committed_session, "Grace", "Hopper")
    client.post(f"/contacts/{first.id}/labels", data={"label": "Venture Capital"})
    client.post(f"/contacts/{second.id}/labels", data={"label": "venture-capital"})

    assert committed_session.scalar(select(func.count()).select_from(ContactLabel)) == 1


def test_an_empty_label_is_refused_with_a_readable_message(
    client: TestClient, committed_session: Session
) -> None:
    contact = _contact(committed_session)
    response = client.post(
        f"/contacts/{contact.id}/labels", data={"label": "   "}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "err=" in response.headers["location"]
    assert committed_session.scalar(select(func.count()).select_from(ContactLabelAssignment)) == 0


def test_filtering_by_label_returns_both_kinds(
    client: TestClient, committed_session: Session
) -> None:
    contact = _contact(committed_session)
    capture = _capture(committed_session)
    _capture(committed_session, "Unlabelled Person")
    client.post(f"/contacts/{contact.id}/labels", data={"label": "Priority"})
    client.post(f"/captures/{capture.id}/labels", data={"label": "Priority"})

    body = client.get("/contacts?label=priority").text
    assert "Ada Lovelace" in body
    assert "Grace Hopper" in body
    assert "Unlabelled Person" not in body


# --------------------------------------------------------------------------
# Notes — append only
# --------------------------------------------------------------------------


def test_notes_accumulate_and_never_overwrite(
    client: TestClient, committed_session: Session
) -> None:
    contact = _contact(committed_session)
    client.post(f"/contacts/{contact.id}/notes", data={"note": "First observation."})
    client.post(f"/contacts/{contact.id}/notes", data={"note": "Correction: second."})

    subject = crm_annotations.resolve_subject(committed_session, contact_id=contact.id)
    notes = [note.note_text for note in crm_annotations.notes_for(committed_session, subject)]
    assert notes == ["First observation.", "Correction: second."]

    body = client.get(f"/contacts/{contact.id}").text
    assert "First observation." in body
    assert "Correction: second." in body


def test_a_note_can_be_added_to_a_pending_capture(
    client: TestClient, committed_session: Session
) -> None:
    capture = _capture(committed_session)
    client.post(f"/captures/{capture.id}/notes", data={"note": "Worth chasing."})
    assert "Worth chasing." in client.get(f"/captures/{capture.id}").text


def test_a_contact_with_no_capture_can_still_carry_a_note(
    client: TestClient, committed_session: Session
) -> None:
    """The second gap the APP-002 migration closed: a spreadsheet-only contact."""

    contact = _contact(committed_session)
    client.post(f"/contacts/{contact.id}/notes", data={"note": "Imported from CSV."})

    note = committed_session.scalars(
        select(ContactCaptureNote).where(ContactCaptureNote.contact_id == contact.id)
    ).one()
    assert note.capture_id is None


def test_an_empty_note_is_refused(client: TestClient, committed_session: Session) -> None:
    contact = _contact(committed_session)
    response = client.post(
        f"/contacts/{contact.id}/notes", data={"note": "  "}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "err=" in response.headers["location"]
    assert committed_session.scalar(select(func.count()).select_from(ContactCaptureNote)) == 0


def test_a_capture_note_stays_visible_after_the_contact_is_promoted(
    client: TestClient, committed_session: Session
) -> None:
    """Operator context written during intake must survive promotion."""

    contact = _contact(committed_session)
    capture = _capture(committed_session)
    client.post(f"/captures/{capture.id}/notes", data={"note": "Met at a conference."})

    capture.matched_contact_id = contact.id
    capture.outcome = LinkedInSnapshotOutcome.EXACT_MATCH_REFRESHED
    committed_session.commit()

    assert "Met at a conference." in client.get(f"/contacts/{contact.id}").text


# --------------------------------------------------------------------------
# Suppression stays authoritative and visible
# --------------------------------------------------------------------------


def test_a_suppressed_contact_is_visible_and_clearly_marked(
    client: TestClient, committed_session: Session
) -> None:
    contact = _contact(committed_session, email="ada@example.test")
    add_suppression(
        committed_session,
        suppression_type=SuppressionType.EMAIL,
        value="ada@example.test",
        reason=SuppressionReason.OPT_OUT,
        actor="test",
    )
    committed_session.commit()

    listing = client.get("/contacts").text
    assert "Ada Lovelace" in listing
    assert "suppressed" in listing

    detail = client.get(f"/contacts/{contact.id}").text
    assert "suppression ledger" in detail
    assert "email opt_out" in detail


def test_no_crm_action_can_lift_a_suppression(
    client: TestClient, committed_session: Session
) -> None:
    """Labelling or annotating a suppressed person must not unblock them."""

    contact = _contact(committed_session, email="ada@example.test")
    add_suppression(
        committed_session,
        suppression_type=SuppressionType.EMAIL,
        value="ada@example.test",
        reason=SuppressionReason.OPT_OUT,
        actor="test",
    )
    committed_session.commit()

    client.post(f"/contacts/{contact.id}/labels", data={"label": "Priority"})
    client.post(f"/contacts/{contact.id}/notes", data={"note": "Still interested?"})

    assert "suppression ledger" in client.get(f"/contacts/{contact.id}").text


# --------------------------------------------------------------------------
# Separate workflow states are shown, and nothing is fabricated
# --------------------------------------------------------------------------


def test_the_detail_page_shows_four_separate_dimensions(
    client: TestClient, committed_session: Session
) -> None:
    contact = _contact(committed_session)
    body = client.get(f"/contacts/{contact.id}").text
    for heading in ("Identity", "Research", "Qualification", "Email", "Suppression"):
        assert heading in body


def test_research_and_qualification_state_that_nothing_has_run(
    client: TestClient, committed_session: Session
) -> None:
    """No fabricated research or qualification data (#158)."""

    contact = _contact(committed_session)
    body = client.get(f"/contacts/{contact.id}").text
    assert "not requested" in body
    assert "not assessed" in body
    assert "No research has been requested" in body
    assert "has not been assessed" in body


def test_evidence_and_provenance_are_inspectable(
    client: TestClient, committed_session: Session
) -> None:
    contact = _contact(committed_session)
    capture = _capture(committed_session, "Ada Lovelace")
    capture.matched_contact_id = contact.id
    capture.outcome = LinkedInSnapshotOutcome.EXACT_MATCH_REFRESHED
    capture.adapter_version = "profile-adapter/3.1.0"
    committed_session.commit()

    body = client.get(f"/contacts/{contact.id}").text
    assert "Captures &amp; evidence" in body or "Captures & evidence" in body
    assert "linkedin-contact-capture/2.0.0" in body
    assert "profile-adapter/3.1.0" in body
    assert f"/profiles/{capture.id}" in body


# --------------------------------------------------------------------------
# Existing surfaces still work
# --------------------------------------------------------------------------


@pytest.mark.parametrize("url", ["/", "/campaigns", "/imports", "/review"])
def test_existing_pages_still_load(client: TestClient, url: str) -> None:
    """APP-002 must not break the surfaces it did not set out to change."""

    assert client.get(url).status_code == 200
