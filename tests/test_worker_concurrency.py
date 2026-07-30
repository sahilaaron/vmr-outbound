"""Two consumers on one queue: no collisions, and no head-of-line blocking.

The queue was always built for this — ``claim_next_job`` selects ``FOR UPDATE SKIP
LOCKED`` under a committed lease — but it had only ever had one consumer, so the
property had never been exercised. These tests exercise it directly at the claim
boundary rather than through the threaded script, because the guarantee lives in the
database and a thread pool only benefits from it.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from app.models.campaign import Campaign
from app.models.company import Company
from app.models.contact import Contact
from app.models.enums import (
    AgentControlStatus,
    AgentIdentifier,
    AgentJobStatus,
    CampaignStatus,
)
from app.services import campaign_contacts
from app.services.agents import controls, jobs
from app.services.agents.orchestrator import claim_next_campaign_job
from sqlalchemy.orm import Session


def _campaign(db: Session) -> Campaign:
    campaign = Campaign(
        name=f"Concurrency {uuid.uuid4()}",
        status=CampaignStatus.ACTIVE,
        execution_enabled=True,
    )
    db.add(campaign)
    db.flush()
    return campaign


def _contact(db: Session, name: str, domain: str) -> Contact:
    company = Company(name=name, domain=domain)
    db.add(company)
    db.flush()
    contact = Contact(
        first_name=name,
        last_name="Tester",
        company_name=name,
        company_domain=domain,
        natural_key=f"{name.lower()}|tester|{domain}",
    )
    db.add(contact)
    db.flush()
    return contact


def test_two_workers_never_claim_the_same_job(committed_session: Session) -> None:
    """The property a thread pool depends on, at the level it is enforced.

    Claiming is ``FOR UPDATE SKIP LOCKED`` inside a committed transaction, so a
    second worker skips a row the first is holding instead of blocking on it or
    double-executing it.
    """

    campaign = _campaign(committed_session)
    contact = _contact(committed_session, "Alpha", "alpha.example")
    enrolled = campaign_contacts.enrol_contact(
        committed_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        enqueue=True,
        desired_stage=AgentIdentifier.IDENTITY,
    )
    committed_session.commit()
    assert enrolled.queued_job is not None

    first = claim_next_campaign_job(committed_session, worker_id="w#0")
    assert first is not None
    committed_session.commit()

    # The same worker id would be a different question; a *second* consumer must
    # find nothing, because the only job is leased.
    second = claim_next_campaign_job(committed_session, worker_id="w#1")
    assert second is None, "a leased job must not be claimable by another worker"

    assert first.status is AgentJobStatus.LEASED
    assert first.lease_owner == "w#0"


def test_a_held_job_does_not_block_the_next_one(committed_session: Session) -> None:
    """Head-of-line blocking, which is the whole reason for a pool.

    One worker holding a slow job must not stop a second worker taking the next
    ready job. With a single consumer this is exactly what could not happen: the
    queue drains in order, so a ninety-second Research call held up work that was
    ready the entire time.
    """

    campaign = _campaign(committed_session)
    first_contact = _contact(committed_session, "Alpha", "alpha.example")
    second_contact = _contact(committed_session, "Beta", "beta.example")
    for contact in (first_contact, second_contact):
        campaign_contacts.enrol_contact(
            committed_session,
            campaign_id=campaign.id,
            contact_id=contact.id,
            source_type="manual",
            enqueue=True,
            desired_stage=AgentIdentifier.IDENTITY,
        )
    committed_session.commit()

    held = claim_next_campaign_job(committed_session, worker_id="w#0")
    assert held is not None
    committed_session.commit()

    # Worker 1 arrives while worker 0 still holds its lease.
    other = claim_next_campaign_job(committed_session, worker_id="w#1")
    assert other is not None, "a second worker must find the next ready job"
    assert other.id != held.id
    assert other.lease_owner == "w#1"
    committed_session.commit()


def test_an_agent_scoped_pool_ignores_work_it_was_not_asked_for(
    committed_session: Session,
) -> None:
    """What makes a small language-model pool possible alongside a larger one.

    Scoping by Agent is how an operator bounds concurrent ``claude`` invocations
    without throttling the rest of the pipeline.
    """

    campaign = _campaign(committed_session)
    contact = _contact(committed_session, "Alpha", "alpha.example")
    campaign_contacts.enrol_contact(
        committed_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        enqueue=True,
        desired_stage=AgentIdentifier.IDENTITY,
    )
    committed_session.commit()

    # The only queued job is IDENTITY.
    claimed = claim_next_campaign_job(
        committed_session,
        worker_id="research-pool#0",
        agent_ids=(AgentIdentifier.RESEARCH,),
    )
    assert claimed is None

    claimed = claim_next_campaign_job(
        committed_session,
        worker_id="identity-pool#0",
        agent_ids=(AgentIdentifier.IDENTITY,),
    )
    assert claimed is not None
    committed_session.commit()


def test_an_expired_lease_returns_the_job_to_the_queue(
    committed_session: Session,
) -> None:
    """A worker thread that dies must not strand its job.

    This is what makes a pool safe to Ctrl+C: an abandoned lease is recovered rather
    than leaving a Contact stuck mid-stage forever.
    """

    campaign = _campaign(committed_session)
    contact = _contact(committed_session, "Alpha", "alpha.example")
    campaign_contacts.enrol_contact(
        committed_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        enqueue=True,
        desired_stage=AgentIdentifier.IDENTITY,
    )
    committed_session.commit()

    claimed = claim_next_campaign_job(committed_session, worker_id="dying#0", lease_seconds=0.001)
    assert claimed is not None
    committed_session.commit()

    # A lease that has already elapsed, without waiting for wall-clock time.
    expiry = claimed.lease_expires_at
    assert expiry is not None
    recovered = jobs.recover_expired_leases(committed_session, now=expiry + timedelta(seconds=1))
    assert claimed.id in {job.id for job in recovered}
    committed_session.commit()

    # Returned to the queue, unowned, and marked with why — which is what lets
    # another thread pick it up on its next pass.
    assert claimed.status is AgentJobStatus.PENDING
    assert claimed.lease_owner is None
    assert claimed.error_class == "lease_expired"


def test_a_disabled_agent_is_still_not_claimed_by_any_worker(
    committed_session: Session,
) -> None:
    """Concurrency must not become a way around a control.

    A pool claims more work in parallel; it does not claim work it was refused.
    """

    controls.set_global_control(
        committed_session,
        agent_id=AgentIdentifier.IDENTITY,
        status=AgentControlStatus.DISABLED,
        reason="held for this test",
    )
    campaign = _campaign(committed_session)
    contact = _contact(committed_session, "Alpha", "alpha.example")
    enrolled = campaign_contacts.enrol_contact(
        committed_session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type="manual",
        enqueue=True,
        desired_stage=AgentIdentifier.IDENTITY,
    )
    committed_session.commit()

    # Identity is not skippable, so a disabled Identity stops the Contact rather
    # than being stepped over — and no worker gets a job for it.
    assert enrolled.queued_job is None
    assert claim_next_campaign_job(committed_session, worker_id="w#0") is None
    committed_session.commit()
