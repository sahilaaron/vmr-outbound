"""Suppression-ledger audit-trail, multi-reason, and enforcement tests (DAT-006).

Complements :mod:`tests.test_suppressions` (basic add/find) and
:mod:`tests.test_imports` (import-time suppression) with the DAT-006 additions:
lifecycle history, multiple reasons, unsuppress-without-losing-history,
precedence, the truthful blocked-reason decision, and enforcement on the
verification (advance-toward-outreach) path.
"""

from __future__ import annotations

import uuid

from app.core.config import get_settings
from app.models.contact import Contact
from app.models.enums import (
    SuppressionEventType,
    SuppressionReason,
    SuppressionType,
)
from app.models.suppression import Suppression, SuppressionEvent
from app.services.suppressions import (
    add_suppression,
    evaluate_suppression,
    find_active_suppression,
    find_active_suppressions,
    get_suppression_history,
    unsuppress,
)
from app.services.verification import service
from sqlalchemy import select
from sqlalchemy.orm import Session


def _contact(db: Session, *, email: str, domain: str) -> Contact:
    c = Contact(
        first_name="Jane",
        last_name="Doe",
        company_name="Acme",
        company_domain=domain,
        email=email,
        natural_key=f"jane|doe|{uuid.uuid4()}",
    )
    db.add(c)
    db.flush()
    return c


# --------------------------------------------------------------------------- #
# Audit trail                                                                  #
# --------------------------------------------------------------------------- #


def test_creating_a_suppression_records_created_event(db_session: Session) -> None:
    s = add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value="a@example.com",
        reason=SuppressionReason.OPT_OUT,
        source="saleshandy",
        created_by="ops@vmr.example",
    )
    assert s.is_active is True
    assert s.created_by == "ops@vmr.example"
    history = get_suppression_history(db_session, s.id)
    assert len(history) == 1
    assert history[0].event_type is SuppressionEventType.CREATED
    assert history[0].active_after is True


def test_unsuppress_preserves_history_and_deactivates(db_session: Session) -> None:
    s = add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value="b@example.com",
        reason=SuppressionReason.CUSTOMER,
    )
    unsuppress(db_session, s, actor="ops", notes="became a prospect again")

    assert s.is_active is False
    # The record itself is not deleted; find_active ignores it.
    assert find_active_suppression(db_session, email="b@example.com", domain=None) is None
    assert db_session.get(Suppression, s.id) is not None
    history = get_suppression_history(db_session, s.id)
    assert [e.event_type for e in history] == [
        SuppressionEventType.CREATED,
        SuppressionEventType.DEACTIVATED,
    ]
    assert history[-1].active_after is False


def test_resuppress_reactivates_same_record(db_session: Session) -> None:
    s = add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value="c@example.com",
        reason=SuppressionReason.HARD_BOUNCE,
    )
    unsuppress(db_session, s, actor="ops")
    again = add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value="c@example.com",
        reason=SuppressionReason.HARD_BOUNCE,
    )
    assert again.id == s.id  # same record, reactivated
    assert again.is_active is True
    history = get_suppression_history(db_session, s.id)
    assert [e.event_type for e in history] == [
        SuppressionEventType.CREATED,
        SuppressionEventType.DEACTIVATED,
        SuppressionEventType.REACTIVATED,
    ]


def test_add_suppression_idempotent_no_duplicate_events(db_session: Session) -> None:
    first = add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value="d@example.com",
        reason=SuppressionReason.OPT_OUT,
    )
    second = add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value="d@example.com",
        reason=SuppressionReason.OPT_OUT,
    )
    assert first.id == second.id
    # Re-adding an already-active record adds no new lifecycle event.
    assert len(get_suppression_history(db_session, first.id)) == 1


# --------------------------------------------------------------------------- #
# Multiple reasons and precedence                                             #
# --------------------------------------------------------------------------- #


def test_multiple_reasons_coexist_on_one_identity(db_session: Session) -> None:
    add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value="multi@example.com",
        reason=SuppressionReason.CUSTOMER,
    )
    add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value="multi@example.com",
        reason=SuppressionReason.COMPETITOR,
    )
    active = find_active_suppressions(db_session, email="multi@example.com", domain=None)
    assert {s.reason for s in active} == {
        SuppressionReason.CUSTOMER,
        SuppressionReason.COMPETITOR,
    }


def test_lifting_one_reason_leaves_others_blocking(db_session: Session) -> None:
    customer = add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value="two@example.com",
        reason=SuppressionReason.CUSTOMER,
    )
    add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value="two@example.com",
        reason=SuppressionReason.OPT_OUT,
    )
    unsuppress(db_session, customer, actor="ops")
    decision = evaluate_suppression(db_session, email="two@example.com", domain=None)
    assert decision.blocked is True
    assert decision.blocked_reason == "email opt_out"  # strongest remaining reason


