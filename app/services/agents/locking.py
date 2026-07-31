"""Authoritative row-locking helpers for Campaign Agent execution.

Every transaction that needs these mutable rows must acquire them in this order:

1. the ``campaigns`` execution gate, in shared mode and ordered by ``id``, when
   the transaction may create or recover a lease;
2. permanent ``contacts`` ordered by ``id`` when the transaction writes Contact
   domain state;
3. ``campaign_contacts`` ordered by ``id``;
4. ``verification_jobs`` (the shared Agent Job table) ordered by Campaign Contact,
   Agent, creation time, and ``id``.

Pipeline stage projections are written only after their owning Campaign Contact is
locked.  A queue-only transaction may lock an Agent Job without a Campaign Contact,
but it must not then reach "backwards" into Campaign Contact or pipeline state.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.models.campaign import Campaign, CampaignContact
from app.models.contact import Contact
from app.models.verification_job import AgentJob


@dataclass(frozen=True)
class LockedJobContext:
    """One requested job plus its locked Campaign Contact, when it has one."""

    job: AgentJob
    contacts: tuple[Contact, ...]
    membership: CampaignContact | None
    related_jobs: tuple[AgentJob, ...]


def lock_campaign_execution_gates(
    session: Session,
    campaign_ids: Iterable[uuid.UUID],
    *,
    skip_locked: bool = False,
) -> tuple[Campaign, ...]:
    """Acquire shared Campaign locks that serialize leasing with Pause.

    Workers may share this gate with each other.  Updating the Campaign master
    switch requires an incompatible row lock, so a Pause waits for claim
    transactions already in progress and excludes claims that start afterwards.
    The gate is deliberately absent from long-running prepare/completion work.
    """

    identifiers = tuple(set(campaign_ids))
    if not identifiers:
        return ()
    rows = session.scalars(
        select(Campaign)
        .where(Campaign.id.in_(identifiers))
        .order_by(Campaign.id)
        .with_for_update(read=True, skip_locked=skip_locked)
    ).all()
    return tuple(rows)


def lock_contacts(
    session: Session,
    contact_ids: Iterable[uuid.UUID],
    *,
    skip_locked: bool = False,
) -> tuple[Contact, ...]:
    """Lock permanent Contacts in deterministic database order."""

    identifiers = tuple(set(contact_ids))
    if not identifiers:
        return ()
    rows = session.scalars(
        select(Contact)
        .where(Contact.id.in_(identifiers))
        .order_by(Contact.id)
        .with_for_update(skip_locked=skip_locked)
    ).all()
    return tuple(rows)


def lock_campaign_contacts(
    session: Session,
    campaign_contact_ids: Iterable[uuid.UUID],
    *,
    skip_locked: bool = False,
) -> tuple[CampaignContact, ...]:
    """Lock existing Campaign Contacts in deterministic database order."""

    identifiers = tuple(set(campaign_contact_ids))
    if not identifiers:
        return ()
    rows = session.scalars(
        select(CampaignContact)
        .where(CampaignContact.id.in_(identifiers))
        .order_by(CampaignContact.id)
        .with_for_update(skip_locked=skip_locked)
    ).all()
    return tuple(rows)


def lock_campaign_contact(
    session: Session,
    campaign_contact_id: uuid.UUID,
    *,
    skip_locked: bool = False,
) -> CampaignContact | None:
    """Lock one Campaign Contact through the same ordered helper."""

    locked = lock_campaign_contacts(
        session,
        (campaign_contact_id,),
        skip_locked=skip_locked,
    )
    return locked[0] if locked else None


def ordered_job_statement(statement: Select[tuple[AgentJob]]) -> Select[tuple[AgentJob]]:
    """Apply the mandatory stable order to a statement selecting Agent Jobs."""

    return statement.order_by(
        AgentJob.campaign_contact_id.asc().nulls_last(),
        AgentJob.agent_id.asc(),
        AgentJob.created_at.asc(),
        AgentJob.id.asc(),
    )


def lock_agent_jobs(
    session: Session,
    statement: Select[tuple[AgentJob]],
    *,
    skip_locked: bool = False,
) -> tuple[AgentJob, ...]:
    """Lock Agent Jobs after their Campaign Contacts have already been locked."""

    rows = session.scalars(
        ordered_job_statement(statement).with_for_update(skip_locked=skip_locked)
    ).all()
    return tuple(rows)


def lock_job_context(
    session: Session,
    job_id: uuid.UUID,
    *,
    include_parent: bool = True,
    skip_locked: bool = False,
) -> LockedJobContext | None:
    """Lock the Campaign Contact, then the requested job and optional parent.

    The first read is deliberately non-locking: it discovers the immutable primary
    key relationship needed to acquire locks in the authoritative order.  Every
    mutable field is re-read from the locked rows before the caller acts.
    """

    reference = session.execute(
        select(
            AgentJob.campaign_contact_id,
            AgentJob.contact_id,
            AgentJob.parent_job_id,
        ).where(AgentJob.id == job_id)
    ).one_or_none()
    if reference is None:
        return None

    contact_ids: set[uuid.UUID] = set()
    if reference.contact_id is not None:
        contact_ids.add(reference.contact_id)
    if reference.campaign_contact_id is not None:
        membership_contact_id = session.scalar(
            select(CampaignContact.contact_id).where(
                CampaignContact.id == reference.campaign_contact_id
            )
        )
        if membership_contact_id is not None:
            contact_ids.add(membership_contact_id)
    contacts = lock_contacts(session, contact_ids, skip_locked=skip_locked)
    if len(contacts) != len(contact_ids):
        return None

    membership = None
    if reference.campaign_contact_id is not None:
        membership = lock_campaign_contact(
            session,
            reference.campaign_contact_id,
            skip_locked=skip_locked,
        )
        if membership is None:
            return None

    job_ids = {job_id}
    if include_parent and reference.parent_job_id is not None:
        job_ids.add(reference.parent_job_id)
    related = lock_agent_jobs(
        session,
        select(AgentJob).where(AgentJob.id.in_(job_ids)),
        skip_locked=skip_locked,
    )
    if len(related) != len(job_ids):
        return None
    by_id = {job.id: job for job in related}
    job = by_id.get(job_id)
    if job is None:
        return None
    return LockedJobContext(
        job=job,
        contacts=contacts,
        membership=membership,
        related_jobs=related,
    )
