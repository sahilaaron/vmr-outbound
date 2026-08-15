"""Accepting one batch of spreadsheet rows into the product.

This is the whole write path, and it is deliberately short. It creates no
schema, no queue, no job type and no second copy of any rule: a row becomes a
permanent Contact and a Campaign membership through the same services the
capture path uses, and from that moment the existing durable Agent pipeline owns
it. Nothing here runs Research, discovers an address, verifies one or writes a
message — those are Agents, they are governed by Campaign switches and Agent
controls, and a spreadsheet must not be a way around any of them.

What one submit request actually does per row, in order:

1. **Look for the same row already submitted.** The idempotency key is derived
   from the install, spreadsheet, tab, Campaign, row and generation, so a second
   click presents the same key. A hit returns the existing membership and stops —
   before a Contact is considered, before a provider is asked, before anything is
   spent. This is the guard that makes a retried Apps Script execution free.
2. **Resolve the company name to a permanent Company.** Confirmed evidence only;
   see ``companies.py``.
3. **Resolve the person.** An exact LinkedIn profile URL may match an existing
   permanent Contact; otherwise the deterministic natural key may. Two candidates
   is an ambiguity the operator resolves, never a merge this code performs.
4. **Ask the suppression ledger, before creating anything.** A suppressed
   identity leaves no Contact and no membership behind.
5. **Enrol.** ``campaign_contacts.enrol_contact`` is the single entry point, and
   it is what initialises the pipeline and enqueues the first job.

Why the request stays short even though the work is long
--------------------------------------------------------

Steps 1–5 are bounded, deterministic database work plus at most one brand-matcher
lookup per distinct new company name. Everything expensive — research,
verification, model generation — happens afterwards in the durable worker, which
is why the add-on submits and leaves rather than holding a connection open. The
operator can close the spreadsheet; the pipeline does not care.

Why the pipeline target is whatever the rest of the product uses
----------------------------------------------------------------

A membership carries a ``desired_stage``, and ``enrol_contact`` refuses to
re-aim one that already exists — correctly, because two surfaces silently
disagreeing about how far a Contact should be taken is exactly the hidden state
this system refuses to keep. So this surface does not choose: it adopts the
stage an existing membership already declares, and for a new one it takes the
same default every other enrolment path takes. A sheet is a way *in*, not a
second opinion about what the pipeline is for.

That default names the Sending Agent, which is registered for pipeline
compatibility, has no production adapter and is disabled. Nothing sends as a
result, and nothing here could make it: the guarantee comes from an Agent that
cannot run rather than from a stage number this module picked, which is the
version of that guarantee that survives somebody changing this file.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.campaign import Campaign, CampaignContact
from app.models.contact import Contact
from app.models.pipeline import CampaignContactSource
from app.services import campaign_access, campaign_contacts
from app.services.imports import dedup
from app.services.imports import normalization as norm
from app.services.integrations.sheets import companies as sheet_companies
from app.services.integrations.sheets.contract import (
    RowContractError,
    RowStatus,
    SheetLocation,
    SubmittedRow,
    row_idempotency_key,
)
from app.services.profiles import refresh as refresh_service
from app.services.suppressions import evaluate_suppression

#: The provenance ``source_type`` every sheet-sourced membership carries. Not
#: ``"api"`` and not ``"manual"``: an operator reading a Contact a year from now
#: needs to see which surface put it there, and both of those words already mean
#: something else in this system.
SOURCE_TYPE = "google_sheets"


@dataclass(frozen=True)
class RowSubmission:
    """What one submitted row became."""

    client_row_id: str
    status: RowStatus
    submission_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    already_submitted: bool = False
    safe_failure_reason: str | None = None
    failure_code: str | None = None


@dataclass
class BatchSubmission:
    """The whole request's answer, in the order the rows arrived."""

    batch_id: str
    campaign_id: uuid.UUID
    rows: list[RowSubmission] = field(default_factory=list)
    provider_calls_made: int = 0

    @property
    def accepted(self) -> int:
        return sum(1 for row in self.rows if row.submission_id is not None)

    @property
    def refused(self) -> int:
        return sum(1 for row in self.rows if row.status is RowStatus.COULD_NOT_PREPARE)


