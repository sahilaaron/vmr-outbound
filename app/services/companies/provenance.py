"""Which observation currently wins a canonical company field, and why (APP-003).

The contact ledger (:mod:`app.services.provenance.service`) answers this question
for people. This answers it for companies, and reuses that module's *policy*
rather than reimplementing it: :func:`app.services.provenance.freshness.sort_key`
is a pure total order over observations, and there is no reason a company field
should age differently from a contact field. What differs is where the
observations come from and what happens to the winner, and that is what lives
here.

The rule that matters most: **research is evidence, not an unconditional
overwrite.** :func:`record_observation` appends a claim and changes nothing.
:func:`reconcile_field` is the only function that touches a canonical column, and
it does so only when the versioned policy says a different observation now wins.
A dossier that asserts an industry has not thereby set one.

The second rule: **unknown is not false.** A source that looked and found nothing
records an observation with a NULL value — a real fact about a real look. A
source that never addressed the field records nothing at all. The absence of a
row and the presence of a NULL row mean different things and neither means
"false".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.company_field_value import CompanyFieldValue
from app.models.enums import CompanyFieldSource
from app.services.audit import record_audit_event
from app.services.provenance.freshness import (
    FRESHNESS_POLICY_VERSION,
    Observation,
    explain_decision,
    resolve_winner,
)

# The canonical company columns that carry provenance.
#
# ``domain`` is absent on purpose, and its absence is the same decision the
# contact ledger made about ``email`` and ``company_domain``: changing a domain
# changes company *identity*, and identity is not a freshness question. A source
# claiming a different domain is a conflict to review, not an observation to
# out-age the current one. See :mod:`app.services.companies.conflicts`.
#
# ``name`` is absent for the same reason at one remove — it is what an operator
# recognises the company by, and letting an automatic source rewrite it would
# make the workspace unrecognisable without anyone deciding anything.
TRACKED_COMPANY_FIELDS: tuple[str, ...] = (
    "industry",
    "country",
    "company_size",
)

COMPANY_PROVENANCE_ACTOR = "system:company-provenance"


class UnknownCompanyFieldError(ValueError):
    """Raised for a field name outside :data:`TRACKED_COMPANY_FIELDS`."""


@dataclass(frozen=True)
class CompanyFieldProvenanceView:
    """Everything the workspace shows about one canonical field."""

    field_name: str
    current_value: str | None
    policy_version: str
    winner: CompanyFieldValue | None
    win_reason: str | None
    observations: list[CompanyFieldValue]


def _require_tracked(field_name: str) -> None:
    if field_name not in TRACKED_COMPANY_FIELDS:
        raise UnknownCompanyFieldError(
            f"{field_name!r} is not a provenance-tracked company field; "
            f"tracked fields are {', '.join(TRACKED_COMPANY_FIELDS)}"
        )


def _load(session: Session, company_id: uuid.UUID, field_name: str) -> list[CompanyFieldValue]:
    return list(
        session.scalars(
            select(CompanyFieldValue)
            .where(
                CompanyFieldValue.company_id == company_id,
                CompanyFieldValue.field_name == field_name,
            )
            .order_by(CompanyFieldValue.ingested_at, CompanyFieldValue.id)
        )
    )


def _to_observation(value: CompanyFieldValue) -> Observation:
    return Observation(
        key=str(value.id),
        value=value.value,
        observed_at=value.observed_at,
        ingested_at=value.ingested_at,
        is_manual_override=value.is_manual_override,
    )


def record_observation(
    session: Session,
    *,
    company: Company,
    field_name: str,
    value: str | None,
    source_kind: CompanyFieldSource,
    source_reference: str | None = None,
    dossier_version_id: uuid.UUID | None = None,
    observed_at: datetime | None = None,
    created_by: str | None = None,
    confidence: float | None = None,
) -> CompanyFieldValue:
    """Append one observation. Changes no canonical value.

    Call :func:`reconcile_field` afterwards to let the policy decide whether this
    observation should become the value in use. Keeping the two apart is what
    makes "a dossier claimed X" and "X is what we show" separately visible.
    """

    _require_tracked(field_name)
    observation = CompanyFieldValue(
        company_id=company.id,
        field_name=field_name,
        value=value,
        source_kind=source_kind,
        source_reference=source_reference,
        dossier_version_id=dossier_version_id,
        is_manual_override=source_kind is CompanyFieldSource.MANUAL,
        created_by=created_by,
        observed_at=observed_at,
        confidence=confidence,
        # Stamped properly by reconcile_field; recorded now so the column is
        # never null and an unreconciled row is still attributable.
        policy_version=FRESHNESS_POLICY_VERSION,
    )
    session.add(observation)
    session.flush()
    return observation


def reconcile_field(
    session: Session,
    *,
    company: Company,
    field_name: str,
    actor: str = COMPANY_PROVENANCE_ACTOR,
) -> CompanyFieldValue | None:
    """Re-run the freshness policy for one field and apply its winner.

    Marks exactly one winner (clearing the previous one first, because the
    partial unique index permits only one at a time), stamps the policy version
    and a decision reason on every observation, and updates the canonical column
    only when the winning value differs from what is there. Older evidence
    therefore cannot rewrite a newer canonical value, and an audit event is
    recorded only when something actually changed.
    """

    _require_tracked(field_name)
    values = _load(session, company.id, field_name)
    if not values:
        return None

    observations = [_to_observation(v) for v in values]
    winner_obs = resolve_winner(observations)
    assert winner_obs is not None  # non-empty set
    winner = next(v for v in values if str(v.id) == winner_obs.key)
    reason = explain_decision(winner_obs, observations)

    for v in values:
        if v.is_current_winner:
            v.is_current_winner = False
    session.flush()

    for v in values:
        v.policy_version = FRESHNESS_POLICY_VERSION
        v.decision_reason = (
            reason
            if v.id == winner.id
            else "superseded by the current winner under " + FRESHNESS_POLICY_VERSION
        )
    winner.is_current_winner = True
    session.flush()

    old_value = getattr(company, field_name)
    if old_value != winner.value:
        setattr(company, field_name, winner.value)
        session.flush()
        record_audit_event(
            session,
            actor=actor,
            action="company.field_reconciled",
            entity_type="company",
            entity_id=str(company.id),
            previous_state=old_value,
            new_state=winner.value,
            reason=f"{field_name}: {reason}",
            context={
                "field": field_name,
                "policy_version": FRESHNESS_POLICY_VERSION,
                "winning_observation_id": str(winner.id),
                "source_kind": winner.source_kind.value,
                "manual_override": winner.is_manual_override,
            },
        )
    return winner


def set_manual_override(
    session: Session,
    *,
    company: Company,
    field_name: str,
    value: str | None,
    actor: str,
    observed_at: datetime | None = None,
) -> CompanyFieldValue:
    """Record an operator decision and apply it.

    A manual override outranks every automatic source until a newer manual
    override replaces it. That is the operator's escape hatch from a confident
    but wrong research claim, and it is recorded as an observation rather than a
    silent edit so the disagreement stays in the history.
    """

    observation = record_observation(
        session,
        company=company,
        field_name=field_name,
        value=value,
        source_kind=CompanyFieldSource.MANUAL,
        observed_at=observed_at,
        created_by=actor,
    )
    reconcile_field(session, company=company, field_name=field_name, actor=actor)
    return observation


def explain_field(
    session: Session,
    *,
    company: Company,
    field_name: str,
) -> CompanyFieldProvenanceView:
    """Everything known about one canonical field, newest evidence first."""

    _require_tracked(field_name)
    values = _load(session, company.id, field_name)
    winner = next((v for v in values if v.is_current_winner), None)
    return CompanyFieldProvenanceView(
        field_name=field_name,
        current_value=getattr(company, field_name),
        policy_version=FRESHNESS_POLICY_VERSION,
        winner=winner,
        win_reason=winner.decision_reason if winner is not None else None,
        observations=list(reversed(values)),
    )


def explain_all(session: Session, *, company: Company) -> list[CompanyFieldProvenanceView]:
    """One view per tracked field, in a stable display order."""

    return [explain_field(session, company=company, field_name=f) for f in TRACKED_COMPANY_FIELDS]
