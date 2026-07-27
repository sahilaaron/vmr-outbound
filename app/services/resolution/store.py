"""Reading and writing company-domain resolution decisions (DAT-017A).

The narrow layer between the policy and everything else. It knows the decision
model and nothing about how a decision is reached, which is what lets
:mod:`app.services.captures.promotion` and
:mod:`app.services.resolution.gates` read decisions without importing the
orchestration that writes them.

One rule lives here rather than in a caller: **a decision row is never updated
in place.** ``record`` supersedes the current row and inserts a new one, and it
declines to insert at all when the new decision says exactly what the current
one already says. That is what makes recalculation idempotent — not a check
somewhere upstream that a future caller might forget.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.company_domain_resolution import CompanyDomainResolution
from app.models.enums import DomainResolutionKind, DomainResolutionState
from app.services.resolution import policy


def current_decision(session: Session, capture_id: uuid.UUID) -> CompanyDomainResolution | None:
    """The live decision for a capture, or None if it has never been resolved."""

    return session.scalars(
        select(CompanyDomainResolution).where(
            CompanyDomainResolution.capture_id == capture_id,
            CompanyDomainResolution.is_current.is_(True),
        )
    ).first()


def decision_history(session: Session, capture_id: uuid.UUID) -> list[CompanyDomainResolution]:
    """Every decision ever made for a capture, newest first.

    Superseded rows are included deliberately: the history is the audit, and a
    correction is only trustworthy if what it replaced is still readable.
    """

    return list(
        session.scalars(
            select(CompanyDomainResolution)
            .where(CompanyDomainResolution.capture_id == capture_id)
            .order_by(CompanyDomainResolution.decision_number.desc())
        )
    )


def current_decisions_for_company(
    session: Session, company_id: uuid.UUID
) -> list[CompanyDomainResolution]:
    """Live decisions that resolved to this company, most certain first.

    A company reached by several captures can carry several decisions, and they
    need not agree in certainty: one contact confirmed and three provisional is
    an ordinary state. Ordering by state means a caller asking "how sure are we
    about this company?" reads the strongest evidence first.
    """

    decisions = list(
        session.scalars(
            select(CompanyDomainResolution).where(
                CompanyDomainResolution.resolved_company_id == company_id,
                CompanyDomainResolution.is_current.is_(True),
            )
        )
    )
    order = {
        DomainResolutionState.CONFIRMED: 0,
        DomainResolutionState.PROVISIONAL: 1,
        DomainResolutionState.UNRESOLVED: 2,
    }
    decisions.sort(key=lambda d: (order[d.state], d.decided_at))
    return decisions


def company_state(session: Session, company_id: uuid.UUID) -> DomainResolutionState | None:
    """The strongest live resolution state recorded for a company.

    ``None`` means no automatic resolution ever produced this company — it came
    from a spreadsheet import, an operator, or a pre-DAT-017A promotion. That is
    a real and common answer, and it is deliberately not conflated with
    ``UNRESOLVED``: "this policy never spoke about it" and "this policy looked
    and could not tell" authorize different things downstream.
    """

    decisions = current_decisions_for_company(session, company_id)
    resolved = [d for d in decisions if d.state is not DomainResolutionState.UNRESOLVED]
    if resolved:
        return resolved[0].state
    return decisions[0].state if decisions else None


def record(
    session: Session,
    *,
    capture_id: uuid.UUID,
    decision: policy.PolicyDecision,
    kind: DomainResolutionKind,
    actor: str,
    enrichment_id: uuid.UUID | None = None,
    resolved_company_id: uuid.UUID | None = None,
    provider_call_made: bool = False,
    company_name_original: str | None = None,
    company_name_normalized: str | None = None,
    correction_note: str | None = None,
) -> tuple[CompanyDomainResolution, bool]:
    """Persist *decision*, superseding the current one, unless nothing changed.

    Returns ``(row, created)``. ``created`` is False when the current decision
    already says the same thing — same state, same domain, same reasons, same
    policy version — in which case nothing is written at all and the existing
    row is returned untouched, including its original ``decided_at``. That is
    what stops a recalculation loop from filling the table with identical rows
    and from making a decision look newer than the evidence behind it.

    A correction is always written even when it agrees with the current
    decision: an operator affirming a domain by hand is new evidence about the
    domain, not a repeat of the automatic reasoning that produced it.
    """

    existing = current_decision(session, capture_id)
    if (
        existing is not None
        and kind is not DomainResolutionKind.OPERATOR_CORRECTION
        and _says_the_same_thing(existing, decision)
    ):
        # Late-arriving links are additive facts, not a changed decision: filling
        # one in does not restate the decision and must not renumber history.
        if resolved_company_id is not None and existing.resolved_company_id is None:
            existing.resolved_company_id = resolved_company_id
        if enrichment_id is not None and existing.enrichment_id is None:
            existing.enrichment_id = enrichment_id
        session.flush()
        return existing, False

    now = datetime.now(UTC)
    if existing is not None:
        existing.is_current = False
        existing.superseded_at = now
        session.flush()

    row = CompanyDomainResolution(
        capture_id=capture_id,
        enrichment_id=enrichment_id,
        resolved_company_id=resolved_company_id,
        decision_number=(existing.decision_number + 1) if existing is not None else 1,
        is_current=True,
        state=decision.state,
        decision_kind=kind,
        policy_version=decision.policy_version,
        company_name_original=company_name_original,
        company_name_normalized=company_name_normalized,
        candidates=decision.candidates_json() or None,
        selected_domain=decision.selected_domain,
        selected_candidate=decision.selected_candidate,
        provider=decision.provider,
        provider_rank=decision.provider_rank,
        reasons=list(decision.reasons),
        warnings=list(decision.warnings) or None,
        provider_call_made=provider_call_made,
        correction_note=correction_note,
        decided_by=actor,
        decided_at=now,
    )
    session.add(row)
    session.flush()
    return row, True


def _says_the_same_thing(
    existing: CompanyDomainResolution, decision: policy.PolicyDecision
) -> bool:
    """Whether a new decision would add nothing to the current one.

    Compares the conclusion *and* the reasoning. Two decisions reaching the same
    domain for different reasons are genuinely different decisions and both
    deserve a row — the reasons are what a reviewer reads, and silently keeping
    the older explanation for a newer decision would be the kind of quiet
    inaccuracy this table exists to prevent. Warnings are compared too, for the
    same reason.
    """

    return (
        existing.state is decision.state
        and existing.selected_domain == decision.selected_domain
        and existing.policy_version == decision.policy_version
        and [str(r) for r in (existing.reasons or [])] == list(decision.reasons)
        and [str(w) for w in (existing.warnings or [])] == list(decision.warnings)
    )
