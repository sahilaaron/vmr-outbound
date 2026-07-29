"""A deterministic Phase 2 execution scenario, built through the real services.

The Workbench is a projection, so its tests are only worth anything if the state
they render was produced the way production produces it. Nothing here inserts a
row by hand or invents a status: Campaigns are created through
``services.campaigns``, Contacts are enrolled through
``services.campaign_contacts``, and every job and pipeline event is whatever the
Phase 2 orchestrator decided.

Where a test needs a state the orchestrator will not reach on its own — a lease
held by a worker, an exhausted retry, a terminal failure — the scenario reaches
it through the Phase 2 job service that owns that transition
(``jobs.claim_job``, ``jobs.schedule_retry``, ``jobs.mark_failed``), never by
assigning to a column.

The result is deterministic because the inputs are: fixed names, fixed order,
fixed suppression. Timestamps are the only thing that moves, and no assertion
depends on their absolute value.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.models.campaign import Campaign, CampaignContact
from app.models.contact import Contact
from app.models.enums import (
    AgentControlStatus,
    AgentIdentifier,
    AgentJobStatus,
    CampaignStatus,
    SuppressionReason,
    SuppressionType,
)
from app.models.suppression import Suppression
from app.models.verification_job import AgentJob
from app.services.agents import controls as agent_controls
from app.services.agents import jobs as agent_jobs
from app.services.campaign_contacts import enrol_contact, refresh_eligibility
from app.services.campaigns import create_campaign, set_campaign_execution
from sqlalchemy import select
from sqlalchemy.orm import Session

#: Every identity is synthetic and uses the repository's fixture convention, so
#: nothing a test writes can be mistaken for a captured person.
DOMAIN = "example.com"


@dataclass
class Scenario:
    """Handles a test needs, resolved once so assertions stay readable."""

    campaign: Campaign
    other_campaign: Campaign
    memberships: dict[str, CampaignContact] = field(default_factory=dict)
    contacts: dict[str, Contact] = field(default_factory=dict)

    def membership(self, key: str) -> CampaignContact:
        return self.memberships[key]

    def job_for(self, session: Session, key: str) -> AgentJob | None:
        return session.scalars(
            select(AgentJob)
            .where(AgentJob.campaign_contact_id == self.memberships[key].id)
            .order_by(AgentJob.created_at.desc())
        ).first()


def _contact(
    session: Session, *, first: str, last: str, company: str, email: str | None
) -> Contact:
    contact = Contact(
        first_name=first,
        last_name=last,
        company_name=company,
        company_domain=f"{company.lower().replace(' ', '')}.{DOMAIN}",
        email=email,
        natural_key=f"{first.lower()}|{last.lower()}|{company.lower()}",
    )
    session.add(contact)
    session.flush()
    return contact


def build(session: Session) -> Scenario:
    """One Campaign mid-flight, one not started, and the awkward states.

    Deliberately not tidy: a suppressed Contact, a Contact whose company domain
    never resolved, a leased job, a retry-scheduled job, a terminal failure, and
    a paused Agent. Those are the states an operator has to be able to read, so
    they are the states the fixture produces.
    """

    campaign = create_campaign(
        session,
        name="Pilot 100 — operations leaders",
        description="Deterministic execution fixture.",
        status=CampaignStatus.DRAFT,
    )
    other = create_campaign(
        session,
        name="Q3 expansion — RevOps",
        description="Second Campaign, used to prove overrides do not leak.",
        status=CampaignStatus.DRAFT,
    )
    session.flush()
    set_campaign_execution(session, campaign.id, enabled=True, actor="operator")
    session.flush()

    scenario = Scenario(campaign=campaign, other_campaign=other)

    people = [
        ("healthy", "Alice", "Nakamura", "Northwind", "alice.nakamura@northwind.example.com"),
        ("leased", "Bruno", "Castellanos", "Harbourline", "b.castellanos@harbourline.example.com"),
        ("retrying", "Chidi", "Okafor", "Meridian", "c.okafor@meridian.example.com"),
        ("terminal", "Dana", "Whitfield", "Colville", "dana.whitfield@colville.example.com"),
        ("suppressed", "Gerald", "Pinto", "Ashcroft", "gerald.pinto@ashcroft.example.com"),
        ("nodomain", "Helena", "Brandt", "Vantage", None),
    ]

    # Suppression is established before enrolment so the eligibility gate sees it
    # the way it would in production.
    session.add(
        Suppression(
            suppression_type=SuppressionType.EMAIL,
            value="gerald.pinto@ashcroft.example.com",
            reason=SuppressionReason.OPT_OUT,
            is_active=True,
            source="fixture",
        )
    )
    session.flush()

    for key, first, last, company, email in people:
        contact = _contact(session, first=first, last=last, company=company, email=email)
        scenario.contacts[key] = contact
        if key == "nodomain":
            contact.company_domain = None
            session.flush()
        result = enrol_contact(
            session,
            campaign_id=campaign.id,
            contact_id=contact.id,
            source_type="fixture",
            source_reference=key,
            actor="operator",
        )
        scenario.memberships[key] = result.membership
    session.flush()

    # One Contact in the second Campaign, so cross-Campaign isolation is testable.
    other_contact = _contact(session, first="Quentin", last="Marsh", company="Ardenway", email=None)
    scenario.contacts["other"] = other_contact
    scenario.memberships["other"] = enrol_contact(
        session,
        campaign_id=other.id,
        contact_id=other_contact.id,
        source_type="fixture",
        source_reference="other",
        actor="operator",
    ).membership
    session.flush()

    _lease(session, scenario, "leased")
    _schedule_retry(session, scenario, "retrying")
    _terminal_failure(session, scenario, "terminal")

    for membership in scenario.memberships.values():
        refresh_eligibility(session, membership=membership, actor="fixture")
    session.flush()
    return scenario


WORKER = "fixture-worker"


def _lease(session: Session, scenario: Scenario, key: str) -> AgentJob | None:
    """Hold a job under a worker lease, through the Phase 2 claim path."""

    job = scenario.job_for(session, key)
    if job is None:
        return None
    agent_jobs.claim_job(session, job_id=job.id, worker_id=WORKER, lease_seconds=120)
    session.flush()
    return job


def _schedule_retry(session: Session, scenario: Scenario, key: str) -> None:
    job = _lease(session, scenario, key)
    if job is None:
        return
    agent_jobs.start_job(session, job, worker_id=WORKER)
    agent_jobs.schedule_retry(
        session,
        job,
        error_class="provider_transient",
        reason="the provider timed out",
        base_seconds=30.0,
        cap_seconds=900.0,
    )
    session.flush()


def _terminal_failure(session: Session, scenario: Scenario, key: str) -> None:
    job = _lease(session, scenario, key)
    if job is None:
        return
    agent_jobs.start_job(session, job, worker_id=WORKER)
    agent_jobs.mark_failed(
        session,
        job,
        error_class="terminal_domain_error",
        reason="the record cannot be resolved from the evidence on file",
    )
    session.flush()


def pause_job(session: Session, job: AgentJob, *, reason_code: str, reason: str) -> AgentJob:
    """Hold a job the way an ``AgentBlocked`` adapter result does.

    Used to reach the Campaign Contact retry acceptance path: a job paused for a
    domain reason (not by an Agent control or a suppression) is exactly what
    ``campaign_contacts.retry_processing`` is willing to requeue.
    """

    return agent_jobs.mark_paused(session, job, reason=reason, reason_code=reason_code)


def pause_agent(session: Session, agent_id: AgentIdentifier, *, reason: str = "fixture") -> None:
    agent_controls.set_global_control(
        session, agent_id=agent_id, status=AgentControlStatus.PAUSED, reason=reason
    )
    session.flush()


def make_retryable_failure(session: Session, job: AgentJob) -> AgentJob:
    """A ``FAILED`` job whose recorded failure is retryable.

    No Phase 2 adapter currently produces this shape — ``schedule_retry`` marks a
    failure non-retryable once attempts are exhausted, and ``mark_failed`` always
    does — so it is written here directly against the documented job contract
    rather than through a service that cannot reach it. It exists so the
    Workbench's retry path is verified against the contract Phase 2 publishes,
    not only against the states today's adapters happen to produce. See the
    integration note in docs/AGENT_WORKBENCH.md.
    """

    job.status = AgentJobStatus.FAILED
    job.finished_at = datetime.now(UTC)
    job.error_class = "provider_transient"
    job.last_error = "the provider was unavailable"
    job.error = {
        "class": "provider_transient",
        "message": "the provider was unavailable",
        "retryable": True,
        "detail": {},
    }
    job.lease_owner = None
    job.lease_expires_at = None
    session.flush()
    return job


def expire_lease(session: Session, job: AgentJob) -> None:
    """Make a held lease look abandoned, so recovery behaviour is observable."""

    job.lease_expires_at = datetime.now(UTC) - timedelta(minutes=5)
    session.flush()


def campaign_ids(scenario: Scenario) -> tuple[uuid.UUID, uuid.UUID]:
    return scenario.campaign.id, scenario.other_campaign.id
