"""Suppression-ledger service (DAT-006).

The authoritative gate on which identities must never enter outreach. Every path
that advances a contact consults this module, and it always returns a *truthful*
blocked reason — an identity is never silently dropped or silently kept.

Key behaviours:

* **Multi-reason.** An identity can be suppressed under several reasons at once
  (e.g. both *customer* and *competitor*). Each reason is its own record;
  re-recording a reason is idempotent (it reactivates rather than duplicating).
* **History-preserving.** Unsuppressing sets a record inactive and appends a
  ``DEACTIVATED`` event; it never deletes history. Re-suppressing reactivates the
  same record and appends a ``REACTIVATED`` event.
* **Deterministic precedence.** When an identity carries several active reasons,
  the strongest reason (``SUPPRESSION_REASON_PRECEDENCE``) is reported. An exact
  email suppression outranks a whole-domain suppression.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import (
    SUPPRESSION_REASON_PRECEDENCE,
    SuppressionEventType,
    SuppressionReason,
    SuppressionType,
)
from app.models.suppression import Suppression, SuppressionEvent
from app.services.audit import record_audit_event

_REASON_RANK = {reason: rank for rank, reason in enumerate(SUPPRESSION_REASON_PRECEDENCE)}


def _record_event(
    session: Session,
    suppression: Suppression,
    *,
    event_type: SuppressionEventType,
    actor: str | None,
    notes: str | None,
) -> None:
    session.add(
        SuppressionEvent(
            suppression_id=suppression.id,
            event_type=event_type,
            reason=suppression.reason,
            source=suppression.source,
            notes=notes if notes is not None else suppression.notes,
            actor=actor,
            active_after=suppression.is_active,
        )
    )


def add_suppression(
    session: Session,
    *,
    suppression_type: SuppressionType,
    value: str,
    reason: SuppressionReason,
    source: str | None = None,
    notes: str | None = None,
    created_by: str | None = None,
    actor: str = "operator",
) -> Suppression:
    """Add (or reactivate) a suppression for one identity+reason, idempotently.

    The value is normalized to lower case. Re-recording the same ``(type, value,
    reason)`` returns the existing record: if it was active it is unchanged; if it
    had been lifted it is reactivated and a ``REACTIVATED`` event is appended, so a
    bounce or opt-out that recurs is safely re-recorded without losing history.
    """

    normalized = value.strip().lower()
    if not normalized:
        raise ValueError("suppression value is required")

    existing = session.scalars(
        select(Suppression).where(
            Suppression.suppression_type == suppression_type,
            Suppression.value == normalized,
            Suppression.reason == reason,
        )
    ).first()
    if existing is not None:
        if not existing.is_active:
            existing.is_active = True
            if source is not None:
                existing.source = source
            if notes is not None:
                existing.notes = notes
            session.flush()
            _record_event(
                session,
                existing,
                event_type=SuppressionEventType.REACTIVATED,
                actor=actor,
                notes=notes,
            )
            record_audit_event(
                session,
                actor=actor,
                action="suppression.reactivated",
                entity_type="suppression",
                entity_id=str(existing.id),
                new_state=reason.value,
                reason=f"{suppression_type.value} re-suppressed: {reason.value}",
                context={"value": normalized, "source": source},
            )
        return existing

    suppression = Suppression(
        suppression_type=suppression_type,
        value=normalized,
        reason=reason,
        source=source,
        notes=notes,
        created_by=created_by,
        is_active=True,
    )
    session.add(suppression)
    session.flush()
    _record_event(
        session,
        suppression,
        event_type=SuppressionEventType.CREATED,
        actor=actor,
        notes=notes,
    )
    record_audit_event(
        session,
        actor=actor,
        action="suppression.added",
        entity_type="suppression",
        entity_id=str(suppression.id),
        new_state=reason.value,
        reason=f"{suppression_type.value} suppressed: {reason.value}",
        context={"value": normalized, "source": source, "created_by": created_by},
    )
    return suppression


def unsuppress(
    session: Session,
    suppression: Suppression,
    *,
    actor: str = "operator",
    notes: str | None = None,
) -> Suppression:
    """Lift one suppression record without destroying its history.

    Sets the record inactive and appends a ``DEACTIVATED`` event. The record and
    its full event history remain; the identity may still be blocked by another
    active reason. Lifting an already-inactive record is a no-op.
    """

    if not suppression.is_active:
        return suppression
    suppression.is_active = False
    session.flush()
    _record_event(
        session,
        suppression,
        event_type=SuppressionEventType.DEACTIVATED,
        actor=actor,
        notes=notes,
    )
    record_audit_event(
        session,
        actor=actor,
        action="suppression.lifted",
        entity_type="suppression",
        entity_id=str(suppression.id),
        previous_state=suppression.reason.value,
        new_state="inactive",
        reason=notes or f"{suppression.suppression_type.value} suppression lifted",
        context={"value": suppression.value},
    )
    return suppression


def _reason_rank(reason: SuppressionReason) -> int:
    return _REASON_RANK.get(reason, len(_REASON_RANK))


def find_active_suppressions(
    session: Session,
    *,
    email: str | None,
    domain: str | None,
) -> list[Suppression]:
    """All active suppressions blocking this identity, strongest reason first.

    Exact email suppressions precede domain suppressions; within each, reasons are
    ordered by :data:`SUPPRESSION_REASON_PRECEDENCE`.
    """

    hits: list[Suppression] = []
    if email:
        hits.extend(
            session.scalars(
                select(Suppression).where(
                    Suppression.suppression_type == SuppressionType.EMAIL,
                    Suppression.value == email.lower(),
                    Suppression.is_active.is_(True),
                )
            ).all()
        )
    if domain:
        hits.extend(
            session.scalars(
                select(Suppression).where(
                    Suppression.suppression_type == SuppressionType.DOMAIN,
                    Suppression.value == domain.lower(),
                    Suppression.is_active.is_(True),
                )
            ).all()
        )
    # Email before domain, then by reason precedence — deterministic ordering.
    hits.sort(
        key=lambda s: (
            0 if s.suppression_type == SuppressionType.EMAIL else 1,
            _reason_rank(s.reason),
        )
    )
    return hits


def find_active_suppression(
    session: Session,
    *,
    email: str | None,
    domain: str | None,
) -> Suppression | None:
    """Return the single highest-precedence active suppression, or None.

    Backward-compatible entry point used by the import pipeline and identity
    resolution: an exact email suppression takes precedence over a domain
    suppression, and the strongest reason wins within each.
    """

    hits = find_active_suppressions(session, email=email, domain=domain)
    return hits[0] if hits else None


@dataclass(frozen=True)
class SuppressionDecision:
    """The truthful result of checking an identity against the ledger."""

    blocked: bool
    suppression: Suppression | None = None
    reason: str | None = None

    @property
    def blocked_reason(self) -> str | None:
        """A human-readable blocked reason, e.g. ``"email opt_out"``. None if not
        blocked."""

        if not self.blocked or self.suppression is None:
            return None
        return f"{self.suppression.suppression_type.value} {self.suppression.reason.value}"


def evaluate_suppression(
    session: Session,
    *,
    email: str | None,
    domain: str | None,
) -> SuppressionDecision:
    """Evaluate an identity against the ledger and return a truthful decision.

    The single enforcement primitive every advancing path should call. When
    blocked, the decision carries the specific record and a human-readable reason
    so the block is never silent and never mislabels *suppressed* as *invalid*.
    """

    hit = find_active_suppression(session, email=email, domain=domain)
    if hit is None:
        return SuppressionDecision(blocked=False)
    reason = f"{hit.suppression_type.value} {hit.reason.value}"
    return SuppressionDecision(blocked=True, suppression=hit, reason=reason)


def get_suppression_history(session: Session, suppression_id: object) -> list[SuppressionEvent]:
    """The full, ordered lifecycle history of one suppression record."""

    return list(
        session.scalars(
            select(SuppressionEvent)
            .where(SuppressionEvent.suppression_id == suppression_id)
            .order_by(SuppressionEvent.seq)
        ).all()
    )
