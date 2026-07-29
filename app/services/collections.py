"""Canonical Collection service over the proven capture Label registry."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.campaign import Campaign
from app.models.collection import CampaignCollection, Collection, CollectionMembership
from app.models.contact import Contact
from app.services.audit import record_audit_event
from app.services.captures import labels as legacy_labels

MAX_DESCRIPTION_LEN = 2_000


class CollectionError(Exception):
    """Safe operator-facing Collection validation error."""


class CollectionNotFound(CollectionError):
    pass


@dataclass(frozen=True)
class CollectionSummary:
    collection: Collection
    contact_count: int
    pending_capture_count: int
    campaign_ids: tuple[uuid.UUID, ...]


def create_collection(
    session: Session,
    *,
    name: str,
    description: str | None = None,
    actor: str = "operator",
) -> tuple[Collection, bool]:
    """Create or return a Collection by deterministic slug."""

    normalized = legacy_labels.normalize_requested_labels([name])
    if not normalized:
        raise CollectionError("collection name is required and must contain usable characters")
    cleaned = normalized[0]
    slug = legacy_labels.slugify_label(cleaned)
    assert slug is not None
    existing = session.scalars(select(Collection).where(Collection.slug == slug)).one_or_none()
    if existing is not None:
        return existing, False
    collection = Collection(
        slug=slug,
        name=cleaned,
        description=_description(description) if description is not None else None,
        created_by=actor,
    )
    try:
        with session.begin_nested():
            session.add(collection)
            session.flush()
    except IntegrityError:
        winner = session.scalars(select(Collection).where(Collection.slug == slug)).one_or_none()
        if winner is None:  # pragma: no cover - defensive
            raise
        return winner, False
    record_audit_event(
        session,
        actor=actor,
        action="collection.created",
        entity_type="collection",
        entity_id=str(collection.id),
        new_state=collection.slug,
        reason="collection created",
        context={"name": collection.name},
    )
    return collection, True


def _description(value: str) -> str | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > MAX_DESCRIPTION_LEN:
        raise CollectionError(
            f"collection description must be {MAX_DESCRIPTION_LEN} characters or fewer"
        )
    return cleaned


def rename_collection(
    session: Session,
    collection_id: uuid.UUID,
    *,
    name: str,
    description: str | None = None,
    actor: str = "operator",
) -> Collection:
    """Rename the global record; every membership and Campaign link follows."""

    collection = session.get(Collection, collection_id)
    if collection is None:
        raise CollectionNotFound(f"collection {collection_id} does not exist")
    normalized = legacy_labels.normalize_requested_labels([name])
    if not normalized:
        raise CollectionError("collection name is required and must contain usable characters")
    cleaned = normalized[0]
    slug = legacy_labels.slugify_label(cleaned)
    assert slug is not None
    conflict = session.scalars(
        select(Collection).where(Collection.slug == slug, Collection.id != collection_id)
    ).one_or_none()
    if conflict is not None:
        raise CollectionError(f"a collection named {cleaned!r} already exists")

    previous = {"name": collection.name, "slug": collection.slug}
    try:
        with session.begin_nested():
            collection.name = cleaned
            collection.slug = slug
            if description is not None:
                collection.description = _description(description)
            session.flush()
    except IntegrityError as exc:
        raise CollectionError(f"a collection named {cleaned!r} already exists") from exc
    record_audit_event(
        session,
        actor=actor,
        action="collection.renamed",
        entity_type="collection",
        entity_id=str(collection.id),
        previous_state=previous["slug"],
        new_state=collection.slug,
        reason="collection renamed",
        context={"previous_name": previous["name"], "name": collection.name},
    )
    return collection


def assign_contact(
    session: Session,
    *,
    collection_id: uuid.UUID,
    contact_id: uuid.UUID,
    actor: str = "operator",
    source: str = "operator",
    capture_id: uuid.UUID | None = None,
) -> tuple[CollectionMembership, bool]:
    """Idempotently add one permanent Contact to a Collection."""

    collection = session.get(Collection, collection_id)
    if collection is None:
        raise CollectionNotFound(f"collection {collection_id} does not exist")
    if session.get(Contact, contact_id) is None:
        raise CollectionError(f"contact {contact_id} does not exist")
    existing = session.scalars(
        select(CollectionMembership).where(
            CollectionMembership.contact_id == contact_id,
            CollectionMembership.collection_id == collection_id,
        )
    ).one_or_none()
    if existing is not None:
        return existing, False

    membership = CollectionMembership(
        contact_id=contact_id,
        collection_id=collection_id,
        capture_id=capture_id,
        source=source,
    )
    try:
        with session.begin_nested():
            session.add(membership)
            session.flush()
    except IntegrityError:
        winner = session.scalars(
            select(CollectionMembership).where(
                CollectionMembership.contact_id == contact_id,
                CollectionMembership.collection_id == collection_id,
            )
        ).one_or_none()
        if winner is None:  # pragma: no cover - defensive
            raise
        return winner, False

    record_audit_event(
        session,
        actor=actor,
        action="collection.contact_added",
        entity_type="collection_membership",
        entity_id=str(membership.id),
        reason="contact added to collection",
        context={"collection_id": str(collection_id), "contact_id": str(contact_id)},
    )
    return membership, True


def remove_contact(
    session: Session,
    *,
    collection_id: uuid.UUID,
    contact_id: uuid.UUID,
    actor: str = "operator",
) -> bool:
    """Remove only Collection membership; never remove the permanent Contact."""

    membership = session.scalars(
        select(CollectionMembership).where(
            CollectionMembership.contact_id == contact_id,
            CollectionMembership.collection_id == collection_id,
        )
    ).one_or_none()
    if membership is None:
        return False
    membership_id = membership.id
    session.delete(membership)
    session.flush()
    record_audit_event(
        session,
        actor=actor,
        action="collection.contact_removed",
        entity_type="collection_membership",
        entity_id=str(membership_id),
        reason="contact removed from collection",
        context={"collection_id": str(collection_id), "contact_id": str(contact_id)},
    )
    return True


def associate_campaign(
    session: Session,
    *,
    collection_id: uuid.UUID,
    campaign_id: uuid.UUID,
    role: str = "audience",
    actor: str = "operator",
) -> tuple[CampaignCollection, bool]:
    """Associate a global Collection with a Campaign without changing ownership."""

    if session.get(Collection, collection_id) is None:
        raise CollectionNotFound(f"collection {collection_id} does not exist")
    if session.get(Campaign, campaign_id) is None:
        raise CollectionError(f"campaign {campaign_id} does not exist")
    clean_role = role.strip().lower()
    if not clean_role or len(clean_role) > 32:
        raise CollectionError("collection Campaign role must be 1 to 32 characters")
    existing = session.scalars(
        select(CampaignCollection).where(
            CampaignCollection.collection_id == collection_id,
            CampaignCollection.campaign_id == campaign_id,
        )
    ).one_or_none()
    if existing is not None:
        return existing, False
    link = CampaignCollection(
        collection_id=collection_id,
        campaign_id=campaign_id,
        association_role=clean_role,
        created_by=actor,
    )
    try:
        with session.begin_nested():
            session.add(link)
            session.flush()
    except IntegrityError:
        winner = session.scalars(
            select(CampaignCollection).where(
                CampaignCollection.collection_id == collection_id,
                CampaignCollection.campaign_id == campaign_id,
            )
        ).one_or_none()
        if winner is None:  # pragma: no cover - defensive
            raise
        return winner, False
    record_audit_event(
        session,
        actor=actor,
        action="collection.campaign_associated",
        entity_type="campaign_collection",
        entity_id=str(link.id),
        reason="collection associated with campaign",
        context={"collection_id": str(collection_id), "campaign_id": str(campaign_id)},
    )
    return link, True


def dissociate_campaign(
    session: Session,
    *,
    collection_id: uuid.UUID,
    campaign_id: uuid.UUID,
    actor: str = "operator",
) -> bool:
    link = session.scalars(
        select(CampaignCollection).where(
            CampaignCollection.collection_id == collection_id,
            CampaignCollection.campaign_id == campaign_id,
        )
    ).one_or_none()
    if link is None:
        return False
    link_id = link.id
    session.delete(link)
    session.flush()
    record_audit_event(
        session,
        actor=actor,
        action="collection.campaign_dissociated",
        entity_type="campaign_collection",
        entity_id=str(link_id),
        reason="collection dissociated from campaign",
        context={"collection_id": str(collection_id), "campaign_id": str(campaign_id)},
    )
    return True


def list_collections(
    session: Session, *, campaign_id: uuid.UUID | None = None
) -> list[CollectionSummary]:
    """List global Collections, optionally limited to one Campaign association."""

    query = select(Collection).order_by(Collection.name.asc())
    if campaign_id is not None:
        query = query.join(
            CampaignCollection,
            CampaignCollection.collection_id == Collection.id,
        ).where(CampaignCollection.campaign_id == campaign_id)
    collections = list(session.scalars(query).all())
    if not collections:
        return []
    ids = [collection.id for collection in collections]
    contact_counts: dict[uuid.UUID, int] = {
        collection_id: count
        for collection_id, count in session.execute(
            select(CollectionMembership.collection_id, func.count(CollectionMembership.id))
            .where(
                CollectionMembership.collection_id.in_(ids),
                CollectionMembership.contact_id.is_not(None),
            )
            .group_by(CollectionMembership.collection_id)
        ).all()
    }
    pending_counts: dict[uuid.UUID, int] = {
        collection_id: count
        for collection_id, count in session.execute(
            select(CollectionMembership.collection_id, func.count(CollectionMembership.id))
            .where(
                CollectionMembership.collection_id.in_(ids),
                CollectionMembership.contact_id.is_(None),
            )
            .group_by(CollectionMembership.collection_id)
        ).all()
    }
    campaign_rows = session.execute(
        select(CampaignCollection.collection_id, CampaignCollection.campaign_id)
        .where(CampaignCollection.collection_id.in_(ids))
        .order_by(CampaignCollection.created_at)
    ).all()
    campaign_ids: dict[uuid.UUID, list[uuid.UUID]] = {}
    for collection_id, linked_campaign_id in campaign_rows:
        campaign_ids.setdefault(collection_id, []).append(linked_campaign_id)
    return [
        CollectionSummary(
            collection=collection,
            contact_count=contact_counts.get(collection.id, 0),
            pending_capture_count=pending_counts.get(collection.id, 0),
            campaign_ids=tuple(campaign_ids.get(collection.id, [])),
        )
        for collection in collections
    ]