def submit_rows(
    session: Session,
    *,
    campaign: Campaign,
    location: SheetLocation,
    rows: list[SubmittedRow],
    generation: int,
    batch_reference: str,
    actor: str,
) -> BatchSubmission:
    """Accept a validated batch. One row's refusal never affects another's."""

    result = BatchSubmission(batch_id=batch_reference, campaign_id=campaign.id)
    cache = sheet_companies.new_cache()

    for row in rows:
        key = row_idempotency_key(
            location,
            campaign_id=str(campaign.id),
            client_row_id=row.client_row_id,
            generation=generation,
        )
        existing = _existing_membership(session, idempotency_key=key)
        if existing is not None:
            result.rows.append(
                RowSubmission(
                    client_row_id=row.client_row_id,
                    status=RowStatus.PENDING,
                    submission_id=existing.id,
                    contact_id=existing.contact_id,
                    already_submitted=True,
                )
            )
            continue

        savepoint = session.begin_nested()
        try:
            submission = _submit_one(
                session,
                campaign=campaign,
                location=location,
                row=row,
                generation=generation,
                idempotency_key=key,
                actor=actor,
                cache=cache,
                counters=result,
            )
        except RowContractError as exc:
            savepoint.rollback()
            submission = RowSubmission(
                client_row_id=row.client_row_id,
                status=RowStatus.COULD_NOT_PREPARE,
                safe_failure_reason=str(exc),
                failure_code=exc.code,
            )
        except campaign_contacts.CampaignContactError as exc:
            # ``CampaignContactError`` is documented as operator-facing text, so it
            # is shown as written rather than replaced with a generic sentence
            # that would hide the one thing the operator can act on.
            savepoint.rollback()
            submission = RowSubmission(
                client_row_id=row.client_row_id,
                status=RowStatus.COULD_NOT_PREPARE,
                safe_failure_reason=str(exc),
                failure_code="enrolment_refused",
            )
        else:
            savepoint.commit()
        result.rows.append(submission)

    return result


def _submit_one(
    session: Session,
    *,
    campaign: Campaign,
    location: SheetLocation,
    row: SubmittedRow,
    generation: int,
    idempotency_key: str,
    actor: str,
    cache: sheet_companies.NameCache,
    counters: BatchSubmission,
) -> RowSubmission:
    # A cache read, not a resolution. Established evidence links the row
    # immediately and for free; nothing established is an ordinary answer that
    # leaves the link NULL and lets the pipeline take it from there. Either way
    # no provider is asked, so this step can neither refuse a row nor spend
    # money — see `companies.py` on why the previous behaviour was wrong.
    company_outcome = sheet_companies.link_established_company(
        session,
        company_name=row.company_name,
        actor=actor,
        cache=cache,
    )
    if company_outcome.provider_call_made:  # pragma: no cover - structurally impossible
        counters.provider_calls_made += 1
    company = company_outcome.company
    domain = company_outcome.domain

    contact = _resolve_contact(session, row=row, company_domain=domain)

    # Asked before anything is created, exactly as before. A row with no domain
    # yet is evaluated on the identity it does have; `evaluate_suppression`
    # already takes an optional domain, so nothing here is relaxed — a domain
    # suppression simply has no domain to match until the pipeline finds one,
    # and every later advancing path asks the ledger again.
    decision = evaluate_suppression(
        session, email=contact.email if contact is not None else None, domain=domain
    )
    if decision.blocked:
        raise RowContractError(
            f"this identity is suppressed ({decision.reason}); no contact was created "
            "and the suppression is untouched",
            code="suppressed",
        )

    if contact is None:
        contact = Contact(
            first_name=row.first_name,
            last_name=row.last_name,
            company_name=row.company_name,
            # NULL when nothing has established this company. The model documents
            # that as "not linked yet" and deliberately prefers it to a guess.
            company_domain=domain,
            company_id=company.id if company is not None else None,
            title=row.job_title,
            linkedin_url=row.linkedin_url,
            # The natural key is a name-plus-*domain* fingerprint, so it only
            # exists once a domain does. Left NULL rather than built from a
            # placeholder, which would make two different people collide.
            natural_key=(
                norm.build_natural_key(row.first_name, row.last_name, domain) if domain else None
            ),
        )
        session.add(contact)
        session.flush()
    else:
        _fill_blanks(
            contact,
            row=row,
            company_id=company.id if company is not None else None,
            domain=domain,
        )

    existing = _membership_for(session, campaign_id=campaign.id, contact_id=contact.id)
    # Adopted, not chosen. See the module docstring: a sheet joins a membership on
    # the terms it already has, and a new one takes the same default every other
    # enrolment path takes.
    desired_stage = (
        existing.desired_stage
        if existing is not None and existing.desired_stage is not None
        else campaign_contacts.DEFAULT_DESIRED_STAGE
    )
    enrolment = campaign_contacts.enrol_contact(
        session,
        campaign_id=campaign.id,
        contact_id=contact.id,
        source_type=SOURCE_TYPE,
        source_reference=row.client_row_id,
        source_context=_provenance(location, row=row, generation=generation),
        idempotency_key=idempotency_key,
        actor=actor,
        desired_stage=desired_stage,
    )
    return RowSubmission(
        client_row_id=row.client_row_id,
        status=RowStatus.PENDING,
        submission_id=enrolment.membership.id,
        contact_id=contact.id,
        already_submitted=not enrolment.created,
    )


