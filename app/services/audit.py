"""Audit-recording service.

Single entry point for writing audit events so that every caller records the
same required fields. The ``dry_run`` flag is stamped from settings unless the
caller overrides it explicitly.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.audit_event import AuditEvent


def record_audit_event(
    session: Session,
    *,
    actor: str,
    action: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    previous_state: str | None = None,
    new_state: str | None = None,
    reason: str | None = None,
    context: dict[str, Any] | None = None,
    dry_run: bool | None = None,
) -> AuditEvent:
    """Persist an audit event and return it.

    The event is added and flushed (so it receives its id and timestamps) but not
    committed — the caller owns the transaction boundary.
    """

    event = AuditEvent(
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        previous_state=previous_state,
        new_state=new_state,
        reason=reason,
        context=context,
        dry_run=get_settings().dry_run if dry_run is None else dry_run,
    )
    session.add(event)
    session.flush()
    return event
