"""Audit event tests (FND-007)."""

from __future__ import annotations

import uuid

from app.models.audit_event import AuditEvent
from app.services.audit import record_audit_event
from sqlalchemy import select
from sqlalchemy.orm import Session


def test_record_audit_event_persists_required_fields(db_session: Session) -> None:
    event = record_audit_event(
        db_session,
        actor="operator@vmr.example",
        action="contact.suppressed",
        entity_type="contact",
        entity_id="c-123",
        previous_state="eligible",
        new_state="suppressed",
        reason="hard bounce recorded",
        context={"source": "test"},
    )

    assert isinstance(event.id, uuid.UUID)
    assert event.created_at is not None

    fetched = db_session.get(AuditEvent, event.id)
    assert fetched is not None
    assert fetched.actor == "operator@vmr.example"
    assert fetched.action == "contact.suppressed"
    assert fetched.previous_state == "eligible"
    assert fetched.new_state == "suppressed"
    assert fetched.reason == "hard bounce recorded"
    assert fetched.context == {"source": "test"}


def test_dry_run_defaults_from_settings(db_session: Session) -> None:
    # Default settings have dry_run=True, so an unspecified event is dry-run.
    event = record_audit_event(db_session, actor="svc", action="pipeline.tick")
    assert event.dry_run is True


def test_dry_run_can_be_overridden(db_session: Session) -> None:
    event = record_audit_event(db_session, actor="svc", action="saleshandy.schedule", dry_run=False)
    assert event.dry_run is False


def test_query_by_entity(db_session: Session) -> None:
    record_audit_event(
        db_session,
        actor="svc",
        action="draft.approved",
        entity_type="draft",
        entity_id="d-1",
    )
    rows = db_session.scalars(select(AuditEvent).where(AuditEvent.entity_type == "draft")).all()
    assert len(rows) == 1
    assert rows[0].entity_id == "d-1"
