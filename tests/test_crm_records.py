"""The contact CRM read model (APP-002).

The behaviour these tests protect is the point of the whole architecture change:
a person the operator saved stays visible even when the system has not finished
resolving them, and nothing in the CRM needs a campaign.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.models.contact import Contact
from app.models.contact_capture import ContactLabel, ContactLabelAssignment
from app.models.enums import (
    CaptureIdentityState,
    LinkedInSnapshotOutcome,
    QualificationState,
    ResearchState,
    SuppressionReason,
    SuppressionType,
)
from app.models.linkedin_profile import (
    LinkedInProfileExperienceObservation,
    LinkedInProfileSnapshot,
)
from app.services.crm.records import (
    SORT_NAME,
    VIEW_ALL,
    VIEW_AMBIGUOUS,
    VIEW_AWAITING_COMPANY,
    VIEW_SUPPRESSED,
    CrmFilters,
    list_crm_rows,
)
from app.services.suppressions import add_suppression
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


def _contact(session: Session, first: str, last: str, **kwargs: Any) -> Contact:
    """A canonical contact. Note there is no campaign anywhere in this helper."""

    domain = kwargs.pop("company_domain", "example.test")
    contact = Contact(
        first_name=first,
        last_name=last,
        company_name=kwargs.pop("company_name", "Example Ltd"),
        company_domain=domain,
        natural_key=f"{first}|{last}|{domain}|{uuid.uuid4()}".lower(),
        **kwargs,
    )
    session.add(contact)
    session.flush()
    return contact


def _capture(
    session: Session,
    full_name: str,
    *,
    outcome: LinkedInSnapshotOutcome = LinkedInSnapshotOutcome.UNMATCHED_STAGED,
    company: str | None = None,
    title: str | None = None,
    url: str | None = None,
    matched_contact_id: uuid.UUID | None = None,
) -> LinkedInProfileSnapshot:
    """One immutable capture, optionally with a current role observation."""

    snapshot = LinkedInProfileSnapshot(
        client_capture_id=f"cap-{uuid.uuid4()}",
        content_hash=uuid.uuid4().hex,
        schema_version="linkedin-contact-capture/2.0.0",
        source="extension",
        normalized_profile_url=url or f"https://www.linkedin.com/in/{uuid.uuid4().hex[:8]}",
        extraction_status="ok",
        payload={},
        profile_fields={"full_name": full_name, "headline": f"{full_name} headline"},
        outcome=outcome,
        matched_contact_id=matched_contact_id,
    )
    if company or title:
        snapshot.experiences.append(
            LinkedInProfileExperienceObservation(
                position_index=0,
                layout="single",
                company_name=company,
                job_title=title,
                is_current=True,
            )
        )
    session.add(snapshot)
    session.flush()
    return snapshot


# --------------------------------------------------------------------------
# Campaign independence — the governing product rule
# --------------------------------------------------------------------------


def test_a_contact_is_visible_without_any_campaign(db_session: Session) -> None:
    _contact(db_session, "Ada", "Lovelace")
    rows, total = list_crm_rows(db_session)
    assert total == 1
    assert rows[0].full_name == "Ada Lovelace"
    assert rows[0].kind == "contact"


def test_list_crm_rows_accepts_no_campaign_argument() -> None:
    """The signature itself must not offer a campaign, or callers will pass one."""

    import inspect

    params = set(inspect.signature(list_crm_rows).parameters)
    assert "campaign_id" not in params
    assert not any("campaign" in p for p in params)

    filter_fields = set(CrmFilters.__dataclass_fields__)
    assert not any("campaign" in f for f in filter_fields)


# --------------------------------------------------------------------------
# Pending captures stay visible
# --------------------------------------------------------------------------


def test_a_pending_capture_appears_beside_canonical_contacts(db_session: Session) -> None:
    _contact(db_session, "Ada", "Lovelace")
    _capture(db_session, "Grace Hopper", company="US Navy", title="Rear Admiral")

    rows, total = list_crm_rows(db_session, filters=CrmFilters(sort=SORT_NAME))
    assert total == 2
    assert [r.kind for r in rows] == ["contact", "pending_capture"]
    assert [r.full_name for r in rows] == ["Ada Lovelace", "Grace Hopper"]


def test_a_pending_capture_reports_its_company_from_the_current_role(
    db_session: Session,
) -> None:
    """Current employment is not in profile_fields — it comes from observations."""

    _capture(db_session, "Grace Hopper", company="US Navy", title="Rear Admiral")
    rows, _ = list_crm_rows(db_session)
    assert rows[0].company_name == "US Navy"
    assert rows[0].title == "Rear Admiral"
    # It is pending precisely because no domain was resolved.
    assert rows[0].company_domain is None


def test_a_pending_capture_says_why_it_is_pending(db_session: Session) -> None:
    _capture(db_session, "Grace Hopper")
    rows, _ = list_crm_rows(db_session)
    assert rows[0].states.identity is CaptureIdentityState.AWAITING_COMPANY
    assert "company-domain" in rows[0].warnings[0]


def test_a_resolved_capture_is_not_listed_as_pending(db_session: Session) -> None:
    """Once a capture matched a contact, the contact is the record — not the capture."""

    contact = _contact(db_session, "Ada", "Lovelace")
    _capture(
        db_session,
        "Ada Lovelace",
        outcome=LinkedInSnapshotOutcome.EXACT_MATCH_REFRESHED,
        matched_contact_id=contact.id,
    )
    rows, total = list_crm_rows(db_session)
    assert total == 1
    assert rows[0].kind == "contact"


def test_a_merged_contact_tombstone_is_not_a_person(db_session: Session) -> None:
    survivor = _contact(db_session, "Ada", "Lovelace")
    loser = _contact(db_session, "Ada", "Byron")
    loser.merged_into_id = survivor.id
    db_session.flush()

    rows, total = list_crm_rows(db_session)
    assert total == 1
    assert rows[0].record_id == survivor.id


# --------------------------------------------------------------------------
# The four views
# --------------------------------------------------------------------------


def test_awaiting_company_view_shows_only_unresolved_company_captures(
    db_session: Session,
) -> None:
    _contact(db_session, "Ada", "Lovelace")
    _capture(db_session, "Grace Hopper")
    _capture(db_session, "Alan Turing", outcome=LinkedInSnapshotOutcome.AMBIGUOUS_REVIEW)

    rows, total = list_crm_rows(db_session, filters=CrmFilters(view=VIEW_AWAITING_COMPANY))
    assert total == 1
    assert rows[0].full_name == "Grace Hopper"


def test_ambiguous_view_shows_only_captures_needing_an_identity_decision(
    db_session: Session,
) -> None:
    _capture(db_session, "Grace Hopper")
    _capture(db_session, "Alan Turing", outcome=LinkedInSnapshotOutcome.AMBIGUOUS_REVIEW)

    rows, total = list_crm_rows(db_session, filters=CrmFilters(view=VIEW_AMBIGUOUS))
    assert total == 1
    assert rows[0].full_name == "Alan Turing"
    assert rows[0].states.identity is CaptureIdentityState.AMBIGUOUS_IDENTITY


def test_suppressed_view_shows_suppressed_contacts_and_the_default_view_marks_them(
    db_session: Session,
) -> None:
    _contact(db_session, "Ada", "Lovelace", email="ada@example.test")
    _contact(db_session, "Grace", "Hopper", email="grace@example.test")
    add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value="ada@example.test",
        reason=SuppressionReason.OPT_OUT,
        actor="test",
    )
    db_session.flush()

    rows, total = list_crm_rows(db_session, filters=CrmFilters(view=VIEW_SUPPRESSED))
    assert total == 1
    assert rows[0].full_name == "Ada Lovelace"

    # A suppressed contact is still visible in the CRM — clearly marked, not hidden.
    everyone, total = list_crm_rows(db_session, filters=CrmFilters(view=VIEW_ALL))
    assert total == 2
    marked = {r.full_name: r.states.suppressed for r in everyone}
    assert marked == {"Ada Lovelace": True, "Grace Hopper": False}
    ada = next(r for r in everyone if r.full_name == "Ada Lovelace")
    assert ada.states.suppression_reason == "email opt_out"


def test_domain_suppression_marks_every_contact_at_that_company(db_session: Session) -> None:
    _contact(db_session, "Ada", "Lovelace", company_domain="blocked.test", email="a@blocked.test")
    add_suppression(
        db_session,
        suppression_type=SuppressionType.DOMAIN,
        value="blocked.test",
        reason=SuppressionReason.COMPETITOR,
        actor="test",
    )
    db_session.flush()

    rows, _ = list_crm_rows(db_session)
    assert rows[0].states.suppressed is True


# --------------------------------------------------------------------------
# Search, filters, sorting, pagination
# --------------------------------------------------------------------------


def test_search_spans_both_record_kinds(db_session: Session) -> None:
    _contact(db_session, "Ada", "Lovelace", company_name="Analytical Engines")
    _capture(db_session, "Grace Hopper", company="Analytical Machines")

    rows, total = list_crm_rows(db_session, filters=CrmFilters(search="analytical"))
    assert total == 2
    assert {r.kind for r in rows} == {"contact", "pending_capture"}


def test_search_matches_a_contact_email_and_a_capture_headline(db_session: Session) -> None:
    _contact(db_session, "Ada", "Lovelace", email="ada@findme.test")
    _capture(db_session, "Grace Hopper")

    rows, total = list_crm_rows(db_session, filters=CrmFilters(search="findme"))
    assert total == 1 and rows[0].kind == "contact"

    rows, total = list_crm_rows(db_session, filters=CrmFilters(search="hopper headline"))
    assert total == 1 and rows[0].kind == "pending_capture"


def test_has_email_true_excludes_pending_captures_entirely(db_session: Session) -> None:
    """A pending capture has no address, so it can never satisfy has_email."""

    _contact(db_session, "Ada", "Lovelace", email="ada@example.test")
    _capture(db_session, "Grace Hopper")

    rows, total = list_crm_rows(db_session, filters=CrmFilters(has_email=True))
    assert total == 1
    assert rows[0].kind == "contact"

    rows, total = list_crm_rows(db_session, filters=CrmFilters(has_email=False))
    assert total == 1
    assert rows[0].kind == "pending_capture"


def test_has_linkedin_filters_both_kinds(db_session: Session) -> None:
    _contact(db_session, "Ada", "Lovelace", linkedin_url="https://linkedin.com/in/ada")
    _contact(db_session, "Charles", "Babbage")
    _capture(db_session, "Grace Hopper")

    rows, total = list_crm_rows(db_session, filters=CrmFilters(has_linkedin=True))
    assert total == 2
    assert {r.full_name for r in rows} == {"Ada Lovelace", "Grace Hopper"}


def test_sorting_by_name_orders_across_the_union(db_session: Session) -> None:
    _capture(db_session, "Zoe Zeta")
    _contact(db_session, "Ada", "Alpha")
    _capture(db_session, "Mid Mu")

    rows, _ = list_crm_rows(db_session, filters=CrmFilters(sort=SORT_NAME))
    assert [r.full_name for r in rows] == ["Ada Alpha", "Mid Mu", "Zoe Zeta"]


def test_pagination_is_computed_over_the_union_not_per_kind(db_session: Session) -> None:
    """The regression this guards: paging two sources separately skips people."""

    for i in range(3):
        _contact(db_session, f"Contact{i}", "Person")
    for i in range(3):
        _capture(db_session, f"Capture{i} Person")

    seen: list[str] = []
    for offset in (0, 2, 4):
        page, total = list_crm_rows(
            db_session, filters=CrmFilters(sort=SORT_NAME), limit=2, offset=offset
        )
        assert total == 6
        seen.extend(r.full_name for r in page)

    assert len(seen) == 6
    assert len(set(seen)) == 6  # nobody repeated
    assert seen == sorted(seen)  # nobody skipped or reordered


def test_an_unknown_view_falls_back_to_the_default_working_set(db_session: Session) -> None:
    _contact(db_session, "Ada", "Lovelace")
    rows, total = list_crm_rows(db_session, filters=CrmFilters(view="nonsense", sort="nonsense"))
    assert total == 1 and rows


def test_page_size_is_capped(db_session: Session) -> None:
    _contact(db_session, "Ada", "Lovelace")
    _, total = list_crm_rows(db_session, limit=10_000)
    assert total == 1  # the request is clamped rather than refused


# --------------------------------------------------------------------------
# Labels — including on a pending capture, which DAT-013 could not do
# --------------------------------------------------------------------------


def _label(session: Session, slug: str, name: str) -> ContactLabel:
    label = ContactLabel(slug=slug, name=name, created_by="test")
    session.add(label)
    session.flush()
    return label


def test_a_pending_capture_can_carry_a_label(db_session: Session) -> None:
    """The APP-002 migration exists for this: pending people stay actionable."""

    capture = _capture(db_session, "Grace Hopper")
    label = _label(db_session, "priority", "Priority")
    db_session.add(
        ContactLabelAssignment(
            contact_id=None, capture_id=capture.id, label_id=label.id, source="operator"
        )
    )
    db_session.flush()

    rows, _ = list_crm_rows(db_session)
    assert rows[0].labels == ["Priority"]


def test_filtering_by_label_covers_both_anchors(db_session: Session) -> None:
    contact = _contact(db_session, "Ada", "Lovelace")
    capture = _capture(db_session, "Grace Hopper")
    other = _capture(db_session, "Alan Turing")
    label = _label(db_session, "priority", "Priority")
    db_session.add_all(
        [
            ContactLabelAssignment(contact_id=contact.id, label_id=label.id, source="operator"),
            ContactLabelAssignment(
                contact_id=None, capture_id=capture.id, label_id=label.id, source="operator"
            ),
        ]
    )
    db_session.flush()

    rows, total = list_crm_rows(db_session, filters=CrmFilters(label_slug="priority"))
    assert total == 2
    assert {r.full_name for r in rows} == {"Ada Lovelace", "Grace Hopper"}
    assert other.id not in {r.record_id for r in rows}


def test_the_same_label_cannot_be_applied_twice_to_one_contact(db_session: Session) -> None:
    contact = _contact(db_session, "Ada", "Lovelace")
    label = _label(db_session, "priority", "Priority")
    db_session.add(
        ContactLabelAssignment(contact_id=contact.id, label_id=label.id, source="operator")
    )
    db_session.flush()
    db_session.add(
        ContactLabelAssignment(contact_id=contact.id, label_id=label.id, source="operator")
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_the_same_label_cannot_be_applied_twice_to_one_capture(db_session: Session) -> None:
    """The capture anchor needs its own uniqueness, not just the contact one."""

    capture = _capture(db_session, "Grace Hopper")
    label = _label(db_session, "priority", "Priority")
    db_session.add(
        ContactLabelAssignment(
            contact_id=None, capture_id=capture.id, label_id=label.id, source="operator"
        )
    )
    db_session.flush()
    db_session.add(
        ContactLabelAssignment(
            contact_id=None, capture_id=capture.id, label_id=label.id, source="operator"
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_a_label_assignment_must_have_at_least_one_anchor(db_session: Session) -> None:
    label = _label(db_session, "priority", "Priority")
    db_session.add(
        ContactLabelAssignment(contact_id=None, capture_id=None, label_id=label.id, source="x")
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_a_contact_anchored_row_may_still_record_its_capture_as_provenance(
    db_session: Session,
) -> None:
    """The anchor check is an inclusive OR precisely so this stays legal."""

    contact = _contact(db_session, "Ada", "Lovelace")
    capture = _capture(
        db_session,
        "Ada Lovelace",
        outcome=LinkedInSnapshotOutcome.EXACT_MATCH_REFRESHED,
        matched_contact_id=contact.id,
    )
    label = _label(db_session, "priority", "Priority")
    db_session.add(
        ContactLabelAssignment(
            contact_id=contact.id, capture_id=capture.id, label_id=label.id, source="capture"
        )
    )
    db_session.flush()  # must not raise

    rows, _ = list_crm_rows(db_session)
    assert rows[0].labels == ["Priority"]


# --------------------------------------------------------------------------
# Workflow dimensions stay separate
# --------------------------------------------------------------------------


def test_the_four_dimensions_are_reported_separately(db_session: Session) -> None:
    _contact(db_session, "Ada", "Lovelace", email="ada@example.test")
    rows, _ = list_crm_rows(db_session)
    states = rows[0].states

    assert states.identity is CaptureIdentityState.CANONICAL
    # Truthful: no engine has run, so it says so rather than implying progress.
    assert states.research is ResearchState.NOT_REQUESTED
    assert states.qualification is QualificationState.NOT_ASSESSED
    assert states.email_precise is not None
    assert states.suppressed is False


def test_a_contacts_source_reflects_how_it_was_acquired(db_session: Session) -> None:
    imported = _contact(db_session, "Ada", "Lovelace")
    captured = _contact(db_session, "Grace", "Hopper")
    _capture(
        db_session,
        "Grace Hopper",
        outcome=LinkedInSnapshotOutcome.EXACT_MATCH_REFRESHED,
        matched_contact_id=captured.id,
    )

    rows, _ = list_crm_rows(db_session, filters=CrmFilters(sort=SORT_NAME))
    by_id = {r.record_id: r.source for r in rows}
    assert by_id[imported.id] == "import"
    assert by_id[captured.id] == "capture"

    rows, total = list_crm_rows(db_session, filters=CrmFilters(source="import"))
    assert total == 1 and rows[0].record_id == imported.id
