"""Offerings, proof points, restricted claims and personas (KB-001).

Four record types with the same lifecycle — create, edit, archive, restore —
and the same rule: an operator entering a record is the authorization for it.
There is no review state, no approval step, and no AI in this module. Nothing
generates, rewrites, enriches, or scores a seller record.

Archive is a state flip, never a delete. That is what makes the campaign
association safe: a campaign that references an offering keeps resolving to the
same row after the offering is withdrawn, so no historical campaign is ever
left pointing at nothing.

The caller owns the transaction boundary; nothing here commits.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.enums import SellerClaimScope, SellerOfferingType, SellerRecordState
from app.models.seller_knowledge import (
    SellerOffering,
    SellerOfferingPersona,
    SellerOfferingProofPoint,
    SellerOfferingRestrictedClaim,
    SellerPersona,
    SellerProofPoint,
    SellerRestrictedClaim,
)
from app.services.audit import record_audit_event
from app.services.seller.common import (
    OPERATOR_ACTOR,
    SellerKnowledgeError,
    clean_list,
    optional_text,
    required_text,
)

# The four record types, and the association model that links each to an
# offering. Keeping this in one mapping is what lets the link/unlink functions
# below be one implementation instead of three near-identical ones.
_LINK_MODELS: dict[str, tuple[Any, Any, str]] = {
    "proof_point": (SellerOfferingProofPoint, SellerProofPoint, "proof_point_id"),
    "restricted_claim": (
        SellerOfferingRestrictedClaim,
        SellerRestrictedClaim,
        "restricted_claim_id",
    ),
    "persona": (SellerOfferingPersona, SellerPersona, "persona_id"),
}


@dataclass(frozen=True)
class RecordCounts:
    """How many records of one type exist, split by state."""

    active: int
    archived: int

    @property
    def total(self) -> int:
        return self.active + self.archived


def _archive_transition(
    session: Session,
    record: SellerOffering | SellerProofPoint | SellerRestrictedClaim | SellerPersona,
    *,
    target: SellerRecordState,
    entity_type: str,
    label: str,
    actor: str | None,
) -> bool:
    """Flip a record's state, reporting whether anything actually changed.

    Re-archiving something already archived is success, not an error — the
    operator's intent is already satisfied — but it writes no audit event,
    because nothing happened.
    """

    previous = record.state
    if previous is target:
        return False
    record.state = target
    record.archived_at = datetime.now(UTC) if target is SellerRecordState.ARCHIVED else None
    session.flush()
    verb = "archived" if target is SellerRecordState.ARCHIVED else "restored"
    record_audit_event(
        session,
        actor=actor or OPERATOR_ACTOR,
        action=f"{entity_type}.{verb}",
        entity_type=entity_type,
        entity_id=str(record.id),
        previous_state=previous.value,
        new_state=target.value,
        reason=(
            f"Operator withdrew the {label} from use."
            if target is SellerRecordState.ARCHIVED
            else f"Operator returned the {label} to use."
        ),
    )
    return True


# --- Offerings ---------------------------------------------------------------


def list_offerings(session: Session, *, include_archived: bool = False) -> list[SellerOffering]:
    """Return offerings, active first, newest first within each state."""

    statement = select(SellerOffering)
    if not include_archived:
        statement = statement.where(SellerOffering.state == SellerRecordState.ACTIVE)
    statement = statement.order_by(SellerOffering.state, SellerOffering.name)
    return list(session.scalars(statement).all())


def get_offering(session: Session, offering_id: uuid.UUID) -> SellerOffering | None:
    return session.get(SellerOffering, offering_id)


def create_offering(
    session: Session,
    *,
    name: str,
    offering_type: SellerOfferingType = SellerOfferingType.OTHER,
    short_description: str | None = None,
    description: str | None = None,
    problems_addressed: list[str] | None = None,
    use_cases: list[str] | None = None,
    differentiators: list[str] | None = None,
    notes: str | None = None,
    created_by: str | None = None,
) -> SellerOffering:
    """Create an offering. Names must be unique among active offerings."""

    cleaned_name = required_text(name, field="name", label="Offering name")
    _refuse_duplicate_active_name(session, SellerOffering, cleaned_name, label="offering")
    offering = SellerOffering(
        name=cleaned_name,
        offering_type=offering_type,
        short_description=optional_text(
            short_description, field="short_description", label="Short description"
        ),
        description=optional_text(description, field="description", label="Description"),
        problems_addressed=clean_list(problems_addressed, label="Problems addressed"),
        use_cases=clean_list(use_cases, label="Use cases"),
        differentiators=clean_list(differentiators, label="Differentiators"),
        notes=optional_text(notes, field="notes", label="Notes"),
        state=SellerRecordState.ACTIVE,
        created_by=optional_text(created_by, field="created_by", label="Created by"),
    )
    session.add(offering)
    session.flush()
    record_audit_event(
        session,
        actor=created_by or OPERATOR_ACTOR,
        action="seller_offering.created",
        entity_type="seller_offering",
        entity_id=str(offering.id),
        new_state=offering.state.value,
        reason="Operator added an offering to the knowledge base.",
        context={"name": offering.name, "offering_type": offering.offering_type.value},
    )
    return offering


def update_offering(
    session: Session,
    offering: SellerOffering,
    *,
    name: str,
    offering_type: SellerOfferingType,
    short_description: str | None = None,
    description: str | None = None,
    problems_addressed: list[str] | None = None,
    use_cases: list[str] | None = None,
    differentiators: list[str] | None = None,
    notes: str | None = None,
    actor: str | None = None,
) -> SellerOffering:
    """Edit an offering in place. Editing never touches its associations."""

    cleaned_name = required_text(name, field="name", label="Offering name")
    if cleaned_name.casefold() != offering.name.casefold():
        _refuse_duplicate_active_name(session, SellerOffering, cleaned_name, label="offering")
    offering.name = cleaned_name
    offering.offering_type = offering_type
    offering.short_description = optional_text(
        short_description, field="short_description", label="Short description"
    )
    offering.description = optional_text(description, field="description", label="Description")
    offering.problems_addressed = clean_list(problems_addressed, label="Problems addressed")
    offering.use_cases = clean_list(use_cases, label="Use cases")
    offering.differentiators = clean_list(differentiators, label="Differentiators")
    offering.notes = optional_text(notes, field="notes", label="Notes")
    session.flush()
    record_audit_event(
        session,
        actor=actor or OPERATOR_ACTOR,
        action="seller_offering.updated",
        entity_type="seller_offering",
        entity_id=str(offering.id),
        reason="Operator edited an offering.",
        context={"name": offering.name},
    )
    return offering


def archive_offering(
    session: Session, offering: SellerOffering, *, actor: str | None = None
) -> bool:
    """Withdraw an offering. Campaigns that reference it are untouched."""

    return _archive_transition(
        session,
        offering,
        target=SellerRecordState.ARCHIVED,
        entity_type="seller_offering",
        label="offering",
        actor=actor,
    )


def restore_offering(
    session: Session, offering: SellerOffering, *, actor: str | None = None
) -> bool:
    """Return an archived offering to use, refusing to duplicate a live name."""

    if offering.state is SellerRecordState.ARCHIVED:
        _refuse_duplicate_active_name(session, SellerOffering, offering.name, label="offering")
    return _archive_transition(
        session,
        offering,
        target=SellerRecordState.ACTIVE,
        entity_type="seller_offering",
        label="offering",
        actor=actor,
    )


def _refuse_duplicate_active_name(
    session: Session,
    model: type[SellerOffering] | type[SellerPersona],
    name: str,
    *,
    label: str,
) -> None:
    """Refuse a name already held by an active record, in the operator's words.

    The partial unique index would refuse it too, but as an IntegrityError that
    aborts the transaction and reads like a database problem. Checking first
    means the operator gets a sentence and keeps everything else they typed.
    """

    # Two unrelated models share this guard and their only common ancestor is
    # ``Base``, so the columns are reached through a widened alias.
    columns: Any = model
    existing: Any = session.scalars(
        select(columns).where(
            func.lower(columns.name) == name.casefold(),
            columns.state == SellerRecordState.ACTIVE,
        )
    ).first()
    if existing is not None:
        raise SellerKnowledgeError(
            f"An active {label} named “{existing.name}” already exists. "
            f"Rename this one, or edit the existing {label}."
        )


# --- Proof points ------------------------------------------------------------


def list_proof_points(
    session: Session, *, include_archived: bool = False
) -> list[SellerProofPoint]:
    statement = select(SellerProofPoint)
    if not include_archived:
        statement = statement.where(SellerProofPoint.state == SellerRecordState.ACTIVE)
    return list(
        session.scalars(
            statement.order_by(SellerProofPoint.state, SellerProofPoint.created_at.desc())
        ).all()
    )


def get_proof_point(session: Session, proof_point_id: uuid.UUID) -> SellerProofPoint | None:
    return session.get(SellerProofPoint, proof_point_id)


def create_proof_point(
    session: Session,
    *,
    statement: str,
    supporting_detail: str | None = None,
    source_reference: str | None = None,
    created_by: str | None = None,
) -> SellerProofPoint:
    """Record a factual statement the operator is prepared to stand behind."""

    proof_point = SellerProofPoint(
        statement=required_text(statement, field="statement", label="Statement"),
        supporting_detail=optional_text(
            supporting_detail, field="supporting_detail", label="Supporting detail"
        ),
        source_reference=optional_text(
            source_reference, field="source_reference", label="Source or internal reference"
        ),
        state=SellerRecordState.ACTIVE,
        created_by=optional_text(created_by, field="created_by", label="Created by"),
    )
    session.add(proof_point)
    session.flush()
    record_audit_event(
        session,
        actor=created_by or OPERATOR_ACTOR,
        action="seller_proof_point.created",
        entity_type="seller_proof_point",
        entity_id=str(proof_point.id),
        new_state=proof_point.state.value,
        reason="Operator added a proof point to the knowledge base.",
    )
    return proof_point


def update_proof_point(
    session: Session,
    proof_point: SellerProofPoint,
    *,
    statement: str,
    supporting_detail: str | None = None,
    source_reference: str | None = None,
    actor: str | None = None,
) -> SellerProofPoint:
    proof_point.statement = required_text(statement, field="statement", label="Statement")
    proof_point.supporting_detail = optional_text(
        supporting_detail, field="supporting_detail", label="Supporting detail"
    )
    proof_point.source_reference = optional_text(
        source_reference, field="source_reference", label="Source or internal reference"
    )
    session.flush()
    record_audit_event(
        session,
        actor=actor or OPERATOR_ACTOR,
        action="seller_proof_point.updated",
        entity_type="seller_proof_point",
        entity_id=str(proof_point.id),
        reason="Operator edited a proof point.",
    )
    return proof_point


def archive_proof_point(
    session: Session, proof_point: SellerProofPoint, *, actor: str | None = None
) -> bool:
    return _archive_transition(
        session,
        proof_point,
        target=SellerRecordState.ARCHIVED,
        entity_type="seller_proof_point",
        label="proof point",
        actor=actor,
    )


def restore_proof_point(
    session: Session, proof_point: SellerProofPoint, *, actor: str | None = None
) -> bool:
    return _archive_transition(
        session,
        proof_point,
        target=SellerRecordState.ACTIVE,
        entity_type="seller_proof_point",
        label="proof point",
        actor=actor,
    )


# --- Restricted claims -------------------------------------------------------


def list_restricted_claims(
    session: Session, *, include_archived: bool = False
) -> list[SellerRestrictedClaim]:
    statement = select(SellerRestrictedClaim)
    if not include_archived:
        statement = statement.where(SellerRestrictedClaim.state == SellerRecordState.ACTIVE)
    return list(
        session.scalars(
            statement.order_by(
                SellerRestrictedClaim.state,
                SellerRestrictedClaim.scope,
                SellerRestrictedClaim.title,
            )
        ).all()
    )


def get_restricted_claim(session: Session, claim_id: uuid.UUID) -> SellerRestrictedClaim | None:
    return session.get(SellerRestrictedClaim, claim_id)


def create_restricted_claim(
    session: Session,
    *,
    title: str,
    explanation: str,
    examples: list[str] | None = None,
    scope: SellerClaimScope = SellerClaimScope.GLOBAL,
    created_by: str | None = None,
) -> SellerRestrictedClaim:
    """Record something generated copy must not say.

    An offering-scoped claim is created without associations and the caller
    links offerings immediately afterwards. The scope is still recorded up
    front, because a claim meant to be narrow that is stored as global would
    quietly over-restrict every campaign — and the reverse would quietly
    under-restrict one.
    """

    claim = SellerRestrictedClaim(
        title=required_text(title, field="title", label="Restriction title"),
        explanation=required_text(explanation, field="explanation", label="Explanation"),
        examples=clean_list(examples, label="Examples"),
        scope=scope,
        state=SellerRecordState.ACTIVE,
        created_by=optional_text(created_by, field="created_by", label="Created by"),
    )
    session.add(claim)
    session.flush()
    record_audit_event(
        session,
        actor=created_by or OPERATOR_ACTOR,
        action="seller_restricted_claim.created",
        entity_type="seller_restricted_claim",
        entity_id=str(claim.id),
        new_state=claim.state.value,
        reason="Operator added a restricted claim.",
        context={"title": claim.title, "scope": claim.scope.value},
    )
    return claim


def update_restricted_claim(
    session: Session,
    claim: SellerRestrictedClaim,
    *,
    title: str,
    explanation: str,
    examples: list[str] | None = None,
    scope: SellerClaimScope,
    actor: str | None = None,
) -> SellerRestrictedClaim:
    """Edit a restricted claim.

    Widening an offering-scoped claim to global drops its offering links: they
    would otherwise sit there implying a narrowing that no longer applies.
    """

    claim.title = required_text(title, field="title", label="Restriction title")
    claim.explanation = required_text(explanation, field="explanation", label="Explanation")
    claim.examples = clean_list(examples, label="Examples")
    previous_scope = claim.scope
    claim.scope = scope
    if previous_scope is SellerClaimScope.OFFERING and scope is SellerClaimScope.GLOBAL:
        for link in session.scalars(
            select(SellerOfferingRestrictedClaim).where(
                SellerOfferingRestrictedClaim.restricted_claim_id == claim.id
            )
        ).all():
            session.delete(link)
    session.flush()
    record_audit_event(
        session,
        actor=actor or OPERATOR_ACTOR,
        action="seller_restricted_claim.updated",
        entity_type="seller_restricted_claim",
        entity_id=str(claim.id),
        previous_state=previous_scope.value,
        new_state=claim.scope.value,
        reason="Operator edited a restricted claim.",
    )
    return claim


def archive_restricted_claim(
    session: Session, claim: SellerRestrictedClaim, *, actor: str | None = None
) -> bool:
    return _archive_transition(
        session,
        claim,
        target=SellerRecordState.ARCHIVED,
        entity_type="seller_restricted_claim",
        label="restricted claim",
        actor=actor,
    )


def restore_restricted_claim(
    session: Session, claim: SellerRestrictedClaim, *, actor: str | None = None
) -> bool:
    return _archive_transition(
        session,
        claim,
        target=SellerRecordState.ACTIVE,
        entity_type="seller_restricted_claim",
        label="restricted claim",
        actor=actor,
    )


# --- Personas ----------------------------------------------------------------


def list_personas(session: Session, *, include_archived: bool = False) -> list[SellerPersona]:
    statement = select(SellerPersona)
    if not include_archived:
        statement = statement.where(SellerPersona.state == SellerRecordState.ACTIVE)
    return list(session.scalars(statement.order_by(SellerPersona.state, SellerPersona.name)).all())


def get_persona(session: Session, persona_id: uuid.UUID) -> SellerPersona | None:
    return session.get(SellerPersona, persona_id)


def create_persona(
    session: Session,
    *,
    name: str,
    role_function: str | None = None,
    seniority: str | None = None,
    responsibilities: list[str] | None = None,
    challenges: list[str] | None = None,
    use_cases: list[str] | None = None,
    messaging_notes: str | None = None,
    created_by: str | None = None,
) -> SellerPersona:
    """Create a reusable buyer persona. This is never a real person."""

    cleaned_name = required_text(name, field="name", label="Persona name")
    _refuse_duplicate_active_name(session, SellerPersona, cleaned_name, label="persona")
    persona = SellerPersona(
        name=cleaned_name,
        role_function=optional_text(role_function, field="role_function", label="Role or function"),
        seniority=optional_text(seniority, field="seniority", label="Seniority"),
        responsibilities=clean_list(responsibilities, label="Responsibilities"),
        challenges=clean_list(challenges, label="Typical challenges"),
        use_cases=clean_list(use_cases, label="Relevant use cases"),
        messaging_notes=optional_text(
            messaging_notes, field="messaging_notes", label="Messaging notes"
        ),
        state=SellerRecordState.ACTIVE,
        created_by=optional_text(created_by, field="created_by", label="Created by"),
    )
    session.add(persona)
    session.flush()
    record_audit_event(
        session,
        actor=created_by or OPERATOR_ACTOR,
        action="seller_persona.created",
        entity_type="seller_persona",
        entity_id=str(persona.id),
        new_state=persona.state.value,
        reason="Operator added a buyer persona.",
        context={"name": persona.name},
    )
    return persona


def update_persona(
    session: Session,
    persona: SellerPersona,
    *,
    name: str,
    role_function: str | None = None,
    seniority: str | None = None,
    responsibilities: list[str] | None = None,
    challenges: list[str] | None = None,
    use_cases: list[str] | None = None,
    messaging_notes: str | None = None,
    actor: str | None = None,
) -> SellerPersona:
    cleaned_name = required_text(name, field="name", label="Persona name")
    if cleaned_name.casefold() != persona.name.casefold():
        _refuse_duplicate_active_name(session, SellerPersona, cleaned_name, label="persona")
    persona.name = cleaned_name
    persona.role_function = optional_text(
        role_function, field="role_function", label="Role or function"
    )
    persona.seniority = optional_text(seniority, field="seniority", label="Seniority")
    persona.responsibilities = clean_list(responsibilities, label="Responsibilities")
    persona.challenges = clean_list(challenges, label="Typical challenges")
    persona.use_cases = clean_list(use_cases, label="Relevant use cases")
    persona.messaging_notes = optional_text(
        messaging_notes, field="messaging_notes", label="Messaging notes"
    )
    session.flush()
    record_audit_event(
        session,
        actor=actor or OPERATOR_ACTOR,
        action="seller_persona.updated",
        entity_type="seller_persona",
        entity_id=str(persona.id),
        reason="Operator edited a buyer persona.",
    )
    return persona


def archive_persona(session: Session, persona: SellerPersona, *, actor: str | None = None) -> bool:
    return _archive_transition(
        session,
        persona,
        target=SellerRecordState.ARCHIVED,
        entity_type="seller_persona",
        label="persona",
        actor=actor,
    )


def restore_persona(session: Session, persona: SellerPersona, *, actor: str | None = None) -> bool:
    if persona.state is SellerRecordState.ARCHIVED:
        _refuse_duplicate_active_name(session, SellerPersona, persona.name, label="persona")
    return _archive_transition(
        session,
        persona,
        target=SellerRecordState.ACTIVE,
        entity_type="seller_persona",
        label="persona",
        actor=actor,
    )


# --- Offering associations ---------------------------------------------------


def link_to_offering(
    session: Session,
    *,
    offering: SellerOffering,
    kind: str,
    related_id: uuid.UUID,
    actor: str | None = None,
) -> bool:
    """Associate a proof point, restricted claim, or persona with an offering.

    Returns whether a link was created. Re-linking something already linked is
    success and writes nothing: the operator's intent is already true.
    """

    link_model, related_model, column = _resolve_link(kind)
    related = session.get(related_model, related_id)
    if related is None:
        raise SellerKnowledgeError(f"That {kind.replace('_', ' ')} no longer exists.")
    if related.state is not SellerRecordState.ACTIVE:
        raise SellerKnowledgeError(
            f"That {kind.replace('_', ' ')} is archived. Restore it before linking it."
        )
    existing = session.scalars(
        select(link_model).where(
            link_model.offering_id == offering.id,
            getattr(link_model, column) == related_id,
        )
    ).first()
    if existing is not None:
        return False
    session.add(
        link_model(
            offering_id=offering.id,
            created_by=actor,
            **{column: related_id},
        )
    )
    session.flush()
    record_audit_event(
        session,
        actor=actor or OPERATOR_ACTOR,
        action=f"seller_offering.{kind}_linked",
        entity_type="seller_offering",
        entity_id=str(offering.id),
        new_state=str(related_id),
        reason=f"Operator associated a {kind.replace('_', ' ')} with an offering.",
    )
    return True


def unlink_from_offering(
    session: Session,
    *,
    offering: SellerOffering,
    kind: str,
    related_id: uuid.UUID,
    actor: str | None = None,
) -> bool:
    """Remove an association. The linked record itself is never touched."""

    link_model, _related_model, column = _resolve_link(kind)
    existing = session.scalars(
        select(link_model).where(
            link_model.offering_id == offering.id,
            getattr(link_model, column) == related_id,
        )
    ).first()
    if existing is None:
        return False
    session.delete(existing)
    session.flush()
    record_audit_event(
        session,
        actor=actor or OPERATOR_ACTOR,
        action=f"seller_offering.{kind}_unlinked",
        entity_type="seller_offering",
        entity_id=str(offering.id),
        previous_state=str(related_id),
        reason=f"Operator removed a {kind.replace('_', ' ')} association from an offering.",
    )
    return True


def _resolve_link(kind: str) -> tuple[Any, Any, str]:
    try:
        return _LINK_MODELS[kind]
    except KeyError:  # pragma: no cover - guarded by the callers' fixed literals
        raise SellerKnowledgeError(f"Unknown association type {kind!r}.") from None


def proof_points_for_offering(
    session: Session, offering_id: uuid.UUID, *, active_only: bool = False
) -> list[SellerProofPoint]:
    """Return the proof points linked to an offering."""

    statement = (
        select(SellerProofPoint)
        .join(
            SellerOfferingProofPoint,
            SellerOfferingProofPoint.proof_point_id == SellerProofPoint.id,
        )
        .where(SellerOfferingProofPoint.offering_id == offering_id)
    )
    if active_only:
        statement = statement.where(SellerProofPoint.state == SellerRecordState.ACTIVE)
    return list(session.scalars(statement.order_by(SellerProofPoint.created_at)).all())


def restricted_claims_for_offering(
    session: Session, offering_id: uuid.UUID, *, active_only: bool = False
) -> list[SellerRestrictedClaim]:
    """Return the offering-scoped restricted claims linked to an offering."""

    statement = (
        select(SellerRestrictedClaim)
        .join(
            SellerOfferingRestrictedClaim,
            SellerOfferingRestrictedClaim.restricted_claim_id == SellerRestrictedClaim.id,
        )
        .where(SellerOfferingRestrictedClaim.offering_id == offering_id)
    )
    if active_only:
        statement = statement.where(SellerRestrictedClaim.state == SellerRecordState.ACTIVE)
    return list(session.scalars(statement.order_by(SellerRestrictedClaim.title)).all())


def personas_for_offering(
    session: Session, offering_id: uuid.UUID, *, active_only: bool = False
) -> list[SellerPersona]:
    """Return the personas linked to an offering."""

    statement = (
        select(SellerPersona)
        .join(SellerOfferingPersona, SellerOfferingPersona.persona_id == SellerPersona.id)
        .where(SellerOfferingPersona.offering_id == offering_id)
    )
    if active_only:
        statement = statement.where(SellerPersona.state == SellerRecordState.ACTIVE)
    return list(session.scalars(statement.order_by(SellerPersona.name)).all())


def offerings_for_proof_point(session: Session, proof_point_id: uuid.UUID) -> list[SellerOffering]:
    """Return every offering a proof point is associated with."""

    return list(
        session.scalars(
            select(SellerOffering)
            .join(
                SellerOfferingProofPoint,
                SellerOfferingProofPoint.offering_id == SellerOffering.id,
            )
            .where(SellerOfferingProofPoint.proof_point_id == proof_point_id)
            .order_by(SellerOffering.name)
        ).all()
    )


def offerings_for_restricted_claim(session: Session, claim_id: uuid.UUID) -> list[SellerOffering]:
    """Return every offering a restricted claim is scoped to."""

    return list(
        session.scalars(
            select(SellerOffering)
            .join(
                SellerOfferingRestrictedClaim,
                SellerOfferingRestrictedClaim.offering_id == SellerOffering.id,
            )
            .where(SellerOfferingRestrictedClaim.restricted_claim_id == claim_id)
            .order_by(SellerOffering.name)
        ).all()
    )


def offerings_for_persona(session: Session, persona_id: uuid.UUID) -> list[SellerOffering]:
    """Return every offering a persona is associated with."""

    return list(
        session.scalars(
            select(SellerOffering)
            .join(
                SellerOfferingPersona,
                SellerOfferingPersona.offering_id == SellerOffering.id,
            )
            .where(SellerOfferingPersona.persona_id == persona_id)
            .order_by(SellerOffering.name)
        ).all()
    )


def counts(session: Session) -> dict[str, RecordCounts]:
    """Return per-type active/archived counts, for readiness and the overview."""

    result: dict[str, RecordCounts] = {}
    for key, model in (
        ("offerings", SellerOffering),
        ("proof_points", SellerProofPoint),
        ("restricted_claims", SellerRestrictedClaim),
        ("personas", SellerPersona),
    ):
        columns: Any = model
        rows = session.execute(select(columns.state, func.count()).group_by(columns.state)).all()
        by_state = {state: count for state, count in rows}
        result[key] = RecordCounts(
            active=by_state.get(SellerRecordState.ACTIVE, 0),
            archived=by_state.get(SellerRecordState.ARCHIVED, 0),
        )
    return result
