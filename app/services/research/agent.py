"""The Research Agent's domain state machine.

Runs the enabled research workers for one Contact's Company and stores
what they found: the raw payload verbatim, one versioned dossier, and one
INS-001 insight per sourced fact.

Everything about *how* work is claimed, retried, leased or failed belongs
to the shared Agent framework. This module only decides what the outcome
is, and returns a step describing it. The adapter translates.

Three outcomes are all legitimate:

* ``COMPLETE`` -- facts were found and stored;
* ``COMPLETE`` with warnings and ``sufficient=False`` -- the site was read
  and says little. That is a fact about the company, not an error, so the
  pipeline advances rather than stalling on a thin website;
* ``BLOCKED`` / ``TERMINAL`` -- research could not honestly run at all.

Nothing here writes a canonical Company field. Turning sourced facts into
canonical values is a separate, reviewable decision.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.models.company import Company
from app.models.contact import Contact
from app.models.enums import DossierSection, InsightKind, InsightState
from app.models.verification_job import AgentJob
from app.services.companies import dossiers
from app.services.insights.evidence import EvidenceInput, InsightError, create_insight
from app.services.research.contracts import (
    ResearchRequest,
    ResearchWorker,
    ResearchWorkerError,
    SourcedFact,
    WorkerResult,
)
from app.services.resolution import gates
from sqlalchemy.orm import Session

RESEARCH_ACTOR = "system:research-agent"
INTERPRETER = "research-agent"
INTERPRETER_VERSION = "1"

#: How a vendored fact field maps onto the closed dossier section set.
#: A field with no mapping is still stored as an insight; it just does not
#: claim a section it was not designed for.
_FIELD_SECTIONS: dict[str, DossierSection] = {
    "company_name": DossierSection.OVERVIEW,
    "legal_name": DossierSection.OVERVIEW,
    "alternate_name": DossierSection.OVERVIEW,
    "short_description": DossierSection.OVERVIEW,
    "founded_year": DossierSection.OVERVIEW,
    "company_type": DossierSection.OVERVIEW,
    "logo_url": DossierSection.OVERVIEW,
    "products": DossierSection.PRODUCTS_SERVICES,
    "services": DossierSection.PRODUCTS_SERVICES,
    "solutions": DossierSection.PRODUCTS_SERVICES,
    "case_studies": DossierSection.PRODUCTS_SERVICES,
    "certifications": DossierSection.PRODUCTS_SERVICES,
    "industries_served": DossierSection.INDUSTRIES,
    "applications": DossierSection.INDUSTRIES,
    "customer_references": DossierSection.INDUSTRIES,
    "headquarters": DossierSection.GEOGRAPHY,
    "office_locations": DossierSection.GEOGRAPHY,
    "contact_addresses": DossierSection.GEOGRAPHY,
    "leadership": DossierSection.LEADERSHIP,
    "recent_news": DossierSection.ACTIVITY_SIGNALS,
    "product_launches": DossierSection.ACTIVITY_SIGNALS,
    "partnerships": DossierSection.ACTIVITY_SIGNALS,
    "acquisitions": DossierSection.ACTIVITY_SIGNALS,
    "funding": DossierSection.ACTIVITY_SIGNALS,
    "expansion": DossierSection.ACTIVITY_SIGNALS,
    "hiring_themes": DossierSection.ACTIVITY_SIGNALS,
    "careers_page": DossierSection.ACTIVITY_SIGNALS,
    "emails": DossierSection.PUBLIC_CONTACTS,
    "phones": DossierSection.PUBLIC_CONTACTS,
    "social_profiles": DossierSection.PUBLIC_CONTACTS,
    "contact_page_urls": DossierSection.PUBLIC_CONTACTS,
}


class ResearchStepKind(enum.StrEnum):
    COMPLETE = "complete"
    BLOCKED = "blocked"
    RETRY = "retry"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class ResearchStep:
    """One execution's verdict, in the vocabulary the adapter translates."""

    kind: ResearchStepKind
    outcome: str
    result: dict[str, Any] = field(default_factory=dict)
    output_reference: dict[str, Any] = field(default_factory=dict)
    reason_code: str | None = None
    reason: str | None = None
    #: True once anything was written that must survive an error unwind.
    committed: bool = False