def test_precedence_reports_strongest_reason(db_session: Session) -> None:
    add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value="p@example.com",
        reason=SuppressionReason.INTERNAL_EXCLUSION,
    )
    add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value="p@example.com",
        reason=SuppressionReason.HARD_BOUNCE,
    )
    top = find_active_suppression(db_session, email="p@example.com", domain=None)
    assert top is not None and top.reason is SuppressionReason.HARD_BOUNCE


def test_email_precedence_over_domain(db_session: Session) -> None:
    add_suppression(
        db_session,
        suppression_type=SuppressionType.DOMAIN,
        value="rival.example",
        reason=SuppressionReason.COMPETITOR,
    )
    add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value="ceo@rival.example",
        reason=SuppressionReason.OPT_OUT,
    )
    top = find_active_suppression(db_session, email="ceo@rival.example", domain="rival.example")
    assert top is not None and top.suppression_type is SuppressionType.EMAIL


def test_legal_compliance_reason_is_supported(db_session: Session) -> None:
    add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value="legal@example.com",
        reason=SuppressionReason.LEGAL_COMPLIANCE,
        notes="GDPR erasure request",
    )
    decision = evaluate_suppression(db_session, email="legal@example.com", domain=None)
    assert decision.blocked is True
    assert decision.blocked_reason == "email legal_compliance"


# --------------------------------------------------------------------------- #
# Truthful decision                                                           #
# --------------------------------------------------------------------------- #


def test_evaluate_not_blocked_for_clean_identity(db_session: Session) -> None:
    decision = evaluate_suppression(db_session, email="clean@example.com", domain="example.com")
    assert decision.blocked is False
    assert decision.blocked_reason is None
    assert decision.suppression is None


# --------------------------------------------------------------------------- #
# Enforcement on the advance-toward-outreach path (verification)              #
# --------------------------------------------------------------------------- #


def test_verification_enqueue_blocked_by_email_suppression(db_session: Session) -> None:
    contact = _contact(db_session, email="opt@acme.com", domain="acme.com")
    add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value="opt@acme.com",
        reason=SuppressionReason.OPT_OUT,
    )
    settings = get_settings()
    outcome = service.prepare_and_enqueue_contact(db_session, contact, settings=settings)
    assert outcome.blocked is True
    assert outcome.blocked_reason == "email opt_out"
    assert outcome.job is None  # no verification work was queued
    # Distinct from "needs review" (an invalid/ambiguous name), never conflated.
    assert outcome.needs_review is False


def test_verification_enqueue_blocked_by_domain_suppression(db_session: Session) -> None:
    contact = _contact(db_session, email="person@rival.example", domain="rival.example")
    add_suppression(
        db_session,
        suppression_type=SuppressionType.DOMAIN,
        value="rival.example",
        reason=SuppressionReason.COMPETITOR,
    )
    outcome = service.prepare_and_enqueue_contact(db_session, contact, settings=get_settings())
    assert outcome.blocked is True
    assert outcome.blocked_reason == "domain competitor"


def test_verification_enqueue_allows_clean_contact(db_session: Session) -> None:
    contact = _contact(db_session, email="ok@acme.com", domain="acme.com")
    outcome = service.prepare_and_enqueue_contact(db_session, contact, settings=get_settings())
    assert outcome.blocked is False
    assert outcome.job is not None


def test_lifting_suppression_reenables_verification(db_session: Session) -> None:
    contact = _contact(db_session, email="back@acme.com", domain="acme.com")
    s = add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value="back@acme.com",
        reason=SuppressionReason.MANUAL,
    )
    blocked = service.prepare_and_enqueue_contact(db_session, contact, settings=get_settings())
    assert blocked.blocked is True

    unsuppress(db_session, s, actor="ops")
    allowed = service.prepare_and_enqueue_contact(db_session, contact, settings=get_settings())
    assert allowed.blocked is False
    assert allowed.job is not None


def test_history_query_is_ordered(db_session: Session) -> None:
    s = add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value="ordered@example.com",
        reason=SuppressionReason.OPT_OUT,
    )
    unsuppress(db_session, s, actor="ops")
    add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value="ordered@example.com",
        reason=SuppressionReason.OPT_OUT,
    )
    events = db_session.scalars(
        select(SuppressionEvent).where(SuppressionEvent.suppression_id == s.id)
    ).all()
    assert len(events) == 3
    ordered = get_suppression_history(db_session, s.id)
    assert [e.event_type for e in ordered] == [
        SuppressionEventType.CREATED,
        SuppressionEventType.DEACTIVATED,
        SuppressionEventType.REACTIVATED,
    ]
