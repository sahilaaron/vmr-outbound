"""Apply the freshness policy against stored field observations (DAT-005).

This is the database-facing half of field-level provenance. It appends
observations, re-runs the deterministic freshness policy over the full set for a
field, marks exactly one current winner, keeps the contact's operational column in
step with that winner, and records an audit event whenever the operational value
actually changes. The pure ordering rules live in
:mod:`app.services.provenance.freshness`; this module only does the I/O and the
side effects.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.contact import Contact
from app.models.contact_field_value import ContactFieldValue
from app.services.audit import record_audit_event
from app.services.provenance.freshness import (
    FRESHNESS_POLICY_VERSION,
    TRACKED_FIELDS,
    Observation,
    explain_decision,
    resolve_winner,
)


def _observed_at_from_export(exported_at: date | None) -> datetime | None:
    """Coerce an export *date* to the source observation *timestamp*.

    An ``exported_at`` date is the source's own statement of when it observed the
    row, so it is the field's observation time (midnight UTC on that date). When
    the source gives no export date the observation time is genuinely unknown
    (``None``) and the freshness policy handles that case explicitly — the system
    never fabricates a timestamp (DAT-005).
    """

    if exported_at is None:
        return None
    return datetime(exported_at.year, exported_at.month, exported_at.day, tzinfo=UTC)


def _to_observation(cfv: ContactFieldValue) -> Observation:
    return Observation(
        key=str(cfv.id),
        value=cfv.value,
        observed_at=cfv.observed_at,
        ingested_at=cfv.ingested_at,
        is_manual_override=cfv.is_manual_override,
    )


def _load_field_values(
    session: Session, contact_id: uuid.UUID, field_name: str
) -> list[ContactFieldValue]:
    return list(
        session.scalars(
            select(ContactFieldValue).where(
                ContactFieldValue.contact_id == contact_id,
                ContactFieldValue.field_name == field_name,
            )
        ).all()
    )


def reconcile_field(
    session: Session,
    *,
    contact: Contact,
    field_name: str,
    actor: str,
) -> ContactFieldValue | None:
    """Re-run the freshness policy for one field and apply its winner.

    Recomputes the winner across every stored observation, marks exactly one
    winner (clearing any previous winner first so the partial-unique winner index
    is never violated), stamps the current policy version and a decision reason on
    every observation, and updates the contact's operational column to the winning
    value — recording an audit event only when that value actually changes. Older
    evidence therefore never rewrites a newer operational value.
    """

    values = _load_field_values(session, contact.id, field_name)
    if not values:
        return None

    observations = [_to_observation(v) for v in values]
    winner_obs = resolve_winner(observations)
    assert winner_obs is not None  # non-empty set
    winner = next(v for v in values if str(v.id) == winner_obs.key)
    reason = explain_decision(winner_obs, observations)

    # Clear all winner flags first, flush, then set the single winner — the
    # partial unique index allows only one winner per (contact, field) at a time.
    for v in values:
        if v.is_current_winner:
            v.is_current_winner = False
    session.flush()

    for v in values:
        v.policy_version = FRESHNESS_POLICY_VERSION
        if v.id == winner.id:
            v.decision_reason = reason
        else:
            v.decision_reason = "superseded by the current winner under " + FRESHNESS_POLICY_VERSION
    winner.is_current_winner = True
    session.flush()

    old_value = getattr(contact, field_name)
    if old_value != winner.value:
        setattr(contact, field_name, winner.value)
        session.flush()
        record_audit_event(
            session,
            actor=actor,
            action="contact.field_reconciled",
            entity_type="contact",
            entity_id=str(contact.id),
            previous_state=old_value,
            new_state=winner.value,
            reason=f"{field_name}: {reason}",
            context={
                "field": field_name,
                "policy_version": FRESHNESS_POLICY_VERSION,
                "winning_observation_id": str(winner.id),
                "manual_override": winner.is_manual_override,
            },
        )
    return winner


def _append_observation(
    session: Session,
    *,
    contact_id: uuid.UUID,
    field_name: str,
    value: str | None,
    import_batch_id: uuid.UUID | None,
    import_row_id: uuid.UUID | None,
    source_name: str | None,
    source_reference: str | None,
    exported_by: str | None,
    observed_at: datetime | None,
    confidence: float | None,
    is_manual_override: bool,
    created_by: str | None,
) -> ContactFieldValue:
    cfv = ContactFieldValue(
        contact_id=contact_id,
        field_name=field_name,
        value=value,
        import_batch_id=import_batch_id,
        import_row_id=import_row_id,
        source_name=source_name,
        source_reference=source_reference,
        exported_by=exported_by,
        observed_at=observed_at,
        confidence=confidence,
        is_manual_override=is_manual_override,
        created_by=created_by,
        policy_version=FRESHNESS_POLICY_VERSION,
    )
    session.add(cfv)
    session.flush()
    return cfv


def record_import_observations(
    session: Session,
    *,
    contact: Contact,
    normalized: dict[str, str | None],
    batch_id: uuid.UUID,
    row_id: uuid.UUID,
    resolved_provenance: dict[str, Any],
    actor: str,
) -> None:
    """Record one import's observation of every tracked field, then reconcile.

    Called for each accepted or duplicate row. The row's ``exported_at`` provides
    the source observation time (or None when absent). Each tracked field gets one
    appended observation and is reconciled independently, so a newer import can
    correct a stale ``title`` while an unrelated field is untouched.
    """

    observed_at = _observed_at_from_export(resolved_provenance.get("exported_at"))
    for field_name in TRACKED_FIELDS:
        _append_observation(
            session,
            contact_id=contact.id,
            field_name=field_name,
            value=normalized.get(field_name),
            import_batch_id=batch_id,
            import_row_id=row_id,
            source_name=resolved_provenance.get("source_name"),
            source_reference=resolved_provenance.get("source_reference"),
            exported_by=resolved_provenance.get("exported_by"),
            observed_at=observed_at,
            confidence=None,
            is_manual_override=False,
            created_by=None,
        )
        reconcile_field(session, contact=contact, field_name=field_name, actor=actor)


class UnknownFieldError(ValueError):
    """Raised when a manual override targets a field that is not tracked."""


def set_manual_override(
    session: Session,
    *,
    contact: Contact,
    field_name: str,
    value: str | None,
    actor: str,
    reason: str | None = None,
    confidence: float | None = None,
    observed_at: datetime | None = None,
) -> ContactFieldValue:
    """Record an explicit manual override for one tracked field and reconcile.

    A manual override is a first-class observation that outranks all import
    evidence and stays winning until a newer manual override replaces it, so an
    operator correction is never silently undone by a later import. The override
    remains explicit and fully auditable in the field's history.
    """

    if field_name not in TRACKED_FIELDS:
        raise UnknownFieldError(
            f"{field_name!r} is not a tracked operational field; "
            f"tracked fields are: {', '.join(TRACKED_FIELDS)}"
        )

    cfv = _append_observation(
        session,
        contact_id=contact.id,
        field_name=field_name,
        value=value,
        import_batch_id=None,
        import_row_id=None,
        source_name="manual_override",
        source_reference=None,
        exported_by=None,
        observed_at=observed_at if observed_at is not None else datetime.now(UTC),
        confidence=confidence,
        is_manual_override=True,
        created_by=actor,
    )
    record_audit_event(
        session,
        actor=actor,
        action="contact.field_override",
        entity_type="contact",
        entity_id=str(contact.id),
        new_state=value,
        reason=reason or f"manual override of {field_name}",
        context={"field": field_name, "override_observation_id": str(cfv.id)},
    )
    reconcile_field(session, contact=contact, field_name=field_name, actor=actor)
    return cfv


@dataclass
class FieldProvenanceView:
    """An operator-facing explanation of one field's current value and history."""

    field_name: str
    current_value: str | None
    policy_version: str
    winner: ContactFieldValue | None = None
    win_reason: str | None = None
    observations: list[ContactFieldValue] = field(default_factory=list)


def explain_field(session: Session, *, contact: Contact, field_name: str) -> FieldProvenanceView:
    """Return the current value, the winning observation, why it won, and every
    previous value with its source and timestamps — the full audit answer to
    "why is this the value currently being used?" (DAT-005).
    """

    values = _load_field_values(session, contact.id, field_name)
    # Newest observation first for display.
    values.sort(key=lambda v: v.ingested_at, reverse=True)
    winner = next((v for v in values if v.is_current_winner), None)
    return FieldProvenanceView(
        field_name=field_name,
        current_value=winner.value if winner is not None else None,
        policy_version=FRESHNESS_POLICY_VERSION,
        winner=winner,
        win_reason=winner.decision_reason if winner is not None else None,
        observations=values,
    )