def _resolve_contact(
    session: Session, *, row: SubmittedRow, company_domain: str | None
) -> Contact | None:
    """The existing permanent Contact this row names, or ``None`` for a new one.

    Two matchers, in the order the rest of the system uses them. An exact
    normalized LinkedIn profile URL is the strongest signal and is tried first;
    the deterministic name-plus-domain natural key is the fallback. Either
    returning more than one candidate is an ambiguity, and this surface refuses
    the row rather than choosing — merging the wrong two people is not something
    a retry can undo.

    With no established domain only the first matcher runs, and that is the
    honest answer rather than a degraded one: the natural key *is*
    name-plus-domain, so without a domain there is no deterministic key to
    compare. Matching on name-and-company-string instead would be a new,
    fuzzier identity rule invented for this surface alone, which is exactly the
    kind of divergence this repair removes. The cost is recorded in the handoff:
    a domainless row resubmitted under a new ``generation`` can create a second
    Contact, where a row with a domain would have matched.

    No identity link is written for a spreadsheet-supplied URL. An identity link
    is a record of a handle *observed on a page*; a URL typed into a cell is an
    operator's assertion, and recording it as an observation would let a typo
    become permanent matching authority.
    """

    normalized_url = norm.normalize_linkedin_profile_url(row.linkedin_url)
    if normalized_url:
        matches = refresh_service.find_exact_matches(session, normalized_url)
        if len(matches) > 1:
            raise RowContractError(
                "more than one existing contact carries this exact LinkedIn URL; "
                "resolve the duplicate in VMR before submitting this row again",
                code="contact_identity_ambiguous",
            )
        if len(matches) == 1:
            return matches[0]

    if not company_domain:
        return None

    natural_key = norm.build_natural_key(row.first_name, row.last_name, company_domain)
    deduped = dedup.find_existing_contact(session, email=None, natural_key=natural_key)
    if deduped.ambiguous:
        raise RowContractError(
            "several existing contacts share this name and company; resolve them in "
            "VMR before submitting this row again",
            code="contact_identity_ambiguous",
        )
    return deduped.contact


def _fill_blanks(
    contact: Contact, *, row: SubmittedRow, company_id: uuid.UUID | None, domain: str | None
) -> None:
    """Add what the permanent record is missing, and overwrite nothing.

    A spreadsheet is a weaker source than a capture and a much weaker one than an
    operator's own correction, so it may fill a blank and may never replace a
    value. That asymmetry is the whole rule: it makes re-submitting a row safe,
    and it makes a stale sheet incapable of undoing work done in the product.
    """

    if not contact.title and row.job_title:
        contact.title = row.job_title
    if not contact.linkedin_url and row.linkedin_url:
        contact.linkedin_url = row.linkedin_url
    if not contact.company_name:
        contact.company_name = row.company_name
    if not contact.company_domain:
        contact.company_domain = domain
    if contact.company_id is None:
        contact.company_id = company_id
    if not contact.natural_key and contact.company_domain:
        contact.natural_key = norm.build_natural_key(
            row.first_name, row.last_name, contact.company_domain
        )


def _provenance(
    location: SheetLocation, *, row: SubmittedRow, generation: int
) -> dict[str, object]:
    """Everything a later reader needs to answer "where did this row come from".

    The operator's free-text context is stored here, labelled as what it is. It
    is **not** evidence: it never becomes a sourced fact, it carries no URL and no
    retrieval time, and personalization reads it as operator-supplied prospect
    context rather than as something the system established. Keeping the label on
    the value is what stops a sentence typed into a spreadsheet from being cited
    later as research.
    """

    payload: dict[str, object] = {
        "surface": "google_sheets_addon",
        "installation_id": location.installation_id,
        "spreadsheet_id": location.spreadsheet_id,
        "sheet_id": location.sheet_id,
        "client_row_id": row.client_row_id,
        "generation": generation,
        "submitted_company_name": row.company_name,
    }
    if row.job_title:
        payload["submitted_job_title"] = row.job_title
    if row.linkedin_url:
        payload["submitted_linkedin_url"] = row.linkedin_url
    if row.context:
        payload["operator_supplied_context"] = {
            "text": row.context,
            "kind": "operator_supplied",
            "verified": False,
        }
    return payload


def _membership_for(
    session: Session, *, campaign_id: uuid.UUID, contact_id: uuid.UUID
) -> CampaignContact | None:
    return session.scalars(
        select(CampaignContact).where(
            CampaignContact.campaign_id == campaign_id,
            CampaignContact.contact_id == contact_id,
        )
    ).first()


def _existing_membership(session: Session, *, idempotency_key: str) -> CampaignContact | None:
    source = session.scalars(
        select(CampaignContactSource).where(
            CampaignContactSource.idempotency_key == idempotency_key
        )
    ).first()
    if source is None:
        return None
    return session.get(CampaignContact, source.campaign_contact_id)


def require_campaign(
    session: Session, *, campaign_id: uuid.UUID, actor: campaign_access.CampaignActor
) -> Campaign:
    """The Campaign this account may act on, or a refusal it cannot see past.

    ``require_campaign_access`` raises ``CampaignAccessError`` for a Campaign the
    account may not reach, and this raises the same error for one that does not
    exist. Same answer for both, deliberately: "no such Campaign" and "not yours"
    must not be distinguishable from outside, or the endpoint becomes a way to
    enumerate other people's Campaigns.
    """

    campaign_access.require_campaign_access(session, campaign_id, actor)
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise campaign_access.CampaignAccessError("campaign_access_denied")
    return campaign


__all__ = [
    "SOURCE_TYPE",
    "BatchSubmission",
    "RowSubmission",
    "require_campaign",
    "submit_rows",
]