def _blocked(code: str, reason: str, *, committed: bool = False) -> ResearchStep:
    return ResearchStep(
        kind=ResearchStepKind.BLOCKED,
        outcome="research_blocked",
        result={"reason_code": code, "reason": reason},
        reason_code=code,
        reason=reason,
        committed=committed,
    )


def _claim_text(fact: SourcedFact) -> str:
    """The human-readable claim stored on the insight.

    Deliberately mechanical: ``field: value``. The Research Agent reports
    what a page said; it does not paraphrase, and paraphrasing here would
    be exactly the inference this stage is not allowed to make.
    """

    label = fact.field.replace("_", " ")
    return f"{label}: {fact.value}"


def _evidence(fact: SourcedFact) -> EvidenceInput:
    return EvidenceInput(
        source_url=fact.source_url,
        retrieved_at=fact.retrieved_at,
        evidence_summary=(fact.excerpt or fact.value)[:1000],
        confidence=fact.confidence,
        extraction_method=fact.extraction_method,
        excerpt=fact.excerpt,
        published_at=fact.published_at,
        freshness_at=fact.retrieved_at,
    )


def _sections(results: list[WorkerResult]) -> dict[str, Any]:
    """Group facts into the closed dossier section set, with provenance."""

    grouped: dict[str, list[dict[str, Any]]] = {}
    unknown: list[str] = []
    sources: list[dict[str, Any]] = []

    for result in results:
        for fact in result.facts:
            section = _FIELD_SECTIONS.get(fact.field)
            if section is None:
                unknown.append(fact.field)
                continue
            grouped.setdefault(section.value, []).append(
                {
                    "field": fact.field,
                    "value": fact.value,
                    "source_url": fact.source_url,
                    "confidence": fact.confidence,
                    "extraction_method": fact.extraction_method,
                    "retrieved_at": fact.retrieved_at.isoformat(),
                    "worker": result.worker,
                }
            )
        for page in result.raw.get("pages", []):
            sources.append({"worker": result.worker, **page})

    payload: dict[str, Any] = dict(grouped)
    payload[DossierSection.SOURCES.value] = sources
    # Present-but-empty says "looked and found nothing"; absent says "did
    # not address it". Only claim the former for what was actually sought.
    payload[DossierSection.UNKNOWNS.value] = sorted(set(unknown))
    return payload


def _store_facts(
    session: Session,
    *,
    company: Company,
    job: AgentJob,
    results: list[WorkerResult],
) -> tuple[int, list[str]]:
    """Write one insight per sourced fact. Returns ``(stored, warnings)``.

    The idempotency key is derived from the job, so a retried execution
    reuses its earlier rows rather than duplicating them.
    """

    stored = 0
    warnings: list[str] = []
    for result in results:
        for index, fact in enumerate(result.facts):
            key = f"research:{job.id}:{result.worker}:{index}"
            try:
                create_insight(
                    session,
                    claim=_claim_text(fact),
                    kind=InsightKind.FACT,
                    state=InsightState.SUPPORTED,
                    evidence=[_evidence(fact)],
                    company_id=company.id,
                    idempotency_key=key,
                    actor=RESEARCH_ACTOR,
                )
                stored += 1
            except InsightError as exc:
                # One malformed fact must not discard the rest of the run.
                warnings.append(f"fact {result.worker}[{index}] rejected: {exc}")
    return stored, warnings


