"""Operator identity resolution for ambiguous imports and duplicate contacts (DAT-004).

The importer never merges an uncertain match: a row whose natural key matches
several existing contacts becomes an explicit ``AMBIGUOUS`` outcome with no
contact and no campaign membership. This service is the *human* side of that
contract — it lets an operator safely resolve each ambiguity, and merge confirmed
duplicate contacts, without ever silently combining records or losing provenance.

Guarantees enforced here (never in the template or the browser):

* **No silent merge.** Every resolution is one explicit, recorded operator
  decision (assign / create / mark-separate / merge).
* **Raw rows and provenance are preserved.** The originating import row and its
  ``ImportRowValidation`` keep their ``AMBIGUOUS`` outcome forever; resolutions
  are layered on top, never destructive to import history. A merged contact is
  tombstoned (``merged_into_id``), never deleted, so its provenance survives.
* **Suppression cannot be bypassed.** If the identity is on the suppression
  ledger, any resulting membership is forced to ``SUPPRESSED`` and propagated
  across the contact's campaigns; a resolution can never route a suppressed
  identity into outreach.
* **No duplicate active membership.** Assign/merge never creates a second
  membership for the same (campaign, contact) pair.
* **Idempotent.** A repeated submission (same import row, or same idempotency
  key, or an already-merged pair) returns the existing decision and mutates
  nothing.
* **Merges are deterministic.** A merge has a defined survivor and a fixed
  transfer policy for memberships, provenance, observations, and email evidence;
  conflicting emails are refused and remain reviewable.
* **Audited.** Every applied resolution writes both an ``IdentityResolution``
  record (actor, action, reason, before/after snapshot) and an audit event.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.campaign import Campaign, CampaignContact
from app.models.contact import Contact
from app.models.enums import (
    ContactWorkflowState,
    IdentityResolutionType,
    ImportRowOutcome,
)
from app.models.identity_resolution import IdentityResolution
from app.models.import_batch import ImportBatch, ImportRow, ImportRowValidation
from app.models.provenance import ProvenanceRecord
from app.models.suppression import Suppression
from app.services.audit import record_audit_event
from app.services.contact_state import transition_contact_state
from app.services.imports import normalization as norm
from app.services.suppressions import find_active_suppression

_TERMINAL_STATES = frozenset({ContactWorkflowState.SUPPRESSED, ContactWorkflowState.EXCLUDED})
# Contact identity + optional fields copied when a resolution creates a contact.
_CONTACT_FIELDS: tuple[str, ...] = (
    "first_name",
    "last_name",
    "company_name",
    "company_domain",
    "email",
    "title",
    "linkedin_url",
    "country",
    "industry",
    "company_size",
)


class ResolutionError(Exception):
    """A resolution request is invalid, unsafe, or refers to missing records.

    Raised for malformed or unauthorized requests (unknown row, a row that is not
    ambiguous, a target that is not a real candidate, a merge with conflicting
    emails, and so on). The web layer turns this into a clear failure state; no
    partial mutation is ever committed.
    """


# --- Read side: the review queue and per-row detail --------------------------


@dataclass(frozen=True)
class QueueItem:
    """One unresolved ambiguous row awaiting an operator decision."""

    validation: ImportRowValidation
    row: ImportRow
    batch: ImportBatch
    campaign: Campaign
    candidate_count: int


def _unresolved_ambiguous_query() -> Any:
    """Rows with an AMBIGUOUS outcome that have no active resolution yet."""

    resolved_subq = select(IdentityResolution.import_row_id).where(
        IdentityResolution.import_row_id.is_not(None)
    )
    return (
        select(ImportRowValidation, ImportRow, ImportBatch, Campaign)
        .join(ImportRow, ImportRow.id == ImportRowValidation.import_row_id)
        .join(ImportBatch, ImportBatch.id == ImportRow.batch_id)
        .join(Campaign, Campaign.id == ImportBatch.campaign_id)
        .where(
            ImportRowValidation.outcome == ImportRowOutcome.AMBIGUOUS,
            ImportRowValidation.import_row_id.not_in(resolved_subq),
        )
    )


def count_open_reviews(session: Session) -> int:
    """How many ambiguous rows are still awaiting resolution."""

    subq = _unresolved_ambiguous_query().subquery()
    return session.scalar(select(func.count()).select_from(subq)) or 0


def list_review_queue(
    session: Session, *, limit: int = 50, offset: int = 0
) -> tuple[list[QueueItem], int]:
    """Paginated queue of unresolved ambiguous rows, oldest batch first."""

    total = count_open_reviews(session)
    rows = session.execute(
        _unresolved_ambiguous_query()
        .order_by(ImportBatch.created_at.desc(), ImportRow.row_number)
        .limit(limit)
        .offset(offset)
    ).all()

    items: list[QueueItem] = []
    for validation, row, batch, campaign in rows:
        candidates = _find_candidates(session, validation)
        items.append(
            QueueItem(
                validation=validation,
                row=row,
                batch=batch,
                campaign=campaign,
                candidate_count=len(candidates),
            )
        )
    return items, total


@dataclass(frozen=True)
class Candidate:
    """An existing contact this ambiguous row might be the same person as."""

    contact: Contact
    match_reason: str
    memberships: list[tuple[CampaignContact, Campaign]]
    provenance_count: int
    active_suppression: Suppression | None


@dataclass
class RowReview:
    """Everything the review-detail page shows for one ambiguous row."""

    validation: ImportRowValidation
    row: ImportRow
    batch: ImportBatch
    campaign: Campaign
    normalized: dict[str, Any]
    natural_key: str | None
    candidates: list[Candidate]
    active_suppression: Suppression | None
    existing_resolution: IdentityResolution | None
    reasons: list[str] = field(default_factory=list)


def _normalized_of(validation: ImportRowValidation) -> dict[str, Any]:
    return dict(validation.normalized_data or {})


def _natural_key_of(normalized: dict[str, Any]) -> str | None:
    first, last, domain = (
        normalized.get("first_name"),
        normalized.get("last_name"),
        normalized.get("company_domain"),
    )
    if first and last and domain:
        return norm.build_natural_key(str(first), str(last), str(domain))
    return None


def _memberships_of(
    session: Session, contact_id: uuid.UUID
) -> list[tuple[CampaignContact, Campaign]]:
    return [
        (membership, campaign)
        for membership, campaign in session.execute(
            select(CampaignContact, Campaign)
            .join(Campaign, Campaign.id == CampaignContact.campaign_id)
            .where(CampaignContact.contact_id == contact_id)
            .order_by(CampaignContact.created_at.desc())
        ).all()
    ]


def _active_suppression_for_contact(session: Session, contact: Contact) -> Suppression | None:
    return find_active_suppression(
        session,
        email=contact.email,
        domain=contact.company_domain,
    )


def _find_candidates(session: Session, validation: ImportRowValidation) -> list[Candidate]:
    """Existing (non-merged) contacts the ambiguous row could resolve to.

    Candidates are found by *exact* signals only — a shared normalized email or
    the exact natural key (first|last|domain). Similar names or a shared company
    never make a candidate: two people are never inferred identical from a name
    or company alone (AGENTS.md / DAT-004).
    """

    normalized = _normalized_of(validation)
    natural_key = _natural_key_of(normalized)
    email = normalized.get("email")

    found: dict[uuid.UUID, str] = {}
    if email:
        for contact in session.scalars(
            select(Contact).where(Contact.email == email, Contact.merged_into_id.is_(None))
        ).all():
            found[contact.id] = "exact normalized email match"
    if natural_key:
        for contact in session.scalars(
            select(Contact).where(
                Contact.natural_key == natural_key, Contact.merged_into_id.is_(None)
            )
        ).all():
            found.setdefault(contact.id, "exact natural key (first · last · company domain)")

    candidates: list[Candidate] = []
    for contact_id, reason in found.items():
        cand = session.get(Contact, contact_id)
        if cand is None:
            continue
        candidates.append(
            Candidate(
                contact=cand,
                match_reason=reason,
                memberships=_memberships_of(session, contact_id),
                provenance_count=session.scalar(
                    select(func.count(ProvenanceRecord.id)).where(
                        ProvenanceRecord.contact_id == contact_id
                    )
                )
                or 0,
                active_suppression=_active_suppression_for_contact(session, cand),
            )
        )
    candidates.sort(key=lambda c: c.contact.created_at)
    return candidates


def _existing_resolution_for_row(
    session: Session, import_row_id: uuid.UUID
) -> IdentityResolution | None:
    return session.scalars(
        select(IdentityResolution).where(IdentityResolution.import_row_id == import_row_id)
    ).first()


def get_row_review(session: Session, import_row_id: uuid.UUID) -> RowReview | None:
    """Load the full review detail for one ambiguous import row, or None."""

    found = session.execute(
        select(ImportRowValidation, ImportRow, ImportBatch, Campaign)
        .join(ImportRow, ImportRow.id == ImportRowValidation.import_row_id)
        .join(ImportBatch, ImportBatch.id == ImportRow.batch_id)
        .join(Campaign, Campaign.id == ImportBatch.campaign_id)
        .where(ImportRowValidation.import_row_id == import_row_id)
    ).first()
    if found is None:
        return None
    validation, row, batch, campaign = found
    if validation.outcome != ImportRowOutcome.AMBIGUOUS:
        return None

    normalized = _normalized_of(validation)
    natural_key = _natural_key_of(normalized)
    candidates = _find_candidates(session, validation)
    suppression = find_active_suppression(
        session,
        email=normalized.get("email"),
        domain=normalized.get("company_domain"),
    )

    reasons: list[str] = []
    if validation.note:
        reasons.append(validation.note)
    if natural_key and len(candidates) > 1:
        reasons.append(
            f"{len(candidates)} existing contacts share the natural key "
            f"“{natural_key}”, so the correct match cannot be chosen automatically."
        )

    return RowReview(
        validation=validation,
        row=row,
        batch=batch,
        campaign=campaign,
        normalized=normalized,
        natural_key=natural_key,
        candidates=candidates,
        active_suppression=suppression,
        existing_resolution=_existing_resolution_for_row(session, import_row_id),
        reasons=reasons,
    )


# --- Consequence preview -----------------------------------------------------


@dataclass
class ConsequencePreview:
    """A dry description of exactly what a resolution WILL do — no mutation."""

    action: IdentityResolutionType
    ok: bool
    summary: list[str] = field(default_factory=list)
    blocked_reason: str | None = None
    destructive: bool = False
    requires_confirmation: bool = False
    will_create_contact: bool = False
    target_contact_id: uuid.UUID | None = None
    merged_contact_id: uuid.UUID | None = None
    membership_added_campaign: str | None = None
    membership_collision: bool = False
    provenance_appended: bool = False
    suppression_enforced: bool = False
    email_inherited: bool = False
    memberships_transferred: int = 0
    memberships_skipped: int = 0
    provenance_transferred: int = 0
    observations_transferred: int = 0


def _load_contact(session: Session, contact_id: uuid.UUID | None, *, label: str) -> Contact:
    if contact_id is None:
        raise ResolutionError(f"{label} is required for this action.")
    contact = session.get(Contact, contact_id)
    if contact is None:
        raise ResolutionError(f"{label} {contact_id} does not exist.")
    return contact


def preview_row_resolution(
    session: Session,
    *,
    import_row_id: uuid.UUID,
    action: IdentityResolutionType,
    target_contact_id: uuid.UUID | None = None,
    merged_contact_id: uuid.UUID | None = None,
) -> ConsequencePreview:
    """Compute the consequence of resolving *import_row_id* without mutating."""

    review = get_row_review(session, import_row_id)
    if review is None:
        raise ResolutionError("That ambiguous row does not exist or was already resolved.")

    if review.existing_resolution is not None:
        return ConsequencePreview(
            action=action,
            ok=False,
            blocked_reason=(
                "This row was already resolved "
                f"({review.existing_resolution.resolution_type.value}); "
                "re-submitting will not change anything."
            ),
        )

    if action is IdentityResolutionType.MERGE:
        return _preview_merge(
            session,
            survivor_id=target_contact_id,
            loser_id=merged_contact_id,
            import_row_id=import_row_id,
        )
    if action in (IdentityResolutionType.ASSIGN_EXISTING,):
        return _preview_assign(session, review, target_contact_id)
    if action in (IdentityResolutionType.CREATE_NEW, IdentityResolutionType.MARK_SEPARATE):
        return _preview_create(session, review, action)
    raise ResolutionError(f"Unknown resolution action: {action!r}")


def _campaign_membership(
    session: Session, campaign_id: uuid.UUID, contact_id: uuid.UUID
) -> CampaignContact | None:
    return session.scalars(
        select(CampaignContact).where(
            CampaignContact.campaign_id == campaign_id,
            CampaignContact.contact_id == contact_id,
        )
    ).first()


def _preview_assign(
    session: Session, review: RowReview, target_contact_id: uuid.UUID | None
) -> ConsequencePreview:
    candidate_ids = {c.contact.id for c in review.candidates}
    if target_contact_id is None or target_contact_id not in candidate_ids:
        raise ResolutionError("Choose one of the listed candidate contacts to assign this row to.")
    contact = _load_contact(session, target_contact_id, label="Target contact")
    collision = _campaign_membership(session, review.campaign.id, contact.id) is not None
    suppressed = (
        review.active_suppression is not None
        or _active_suppression_for_contact(session, contact) is not None
    )

    preview = ConsequencePreview(
        action=IdentityResolutionType.ASSIGN_EXISTING,
        ok=True,
        target_contact_id=contact.id,
        will_create_contact=False,
        provenance_appended=True,
        membership_collision=collision,
        suppression_enforced=suppressed,
    )
    name = f"{contact.first_name} {contact.last_name}"
    preview.summary.append(f"Link this row to the existing contact {name}.")
    if collision:
        preview.summary.append(
            f"{name} is already a member of “{review.campaign.name}” — no duplicate "
            "membership will be created; the observation and provenance are still recorded."
        )
    else:
        preview.membership_added_campaign = review.campaign.name
        preview.summary.append(f"Add {name} to “{review.campaign.name}”.")
    preview.summary.append("Append this import row as a new provenance observation.")
    if suppressed:
        preview.summary.append(
            "This identity is on the suppression ledger — the membership will be "
            "SUPPRESSED and suppression propagated across the contact's campaigns."
        )
    return preview


def _preview_create(
    session: Session, review: RowReview, action: IdentityResolutionType
) -> ConsequencePreview:
    email = review.normalized.get("email")
    if email:
        clash = session.scalars(
            select(Contact).where(Contact.email == email, Contact.merged_into_id.is_(None))
        ).first()
        if clash is not None:
            raise ResolutionError(
                f"An existing contact already uses {email}; assign this row to it "
                "instead of creating a duplicate."
            )
    if not (
        review.normalized.get("first_name")
        and review.normalized.get("last_name")
        and review.normalized.get("company_domain")
    ):
        raise ResolutionError(
            "This row is missing required identity fields; it cannot become a contact. "
            "Fix the source data and re-import."
        )

    suppressed = review.active_suppression is not None
    preview = ConsequencePreview(
        action=action,
        ok=True,
        will_create_contact=True,
        provenance_appended=True,
        membership_added_campaign=review.campaign.name,
        suppression_enforced=suppressed,
    )
    verb = (
        "Create a new, distinct contact"
        if action is IdentityResolutionType.MARK_SEPARATE
        else "Create a new contact"
    )
    preview.summary.append(f"{verb} from this row's normalized values.")
    if action is IdentityResolutionType.MARK_SEPARATE and review.candidates:
        preview.summary.append(
            "Record that this person is intentionally separate from the "
            f"{len(review.candidates)} similar existing contact(s), so the ambiguity "
            "will not re-surface."
        )
    preview.summary.append(f"Add the new contact to “{review.campaign.name}”.")
    preview.summary.append("Append this import row as the contact's first provenance observation.")
    if suppressed:
        preview.summary.append(
            "This identity is on the suppression ledger — the new membership will be "
            "SUPPRESSED and cannot enter outreach."
        )
    return preview


def _preview_merge(
    session: Session,
    *,
    survivor_id: uuid.UUID | None,
    loser_id: uuid.UUID | None,
    import_row_id: uuid.UUID | None,
) -> ConsequencePreview:
    survivor = _load_contact(session, survivor_id, label="Survivor contact")
    loser = _load_contact(session, loser_id, label="Duplicate contact")
    if survivor.id == loser.id:
        raise ResolutionError("A contact cannot be merged into itself.")

    # Idempotent: already merged this exact pair.
    if loser.merged_into_id == survivor.id:
        return ConsequencePreview(
            action=IdentityResolutionType.MERGE,
            ok=False,
            blocked_reason="These contacts are already merged; nothing further to do.",
        )
    if loser.merged_into_id is not None:
        raise ResolutionError("That duplicate has already been merged into a different contact.")
    if survivor.merged_into_id is not None:
        raise ResolutionError(
            "The chosen survivor has itself been merged away; pick an active survivor."
        )

    # Safety: conflicting distinct emails are never merged — they stay reviewable.
    if survivor.email and loser.email and survivor.email != loser.email:
        raise ResolutionError(
            f"These contacts have conflicting emails ({survivor.email} vs {loser.email}); "
            "they cannot be merged automatically and remain separate for review."
        )

    loser_memberships = _memberships_of(session, loser.id)
    transferred = 0
    skipped = 0
    for membership, _campaign in loser_memberships:
        if _campaign_membership(session, membership.campaign_id, survivor.id) is not None:
            skipped += 1
        else:
            transferred += 1
    provenance_count = (
        session.scalar(
            select(func.count(ProvenanceRecord.id)).where(ProvenanceRecord.contact_id == loser.id)
        )
        or 0
    )
    observation_count = (
        session.scalar(
            select(func.count(ImportRowValidation.id)).where(
                ImportRowValidation.contact_id == loser.id
            )
        )
        or 0
    )
    email_inherited = bool(loser.email) and not survivor.email
    suppressed = (
        _active_suppression_for_contact(session, survivor) is not None
        or _active_suppression_for_contact(session, loser) is not None
    )

    preview = ConsequencePreview(
        action=IdentityResolutionType.MERGE,
        ok=True,
        destructive=True,
        requires_confirmation=True,
        target_contact_id=survivor.id,
        merged_contact_id=loser.id,
        memberships_transferred=transferred,
        memberships_skipped=skipped,
        provenance_transferred=provenance_count,
        observations_transferred=observation_count,
        email_inherited=email_inherited,
        suppression_enforced=suppressed,
    )
    s_name = f"{survivor.first_name} {survivor.last_name}"
    l_name = f"{loser.first_name} {loser.last_name}"
    preview.summary.append(f"Keep {s_name} as the surviving contact.")
    preview.summary.append(
        f"Tombstone {l_name} (it is preserved, not deleted, and points to the survivor)."
    )
    if transferred:
        preview.summary.append(
            f"Move {transferred} campaign membership(s) from {l_name} to {s_name}."
        )
    if skipped:
        preview.summary.append(
            f"Skip {skipped} membership(s) where {s_name} is already a member "
            "(no duplicate active membership is created)."
        )
    if provenance_count:
        preview.summary.append(f"Re-home {provenance_count} provenance record(s) to {s_name}.")
    if observation_count:
        preview.summary.append(f"Re-home {observation_count} import observation(s) to {s_name}.")
    if email_inherited:
        preview.summary.append(
            f"The survivor has no email; it inherits {loser.email} from the duplicate."
        )
    if suppressed:
        preview.summary.append(
            "One of these identities is suppressed — the survivor's memberships will be "
            "SUPPRESSED after the merge."
        )
    return preview


# --- Apply side --------------------------------------------------------------


@dataclass
class ResolutionResult:
    """The applied resolution plus the consequence that was carried out."""

    resolution: IdentityResolution
    preview: ConsequencePreview
    reused: bool = False


def _snapshot_contact(contact: Contact) -> dict[str, Any]:
    return {
        "contact_id": str(contact.id),
        "email": contact.email,
        "natural_key": contact.natural_key,
        "merged_into_id": str(contact.merged_into_id) if contact.merged_into_id else None,
    }


def _propagate_suppression(
    session: Session, *, contact_id: uuid.UUID, actor: str, reason: str
) -> None:
    """Force every non-terminal membership of a contact to SUPPRESSED."""

    for membership in session.scalars(
        select(CampaignContact).where(CampaignContact.contact_id == contact_id)
    ).all():
        if membership.state not in _TERMINAL_STATES:
            transition_contact_state(
                session,
                membership,
                target=ContactWorkflowState.SUPPRESSED,
                actor=actor,
                reason=reason,
            )


def _ensure_membership(
    session: Session,
    *,
    campaign_id: uuid.UUID,
    contact_id: uuid.UUID,
    batch_id: uuid.UUID,
    suppressed: bool,
) -> tuple[CampaignContact, bool]:
    """Return the (membership, created?) for a (campaign, contact), never duplicating."""

    existing = _campaign_membership(session, campaign_id, contact_id)
    if existing is not None:
        return existing, False
    membership = CampaignContact(
        campaign_id=campaign_id,
        contact_id=contact_id,
        source_batch_id=batch_id,
        state=ContactWorkflowState.SUPPRESSED if suppressed else ContactWorkflowState.IMPORTED,
    )
    session.add(membership)
    session.flush()
    return membership, True


def _append_provenance(
    session: Session, *, contact_id: uuid.UUID, batch: ImportBatch, row: ImportRow
) -> None:
    session.add(
        ProvenanceRecord(
            contact_id=contact_id,
            import_batch_id=batch.id,
            import_row_id=row.id,
            source_name=batch.source_name,
            source_reference=batch.source_reference,
            exported_by=batch.exported_by,
            exported_at=batch.exported_at,
        )
    )
    session.flush()


def _create_contact_from_normalized(session: Session, normalized: dict[str, Any]) -> Contact:
    natural_key = _natural_key_of(normalized)
    if natural_key is None:
        raise ResolutionError("Cannot create a contact: required identity fields are missing.")
    values = {field: normalized.get(field) for field in _CONTACT_FIELDS}
    contact = Contact(natural_key=natural_key, **values)
    session.add(contact)
    session.flush()
    return contact


def resolve_row(
    session: Session,
    *,
    import_row_id: uuid.UUID,
    action: IdentityResolutionType,
    idempotency_key: str,
    actor: str,
    reason: str | None = None,
    target_contact_id: uuid.UUID | None = None,
    merged_contact_id: uuid.UUID | None = None,
    _fault: Any = None,
) -> ResolutionResult:
    """Apply one operator resolution to an ambiguous import row (transactional).

    Idempotent on three keys: an already-resolved row, a re-used idempotency key,
    or (for merges) an already-merged pair all return the existing decision and
    mutate nothing. On any failure the whole transaction is rolled back so no
    partial contact, membership, or provenance is ever committed. ``_fault`` is a
    test-only hook fired after the mutations but before commit, to prove rollback.
    """

    if not idempotency_key.strip():
        raise ResolutionError("An idempotency key is required.")

    # Idempotency by key first — a retried POST with the same key is a no-op.
    existing_by_key = session.scalars(
        select(IdentityResolution).where(IdentityResolution.idempotency_key == idempotency_key)
    ).first()
    if existing_by_key is not None:
        return ResolutionResult(
            resolution=existing_by_key,
            preview=ConsequencePreview(action=existing_by_key.resolution_type, ok=True),
            reused=True,
        )

    # Idempotency by row — the row was already resolved by an earlier decision.
    already = _existing_resolution_for_row(session, import_row_id)
    if already is not None:
        return ResolutionResult(
            resolution=already,
            preview=ConsequencePreview(action=already.resolution_type, ok=True),
            reused=True,
        )

    review = get_row_review(session, import_row_id)
    if review is None:
        raise ResolutionError("That ambiguous row does not exist or was already resolved.")

    preview = preview_row_resolution(
        session,
        import_row_id=import_row_id,
        action=action,
        target_contact_id=target_contact_id,
        merged_contact_id=merged_contact_id,
    )
    if not preview.ok:
        raise ResolutionError(preview.blocked_reason or "This resolution cannot be applied.")

    try:
        resolution = _apply_row_resolution(
            session,
            review=review,
            action=action,
            preview=preview,
            actor=actor,
            reason=reason,
            idempotency_key=idempotency_key,
            target_contact_id=target_contact_id,
            merged_contact_id=merged_contact_id,
        )
        if _fault is not None:
            _fault()
        session.commit()
        return ResolutionResult(resolution=resolution, preview=preview)
    except Exception:
        session.rollback()
        raise


def _apply_row_resolution(
    session: Session,
    *,
    review: RowReview,
    action: IdentityResolutionType,
    preview: ConsequencePreview,
    actor: str,
    reason: str | None,
    idempotency_key: str,
    target_contact_id: uuid.UUID | None,
    merged_contact_id: uuid.UUID | None,
) -> IdentityResolution:
    if action is IdentityResolutionType.MERGE:
        return _apply_merge(
            session,
            survivor_id=target_contact_id,
            loser_id=merged_contact_id,
            actor=actor,
            reason=reason,
            idempotency_key=idempotency_key,
            import_row_id=review.row.id,
        )

    suppressed = review.active_suppression is not None
    normalized = review.normalized

    if action is IdentityResolutionType.ASSIGN_EXISTING:
        assert target_contact_id is not None
        contact = session.get(Contact, target_contact_id)
        assert contact is not None
        previous_state: dict[str, Any] = {"assigned_to": _snapshot_contact(contact)}
        suppressed = suppressed or _active_suppression_for_contact(session, contact) is not None
        _ensure_membership(
            session,
            campaign_id=review.campaign.id,
            contact_id=contact.id,
            batch_id=review.batch.id,
            suppressed=suppressed,
        )
    else:
        # CREATE_NEW / MARK_SEPARATE both create a fresh, distinct contact.
        previous_state = {"created_from_row": str(review.row.id)}
        contact = _create_contact_from_normalized(session, normalized)
        _ensure_membership(
            session,
            campaign_id=review.campaign.id,
            contact_id=contact.id,
            batch_id=review.batch.id,
            suppressed=suppressed,
        )

    _append_provenance(session, contact_id=contact.id, batch=review.batch, row=review.row)

    if suppressed:
        _propagate_suppression(
            session,
            contact_id=contact.id,
            actor=actor,
            reason="suppressed identity confirmed during ambiguity resolution",
        )

    resulting_state: dict[str, Any] = {
        "contact_id": str(contact.id),
        "campaign_id": str(review.campaign.id),
        "membership_collision": preview.membership_collision,
        "suppression_enforced": suppressed,
    }
    if action is IdentityResolutionType.MARK_SEPARATE:
        resulting_state["distinguished_from"] = [str(c.contact.id) for c in review.candidates]

    resolution = IdentityResolution(
        resolution_type=action,
        import_row_id=review.row.id,
        target_contact_id=contact.id,
        actor=actor,
        reason=reason,
        idempotency_key=idempotency_key,
        previous_state=previous_state,
        resulting_state=resulting_state,
    )
    session.add(resolution)
    session.flush()

    record_audit_event(
        session,
        actor=actor,
        action=f"identity.{action.value}",
        entity_type="contact",
        entity_id=str(contact.id),
        previous_state=ImportRowOutcome.AMBIGUOUS.value,
        new_state=action.value,
        reason=reason or f"ambiguous row resolved by {action.value}",
        context={
            "import_row_id": str(review.row.id),
            "batch_id": str(review.batch.id),
            "campaign_id": str(review.campaign.id),
            "suppression_enforced": suppressed,
        },
    )
    return resolution


def _apply_merge(
    session: Session,
    *,
    survivor_id: uuid.UUID | None,
    loser_id: uuid.UUID | None,
    actor: str,
    reason: str | None,
    idempotency_key: str,
    import_row_id: uuid.UUID | None,
) -> IdentityResolution:
    survivor = _load_contact(session, survivor_id, label="Survivor contact")
    loser = _load_contact(session, loser_id, label="Duplicate contact")

    previous_state = {
        "survivor": _snapshot_contact(survivor),
        "loser": _snapshot_contact(loser),
    }

    # Deterministic transfer: memberships, provenance, observations, then email.
    transferred = 0
    skipped = 0
    for membership in session.scalars(
        select(CampaignContact).where(CampaignContact.contact_id == loser.id)
    ).all():
        survivor_membership = _campaign_membership(session, membership.campaign_id, survivor.id)
        if survivor_membership is not None:
            # Survivor already a member — never create a duplicate. Preserve a
            # suppressed state from the loser so suppression is not lost.
            if (
                membership.state in _TERMINAL_STATES
                and survivor_membership.state not in _TERMINAL_STATES
            ):
                transition_contact_state(
                    session,
                    survivor_membership,
                    target=ContactWorkflowState.SUPPRESSED,
                    actor=actor,
                    reason="suppression preserved from merged duplicate",
                )
            skipped += 1
        else:
            membership.contact_id = survivor.id
            session.flush()
            transferred += 1

    provenance_moved = 0
    for record in session.scalars(
        select(ProvenanceRecord).where(ProvenanceRecord.contact_id == loser.id)
    ).all():
        record.contact_id = survivor.id
        provenance_moved += 1
    observations_moved = 0
    for validation in session.scalars(
        select(ImportRowValidation).where(ImportRowValidation.contact_id == loser.id)
    ).all():
        validation.contact_id = survivor.id
        observations_moved += 1
    session.flush()

    email_inherited = False
    if loser.email and not survivor.email:
        # Move the email to the survivor. The partial unique index forbids two
        # non-null equal emails even transiently, so the loser's address must be
        # cleared and flushed BEFORE it is assigned to the survivor.
        moved_email = loser.email
        loser.email = None
        session.flush()
        survivor.email = moved_email
        session.flush()
        email_inherited = True

    # Tombstone the loser: preserved and pointed at the survivor, never deleted.
    loser.merged_into_id = survivor.id
    session.flush()

    suppressed = _active_suppression_for_contact(session, survivor) is not None
    if suppressed:
        _propagate_suppression(
            session,
            contact_id=survivor.id,
            actor=actor,
            reason="suppression enforced on merge survivor",
        )

    resulting_state = {
        "survivor_id": str(survivor.id),
        "loser_id": str(loser.id),
        "memberships_transferred": transferred,
        "memberships_skipped": skipped,
        "provenance_transferred": provenance_moved,
        "observations_transferred": observations_moved,
        "email_inherited": email_inherited,
        "suppression_enforced": suppressed,
    }

    resolution = IdentityResolution(
        resolution_type=IdentityResolutionType.MERGE,
        import_row_id=import_row_id,
        target_contact_id=survivor.id,
        merged_contact_id=loser.id,
        actor=actor,
        reason=reason,
        idempotency_key=idempotency_key,
        previous_state=previous_state,
        resulting_state=resulting_state,
    )
    session.add(resolution)
    session.flush()

    record_audit_event(
        session,
        actor=actor,
        action="identity.merge",
        entity_type="contact",
        entity_id=str(survivor.id),
        previous_state=str(loser.id),
        new_state=str(survivor.id),
        reason=reason or "confirmed duplicate contacts merged",
        context=resulting_state,
    )
    return resolution


def merge_contacts(
    session: Session,
    *,
    survivor_id: uuid.UUID,
    loser_id: uuid.UUID,
    idempotency_key: str,
    actor: str,
    reason: str | None = None,
    import_row_id: uuid.UUID | None = None,
    _fault: Any = None,
) -> ResolutionResult:
    """Merge two confirmed duplicate contacts (transactional, idempotent, audited)."""

    if not idempotency_key.strip():
        raise ResolutionError("An idempotency key is required.")

    existing_by_key = session.scalars(
        select(IdentityResolution).where(IdentityResolution.idempotency_key == idempotency_key)
    ).first()
    if existing_by_key is not None:
        return ResolutionResult(
            resolution=existing_by_key,
            preview=ConsequencePreview(action=IdentityResolutionType.MERGE, ok=True),
            reused=True,
        )

    loser = session.get(Contact, loser_id) if loser_id is not None else None
    if loser is not None and loser.merged_into_id == survivor_id:
        prior = session.scalars(
            select(IdentityResolution).where(
                IdentityResolution.resolution_type == IdentityResolutionType.MERGE,
                IdentityResolution.merged_contact_id == loser_id,
                IdentityResolution.target_contact_id == survivor_id,
            )
        ).first()
        if prior is not None:
            return ResolutionResult(
                resolution=prior,
                preview=ConsequencePreview(action=IdentityResolutionType.MERGE, ok=True),
                reused=True,
            )

    preview = _preview_merge(
        session, survivor_id=survivor_id, loser_id=loser_id, import_row_id=import_row_id
    )
    if not preview.ok:
        raise ResolutionError(preview.blocked_reason or "This merge cannot be applied.")

    try:
        resolution = _apply_merge(
            session,
            survivor_id=survivor_id,
            loser_id=loser_id,
            actor=actor,
            reason=reason,
            idempotency_key=idempotency_key,
            import_row_id=import_row_id,
        )
        if _fault is not None:
            _fault()
        session.commit()
        return ResolutionResult(resolution=resolution, preview=preview)
    except Exception:
        session.rollback()
        raise
