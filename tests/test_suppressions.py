"""Suppression-ledger tests (DAT-006)."""

from __future__ import annotations

from app.models.enums import SuppressionReason, SuppressionType
from app.services.suppressions import add_suppression, find_active_suppression
from sqlalchemy.orm import Session


def test_add_suppression_is_idempotent(db_session: Session) -> None:
    first = add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value="OptOut@Example.com",
        reason=SuppressionReason.OPT_OUT,
    )
    second = add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value="optout@example.com",
        reason=SuppressionReason.OPT_OUT,
    )
    assert first.id == second.id
    assert first.value == "optout@example.com"  # normalized lower-case


def test_find_active_suppression_by_email(db_session: Session) -> None:
    add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value="blocked@example.com",
        reason=SuppressionReason.HARD_BOUNCE,
    )
    hit = find_active_suppression(db_session, email="blocked@example.com", domain="example.com")
    assert hit is not None
    assert hit.reason is SuppressionReason.HARD_BOUNCE


def test_find_active_suppression_by_domain(db_session: Session) -> None:
    add_suppression(
        db_session,
        suppression_type=SuppressionType.DOMAIN,
        value="rival.example",
        reason=SuppressionReason.COMPETITOR,
    )
    hit = find_active_suppression(db_session, email="anyone@rival.example", domain="rival.example")
    assert hit is not None
    assert hit.suppression_type is SuppressionType.DOMAIN


def test_no_suppression_returns_none(db_session: Session) -> None:
    assert (
        find_active_suppression(db_session, email="fresh@example.com", domain="example.com") is None
    )