def execute_step(
    session: Session,
    *,
    job: AgentJob,
    contact: Contact,
    workers: tuple[ResearchWorker, ...],
    options: dict[str, Any] | None = None,
    now: datetime | None = None,
    actor: str = RESEARCH_ACTOR,
) -> ResearchStep:
    """Research one Contact's Company and persist what was found."""

    if not workers:
        return _blocked(
            "no_workers_enabled",
            "no research worker is enabled for this Agent; "
            "set config['workers'] to at least one registered worker",
        )

    if contact.company_id is None:
        return _blocked(
            "company_unavailable",
            "this contact has no resolved company, so there is nothing to research",
        )

    company = session.get(Company, contact.company_id)
    if company is None:
        return _blocked("company_unavailable", "the contact's company record is missing")

    domain = (company.domain or "").strip()
    if not domain:
        return _blocked(
            "domain_unavailable",
            f"company {company.name!r} has no resolved domain; "
            "research reads the company's own website and cannot start without one",
        )

    # Company research is the one stage a provisional domain opens. Ask the
    # gate rather than re-deriving it here.
    decision = gates.authorize_contact(
        session, contact=contact, stage=gates.DownstreamStage.COMPANY_RESEARCH
    )
    if decision.blocked:
        return _blocked(
            "domain_not_authorized",
            decision.reason or "company research is not authorized for this contact",
        )

    request = ResearchRequest(
        domain=domain,
        company_name=company.name,
        options=dict(options or {}),
    )

    results: list[WorkerResult] = []
    warnings: list[str] = []
    for worker in workers:
        try:
            results.append(worker.run(request))
        except ResearchWorkerError as exc:
            if exc.retryable:
                return ResearchStep(
                    kind=ResearchStepKind.RETRY,
                    outcome="research_retry",
                    result={"reason_code": exc.code, "reason": str(exc)},
                    reason_code=exc.code,
                    reason=str(exc),
                )
            # A dead end for one worker is a warning while another may still
            # succeed; it only ends the run if nothing else produced anything.
            warnings.append(f"{worker.name}: {exc}")

    if not results:
        return ResearchStep(
            kind=ResearchStepKind.TERMINAL,
            outcome="research_failed",
            result={"reason_code": "all_workers_failed", "warnings": warnings},
            reason_code="all_workers_failed",
            reason="; ".join(warnings) or "every research worker failed",
        )

    for result in results:
        warnings.extend(result.warnings)

    # --- Persist. Everything below this line must survive an error unwind. ---
    raw_payload: dict[str, Any] = {
        "domain": domain,
        "company_id": str(company.id),
        "workers": [
            {
                "worker": result.worker,
                "worker_version": result.worker_version,
                "sufficient": result.sufficient,
                "fact_count": len(result.facts),
                "raw": result.raw,
            }
            for result in results
        ],
    }
    submission, created = dossiers.submit(
        session,
        company=company,
        producer=INTERPRETER,
        payload=raw_payload,
        producer_version=INTERPRETER_VERSION,
        submitted_by=actor,
        request_context={"agent_job_id": str(job.id)},
    )

    stored, store_warnings = _store_facts(session, company=company, job=job, results=results)
    warnings.extend(store_warnings)

    sufficient = any(result.sufficient for result in results)
    if not sufficient:
        warnings.append(
            "insufficient evidence: the sources were read but did not support "
            "enough facts to describe this company"
        )

    dossiers.interpret(
        session,
        company=company,
        submission=submission,
        interpreter=INTERPRETER,
        interpreter_version=INTERPRETER_VERSION,
        sections=_sections(results),
        warnings=warnings,
        created_by=actor,
        make_current=True,
    )

    output = {
        "domain": domain,
        "company_id": str(company.id),
        "submission_id": str(submission.id),
        "submission_created": created,
        "workers": [result.worker for result in results],
        "facts_stored": stored,
        "sufficient": sufficient,
        "warning_count": len(warnings),
    }
    return ResearchStep(
        kind=ResearchStepKind.COMPLETE,
        outcome="research_completed" if sufficient else "research_completed_with_warnings",
        result={"domain_outcome": "researched the company website", **output},
        output_reference=output,
        committed=True,
    )
