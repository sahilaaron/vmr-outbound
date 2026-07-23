"""Suppression-ledger service (DAT-006).

Adds entries to and checks against the suppression ledger. The ledger is the
authority on which identities must never enter outreach; the import pipeline
consults :func:`find_active_suppression` for every row so a suppressed identity
can never silently become eligible.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import SuppressionReason, SuppressionType
from app.models.suppression import Suppression
from app.services.audit import record_audit_event


def add_suppression(
    session: Session,
    *,
    suppression_type: SuppressionType,
    value: str,
    reason: SuppressionReason,
    source: str | None = None,
    notes: str | None = None,
    actor: str = "operator",
) -> Suppression:
    """Add (or return the existing) suppression for an identity, idempotently.

    The value is normalized to lower case. Adding the same (type, value) twice
    returns the original entry rather than raising, so re-recording a bounce or
    opt-out is safe.
    """

    normalized = value.strip().lower()
    if not normalized:
        raise ValueError("suppression value is required")

    existing = session.scalars(
        select(Suppression).where(
            Suppression.suppression_type == suppression_type,
            Suppression.value == normalized,
        )
    ).first()
    if existing is not None:
        return existing

    suppression = Suppression(
        suppression_type=suppression_type,
        value=normalized,
        reason=reason,
        source=source,
        notes=notes,
    )
    session.add(suppression)
    session.flush()

    record_audit_event(
        session,
        actor=actor,
        action="suppression.added",
        entity_type="suppression",
        entity_id=str(suppression.id),
        new_state=reason.value,
        reason=f"{suppression_type.value} suppressed: {reason.value}",
        context={"value": normalized, "source": source},
    )
    return suppression


def find_active_suppression(
    session: Session,
    *,
    email: str | None,
    domain: str | None,
) -> Suppression | None:
    """Return the suppression blocking this identity, or None.

    An exact email suppression takes precedence over a domain suppression. Both
    the email and the domain are matched against their normalized ledger values.
    """

    if email:
        email_hit = session.scalars(
            select(Suppression).where(
                Suppression.suppression_type == SuppressionType.EMAIL,
                Suppression.value == email.lower(),
            )
        ).first()
        if email_hit is not None:
            return email_hit

    if domain:
        domain_hit = session.scalars(
            select(Suppression).where(
                Suppression.suppression_type == SuppressionType.DOMAIN,
                Suppression.value == domain.lower(),
            )
        ).first()
        if domain_hit is not None:
            return domain_hit

    return None
