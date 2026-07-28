"""Resolving a LinkedIn identifier to a contact, and relating the two forms.

Every rule that decides whether one person's two identifiers may be joined lives
here, so that "when do we bridge?" has exactly one answer in the codebase.

The bridge rule, stated once:

    A Sales Navigator member id is associated with a vanity profile URL
    automatically ONLY when both were directly observed in the same
    authenticated capture for the same displayed person.

Nothing else qualifies. Not a matching name, not a matching company, not a
matching title or headline, not two separate captures that merely look
compatible, not a model's opinion, and not a ``/in/<member-id>`` alias generated
from the identifier itself. When the evidence is insufficient or contradictory
both identifiers are preserved and the record goes to the DAT-004 operator
review path, because an unresolved duplicate is safer than a false merge.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.contact import Contact
from app.models.enums import IdentityLinkDecision, IdentityLinkState, LinkedInIdentifierKind
from app.models.linkedin_identity_link import LinkedInIdentityLink
from app.services.imports.normalization import normalize_linkedin_profile_url


@dataclass(frozen=True)
class LinkOutcome:
    """What happened to one identifier claim."""

    state: IdentityLinkState
    link: LinkedInIdentityLink | None
    #: Set when another contact already holds this identifier. The claim is not
    #: applied; both contacts survive and an operator decides.
    conflicting_contact_id: uuid.UUID | None = None


@dataclass(frozen=True)
class BridgeOutcome:
    """What happened to a member-id/vanity-URL pair observed together."""

    bridged: bool
    member: LinkOutcome | None = None
    vanity: LinkOutcome | None = None
    reason: str | None = None


def normalize_identifier(kind: LinkedInIdentifierKind, value: str | None) -> str | None:
    """Put an identifier into the form it is stored and compared in.

    A vanity URL is normalized as it always was. A member id is returned
    VERBATIM apart from surrounding whitespace: it is case-sensitive, so the
    URL normalizer — which lowercases slugs — must never touch it.
    """

    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    if kind == LinkedInIdentifierKind.SALESNAV_MEMBER_ID:
        return raw
    return normalize_linkedin_profile_url(raw)


def _active_link(
    session: Session, kind: LinkedInIdentifierKind, value: str
) -> LinkedInIdentityLink | None:
    return session.scalars(
        select(LinkedInIdentityLink).where(
            LinkedInIdentityLink.identifier_kind == kind.value,
            LinkedInIdentityLink.identifier_value == value,
            LinkedInIdentityLink.state == IdentityLinkState.ACTIVE.value,
            LinkedInIdentityLink.suspected_alias.is_(False),
        )
    ).first()


def lookup_contact(
    session: Session, kind: LinkedInIdentifierKind, value: str | None
) -> Contact | None:
    """The contact an identifier currently speaks for, or None.

    An indexed read on ``(identifier_kind, identifier_value)``. Comparison is the
    database's, so a member id matches only its exact casing. Suspected aliases
    and superseded history are excluded: they are evidence, not identity.
    """

    normalized = normalize_identifier(kind, value)
    if normalized is None:
        return None
    link = _active_link(session, kind, normalized)
    if link is None:
        return None
    contact = session.get(Contact, link.contact_id)
    if contact is None or contact.merged_into_id is not None:
        return None
    return contact


def record_observed(
    session: Session,
    *,
    contact: Contact,
    kind: LinkedInIdentifierKind,
    value: str | None,
    decided_by: str,
    capture_id: uuid.UUID | None = None,
    source_surface: str | None = None,
    decision_kind: IdentityLinkDecision = IdentityLinkDecision.OBSERVED_CAPTURE,
    corroboration: dict[str, object] | None = None,
) -> LinkOutcome:
    """Record that an identifier was observed for this contact.

    Idempotent: observing the same identifier for the same contact again returns
    the existing row rather than adding another.

    If a DIFFERENT contact already holds the identifier the claim is refused. A
    ``NEEDS_REVIEW`` row is written so the conflict is visible and reviewable,
    the existing association is left alone, and nothing is merged.
    """

    normalized = normalize_identifier(kind, value)
    if normalized is None:
        return LinkOutcome(state=IdentityLinkState.NEEDS_REVIEW, link=None)

    existing = _active_link(session, kind, normalized)
    if existing is not None:
        if existing.contact_id == contact.id:
            return LinkOutcome(state=IdentityLinkState.ACTIVE, link=existing)
        conflict = LinkedInIdentityLink(
            contact_id=contact.id,
            identifier_kind=kind.value,
            identifier_value=normalized,
            state=IdentityLinkState.NEEDS_REVIEW.value,
            decision_kind=decision_kind.value,
            capture_id=capture_id,
            source_surface=source_surface,
            corroboration=corroboration,
            reason=(
                "another contact already holds this identifier; kept separate "
                "for operator review rather than merged"
            ),
            decided_by=decided_by,
        )
        session.add(conflict)
        return LinkOutcome(
            state=IdentityLinkState.NEEDS_REVIEW,
            link=conflict,
            conflicting_contact_id=existing.contact_id,
        )

    link = LinkedInIdentityLink(
        contact_id=contact.id,
        identifier_kind=kind.value,
        identifier_value=normalized,
        state=IdentityLinkState.ACTIVE.value,
        decision_kind=decision_kind.value,
        capture_id=capture_id,
        source_surface=source_surface,
        corroboration=corroboration,
        decided_by=decided_by,
    )
    session.add(link)
    return LinkOutcome(state=IdentityLinkState.ACTIVE, link=link)


def bridge_observed_pair(
    session: Session,
    *,
    contact: Contact,
    member_id: str | None,
    vanity_url: str | None,
    decided_by: str,
    capture_id: uuid.UUID | None = None,
    source_surface: str | None = None,
) -> BridgeOutcome:
    """Relate a member id and a vanity URL seen together on one captured person.

    This is the whole of the automatic bridge. Both values must come from the
    same capture of the same displayed person; the caller is responsible for
    that, and the co-occurrence is written into each row's ``corroboration`` so
    the justification survives the decision.
    """

    member = normalize_identifier(LinkedInIdentifierKind.SALESNAV_MEMBER_ID, member_id)
    vanity = normalize_identifier(LinkedInIdentifierKind.PUBLIC_VANITY_URL, vanity_url)
    if member is None or vanity is None:
        return BridgeOutcome(
            bridged=False,
            reason="both identifiers must be observed on the same capture to bridge",
        )

    evidence: dict[str, object] = {
        "observed_member_id": member,
        "observed_vanity_url": vanity,
        "source_surface": source_surface,
        "rule": "same_capture_co_occurrence",
    }
    member_outcome = record_observed(
        session,
        contact=contact,
        kind=LinkedInIdentifierKind.SALESNAV_MEMBER_ID,
        value=member,
        decided_by=decided_by,
        capture_id=capture_id,
        source_surface=source_surface,
        decision_kind=IdentityLinkDecision.SAME_CAPTURE_OBSERVED,
        corroboration=evidence,
    )
    vanity_outcome = record_observed(
        session,
        contact=contact,
        kind=LinkedInIdentifierKind.PUBLIC_VANITY_URL,
        value=vanity,
        decided_by=decided_by,
        capture_id=capture_id,
        source_surface=source_surface,
        decision_kind=IdentityLinkDecision.SAME_CAPTURE_OBSERVED,
        corroboration=evidence,
    )
    bridged = (
        member_outcome.state == IdentityLinkState.ACTIVE
        and vanity_outcome.state == IdentityLinkState.ACTIVE
    )
    return BridgeOutcome(
        bridged=bridged,
        member=member_outcome,
        vanity=vanity_outcome,
        reason=None if bridged else "one identifier is already held by another contact",
    )


def revoke(
    session: Session,
    *,
    kind: LinkedInIdentifierKind,
    value: str,
    reason: str,
    decided_by: str,
) -> LinkedInIdentityLink | None:
    """Undo an association without losing the record that it was made.

    The row is superseded, never deleted, which is both the audit trail and the
    reversal: the identifier is free to be claimed correctly afterwards.
    """

    normalized = normalize_identifier(kind, value)
    if normalized is None:
        return None
    link = _active_link(session, kind, normalized)
    if link is None:
        return None
    link.state = IdentityLinkState.SUPERSEDED.value
    link.superseded_at = datetime.now(UTC)
    link.reason = reason
    link.decided_by = decided_by
    return link


def propose_canonical_url(
    session: Session, *, contact: Contact, url: str | None, observed: bool
) -> bool:
    """Offer a value for ``contact.linkedin_url``; report whether it was taken.

    The invariant, encoded rather than left to whichever write happened last: a
    directly observed published handle is never displaced by anything that was
    not directly observed. A member-id alias resolves, but it is not the
    contact's canonical published URL, so it may fill an empty field and may
    never overwrite an observed one.
    """

    normalized = normalize_linkedin_profile_url(url) if url else None
    if normalized is None:
        return False
    if contact.linkedin_url is None:
        contact.linkedin_url = normalized
        return True
    if not observed:
        return False
    if normalize_linkedin_profile_url(contact.linkedin_url) == normalized:
        return False
    if _has_observed_vanity(session, contact):
        return False
    contact.linkedin_url = normalized
    return True


def _has_observed_vanity(session: Session, contact: Contact) -> bool:
    link = session.scalars(
        select(LinkedInIdentityLink).where(
            LinkedInIdentityLink.contact_id == contact.id,
            LinkedInIdentityLink.identifier_kind == LinkedInIdentifierKind.PUBLIC_VANITY_URL.value,
            LinkedInIdentityLink.state == IdentityLinkState.ACTIVE.value,
            LinkedInIdentityLink.suspected_alias.is_(False),
        )
    ).first()
    return link is not None


def looks_like_member_id_alias(stored_url: str | None, salesnav_lead_url: str | None) -> bool:
    """Whether a stored ``/in/`` value is really a member-id alias.

    Deterministic, and deliberately narrow: it says yes only when the row also
    carries a Sales Navigator lead URL AND the stored slug equals that lead URL's
    member id, compared case-insensitively because ingest lowercased the slug on
    the way in. No guessing from shape, length or alphabet — a real handle can
    look like anything, and a false positive here would flag a genuine identity.
    """

    if not stored_url or not salesnav_lead_url:
        return False
    normalized = normalize_linkedin_profile_url(stored_url)
    if not normalized:
        return False
    slug = normalized.rsplit("/", 1)[-1]
    tail = salesnav_lead_url.rstrip("/").rsplit("/sales/lead/", 1)
    if len(tail) != 2:
        return False
    member = tail[1].split(",")[0].split("?")[0].split("#")[0]
    if not member:
        return False
    return slug.casefold() == member.casefold()
