"""The Research Agent's domain state machine.

Runs the enabled research workers for one Contact's Company and stores
what they found: the raw payload verbatim, one versioned dossier, and one
INS-001 insight per sourced fact.

Everything about *how* work is claimed, retried, leased or failed belongs
to the shared Agent framework. This module only decides what the outcome
is, and returns a step describing it. The adapter translates.

Three outcomes are all legitimate:

* ``COMPLETE`` -- facts were found and stored;
* ``COMPLETE`` with warnings and ``sufficient=False`` -- the sources were
  read and say little. That is a fact about the company, not an error, so
  the pipeline advances rather than stalling on a thin website;
* ``BLOCKED`` / ``TERMINAL`` -- research could not honestly run at all.

**Two attempts, one stage.** The deterministic website worker is always the
first attempt, and when it produces something usable the run ends there. When
it does not -- the site is unreachable, JavaScript-only, redirected off-host,
unparseable, or simply says almost nothing -- the bounded Claude CLI fallback in
``app.services.research.fallback`` runs as a second attempt within this same
execution. It is not another Agent, another stage or another job: it is a second
source, filed under its own worker name, subject to the same fact validation and
the same evidence model.

The trigger is deliberately coarse. This module never asks *why* the
deterministic attempt was unusable before deciding whether to fall back; it asks
only whether the result is usable. Classifying the failure first would mean every
new way a website can defeat a crawler needs a code change before the fallback
covers it, and the operator would carry the classification.

Nothing here writes a canonical Company field -- from either source. Turning
sourced facts into canonical values is a separate, reviewable decision.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.company_dossier import CompanyDossierVersion, CompanyResearchSubmission
from app.models.contact import Contact
from app.models.enums import DossierSection, InsightKind, InsightState
from app.models.verification_job import AgentJob
from app.services.companies import dossiers
from app.services.company_intelligence import handoff as intelligence_handoff
from app.services.insights.evidence import EvidenceInput, InsightError, create_insight
from app.services.research import fallback as research_fallback
from app.services.research.contracts import (
    ResearchRequest,
    ResearchWorker,
    ResearchWorkerError,
    SourcedFact,
    WorkerResult,
)
from app.services.research.fallback import (
    FallbackRecord,
    FallbackStatus,
    FallbackSubject,
    ResearchFallback,
)
from app.services.resolution import gates

RESEARCH_ACTOR = "system:research-agent"
INTERPRETER = "research-agent"
INTERPRETER_VERSION = "1"

#: What the committed dossier was actually built from. Written into the durable
#: job result so the question "where did this company description come from?" is
#: answered by a stored value rather than inferred from which rows happen to
#: exist.
BASIS_NONE = "no_sourced_evidence"
BASIS_DETERMINISTIC = "deterministic_website"
BASIS_FALLBACK = "claude_cli_fallback"
BASIS_BOTH = "deterministic_website_and_claude_cli_fallback"

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
        source_title=fact.source_title,
        retrieved_at=fact.retrieved_at,
        evidence_summary=(fact.excerpt or fact.value)[:1000],
        confidence=fact.confidence,
        # Carried through unchanged, and it is what keeps the two sources
        # distinguishable at the level of one stored evidence row: a
        # deterministic extraction and a Claude-assisted read never share a
        # value here.
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


def _summary(
    *,
    results: list[WorkerResult],
    facts_stored: int,
    sources: int,
    sufficient: bool,
) -> str:
    """One factual line about the run, for the operator-facing view.

    Counts only — deliberately not prose. This stage reports what pages said and
    does not paraphrase, and a generated sentence here would be the one piece of
    unsourced language in an otherwise fully sourced record. "Read 4 pages, stored 3
    facts" is checkable against the dossier; "Kiln Systems builds controllers" would
    not be.
    """

    workers = ", ".join(result.worker for result in results) or "no worker"
    verdict = "enough to describe the company" if sufficient else "not enough to describe it"
    return f"{workers}: {sources} page(s) read, {facts_stored} sourced fact(s) stored — {verdict}."


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


def _assess(
    results: list[WorkerResult], *, failures: list[dict[str, Any]]
) -> tuple[bool, str | None, str | None]:
    """Is what the deterministic attempt produced usable? If not, why not?

    Three unusable shapes, and the fallback answers all of them identically.
    The distinction below exists for the operator's report, not for the routing
    decision — which is the point. A stage that had to recognise every way a
    website can defeat a crawler before it was allowed to try something else
    would be wrong about a new one every time.
    """

    if not results:
        codes = ", ".join(sorted({str(item["reason_code"]) for item in failures}))
        return (
            False,
            "deterministic_worker_failed",
            "the deterministic research worker(s) returned no result"
            + (f" ({codes})" if codes else ""),
        )
    if not any(result.facts for result in results):
        return (
            False,
            "empty_extraction",
            "the deterministic research worker(s) ran but extracted no fact at all",
        )
    if not any(result.sufficient for result in results):
        return (
            False,
            "insufficient_evidence",
            "the deterministic research worker(s) read the source, but it did not support "
            "enough facts to describe this company",
        )
    return True, None, None


def _committed_fallback_result(
    session: Session, *, company: Company, job: AgentJob
) -> WorkerResult | None:
    """A fallback attempt this exact job already committed, rebuilt from storage.

    Retry safety, and the reason the fallback's raw payload is written the way it
    is. A re-driven job — a recovered lease, a re-run of the same job row —
    must not spend a second Claude CLI call, must not write a second set of
    evidence rows, and must not produce a dossier that disagrees with the one
    already stored. Reusing the committed payload verbatim gives all three,
    because identical facts produce identical idempotency keys and an identical
    payload hashes to the submission that already exists.

    Best effort by design, and layered rather than relied upon: the authoritative
    guarantee against duplicate evidence remains the per-fact idempotency key in
    :func:`_store_facts`. This only prevents the wasted call and the second
    dossier version.
    """

    submissions = session.scalars(
        select(CompanyResearchSubmission)
        .where(
            CompanyResearchSubmission.company_id == company.id,
            CompanyResearchSubmission.request_context["agent_job_id"].as_string() == str(job.id),
        )
        .order_by(
            CompanyResearchSubmission.submitted_at.desc(),
            CompanyResearchSubmission.id.desc(),
        )
    ).all()
    for submission in submissions:
        payload = submission.payload if isinstance(submission.payload, dict) else {}
        entries = payload.get("workers")
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            rebuilt = research_fallback.result_from_raw(entry)
            if rebuilt is not None:
                return rebuilt
    return None


def _run_fallback(
    session: Session,
    *,
    company: Company,
    job: AgentJob,
    fallback: ResearchFallback | None,
    fallback_unavailable_reason: str | None,
    usable: bool,
    reason_code: str | None,
    reason: str | None,
) -> tuple[WorkerResult | None, FallbackRecord, bool]:
    """Decide whether to fall back, do it, and describe what happened.

    Returns ``(result, record, retryable)``. ``record`` is always produced, for
    every path including the ones that never call anything: an operator reading
    a Research report has to be able to tell "the fallback was not needed" from
    "the fallback is switched off" from "the fallback ran and found nothing",
    and an absent key says none of those.
    """

    if usable:
        return (
            None,
            research_fallback.not_attempted(
                "deterministic_result_usable",
                "The deterministic website worker produced a usable result, "
                "so no fallback was needed.",
            ),
            False,
        )
    if fallback is None:
        return (
            None,
            research_fallback.not_attempted(
                "fallback_unavailable",
                fallback_unavailable_reason
                or "The Claude research fallback is not enabled for this deployment.",
            ),
            False,
        )

    prior = _committed_fallback_result(session, company=company, job=job)
    if prior is not None:
        return prior, research_fallback.record_from_result(prior), False

    subject = FallbackSubject(
        company_name=company.name,
        domain=company.domain,
        country=company.country,
        industry=company.industry,
        linkedin_company_url=company.linkedin_company_url,
    )
    outcome = fallback.run(
        subject,
        reason_code=reason_code or "deterministic_result_unusable",
        reason=reason or "the deterministic research attempt produced nothing usable",
    )
    record = research_fallback.record_for(outcome)
    return (
        outcome.result,
        record,
        outcome.status is FallbackStatus.FAILED and outcome.retryable,
    )


def _same_reading(
    version: CompanyDossierVersion, *, sections: dict[str, Any], warnings: list[str]
) -> bool:
    """Would interpreting again produce exactly the version already stored?"""

    if list(version.warnings or []) != warnings:
        return False
    return all(
        getattr(version, name, None) == sections.get(name) for name in dossiers.SECTION_COLUMNS
    )


def _interpret_once(
    session: Session,
    *,
    company: Company,
    submission: CompanyResearchSubmission,
    submission_created: bool,
    sections: dict[str, Any],
    warnings: list[str],
    actor: str,
) -> CompanyDossierVersion:
    """Store one reading, or reuse the identical one this job already stored.

    A dossier version is an immutable *reading* of one submission. Re-running the
    identical reading of the identical submission produces the identical reading,
    so a second row would record a retry rather than any new knowledge — and
    would make the version number an execution counter instead of a history of
    what was understood. The reuse is deliberately narrow: only when the
    submission itself deduplicated, only for the same interpreter and version,
    and only when every section and warning matches exactly.
    """

    if not submission_created:
        existing = session.scalars(
            select(CompanyDossierVersion)
            .where(
                CompanyDossierVersion.company_id == company.id,
                CompanyDossierVersion.submission_id == submission.id,
                CompanyDossierVersion.interpreter == INTERPRETER,
                CompanyDossierVersion.interpreter_version == INTERPRETER_VERSION,
            )
            .order_by(CompanyDossierVersion.version_number.desc())
        ).first()
        if existing is not None and _same_reading(existing, sections=sections, warnings=warnings):
            if not existing.is_current:
                dossiers.select_current(session, company=company, version=existing, actor=actor)
            return existing

    return dossiers.interpret(
        session,
        company=company,
        submission=submission,
        interpreter=INTERPRETER,
        interpreter_version=INTERPRETER_VERSION,
        sections=sections,
        warnings=list(warnings),
        created_by=actor,
        make_current=True,
    )


def _basis(results: list[WorkerResult]) -> str:
    """Which sources the committed dossier actually rests on."""

    deterministic = any(
        result.facts and result.worker != research_fallback.FALLBACK_WORKER_NAME
        for result in results
    )
    assisted = any(
        result.facts and result.worker == research_fallback.FALLBACK_WORKER_NAME
        for result in results
    )
    if deterministic and assisted:
        return BASIS_BOTH
    if assisted:
        return BASIS_FALLBACK
    if deterministic:
        return BASIS_DETERMINISTIC
    return BASIS_NONE


def execute_step(
    session: Session,
    *,
    job: AgentJob,
    contact: Contact,
    workers: tuple[ResearchWorker, ...],
    options: dict[str, Any] | None = None,
    now: datetime | None = None,
    actor: str = RESEARCH_ACTOR,
    fallback: ResearchFallback | None = None,
    fallback_unavailable_reason: str | None = None,
) -> ResearchStep:
    """Research one Contact's Company and persist what was found.

    ``fallback`` is the second attempt, and ``None`` means there is no second
    attempt — the behaviour this module had before one existed. The adapter owns
    that decision; this module only owns *when* a second attempt is warranted.
    """

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
    attempted_workers: list[str] = []
    worker_failures: list[dict[str, Any]] = []
    retryable_failure: ResearchWorkerError | None = None
    for worker in workers:
        attempted_workers.append(worker.name)
        try:
            results.append(worker.run(request))
        except ResearchWorkerError as exc:
            # Every deterministic failure is recorded and the loop continues,
            # including a retryable one. This used to return RETRY immediately,
            # which was correct while the website was the only source: there was
            # nothing else to try, so trying again later was the whole answer.
            # It is not the answer now. A read timeout is one of the most common
            # ways a perfectly researchable company produces no dossier, and
            # retrying it produces the same timeout. The retryable outcome is
            # kept and returned below if — and only if — nothing usable is
            # produced by anything else.
            worker_failures.append(
                {
                    "worker": worker.name,
                    "reason_code": exc.code,
                    "retryable": exc.retryable,
                    "reason": str(exc),
                }
            )
            warnings.append(f"{worker.name}: {exc}")
            if exc.retryable and retryable_failure is None:
                retryable_failure = exc

    usable, unusable_code, unusable_reason = _assess(results, failures=worker_failures)
    fallback_result, fallback_record, fallback_retryable = _run_fallback(
        session,
        company=company,
        job=job,
        fallback=fallback,
        fallback_unavailable_reason=fallback_unavailable_reason,
        usable=usable,
        reason_code=unusable_code,
        reason=unusable_reason,
    )
    if fallback_record.error:
        warnings.append(
            f"{research_fallback.FALLBACK_WORKER_NAME}: {fallback_record.error}",
        )
    if fallback_result is not None:
        results.append(fallback_result)

    deterministic_summary: dict[str, Any] = {
        "workers": attempted_workers,
        "usable": usable,
        "reason_code": unusable_code,
        "reason": unusable_reason,
        "failures": worker_failures,
    }

    if not results:
        detail: dict[str, Any] = {
            "warnings": warnings,
            "deterministic": deterministic_summary,
            "fallback": fallback_record.as_dict(),
            "dossier_basis": BASIS_NONE,
        }
        # A transient fault anywhere in the chain means "ask again later" — but
        # only here, where nothing at all was produced. If a worker returned a
        # result and the *other* attempt then failed transiently, the result is
        # committed above instead: it was genuinely gathered, and discarding it
        # would make enabling the fallback worse than leaving it off, which is
        # the one outcome a fallback must never produce. The report records that
        # the second attempt was made and failed, so a re-run stays available.
        #
        # A *completed* fallback that found no citable evidence is not transient
        # and must not retry forever — that is an honest answer about this
        # company's public web presence, and it arrives as
        # ``fallback_retryable=False``.
        if retryable_failure is not None or fallback_retryable:
            code = (
                retryable_failure.code
                if retryable_failure is not None
                else fallback_record.error_code or "research_retry"
            )
            reason = (
                str(retryable_failure)
                if retryable_failure is not None
                else fallback_record.error or "the research fallback hit a transient fault"
            )
            return ResearchStep(
                kind=ResearchStepKind.RETRY,
                outcome="research_retry",
                result={"reason_code": code, "reason": reason, **detail},
                reason_code=code,
                reason=reason,
            )
        return ResearchStep(
            kind=ResearchStepKind.TERMINAL,
            outcome="research_failed",
            result={"reason_code": "all_workers_failed", **detail},
            reason_code="all_workers_failed",
            reason="; ".join(warnings) or "every research source failed",
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

    sections = _sections(results)
    version = _interpret_once(
        session,
        company=company,
        submission=submission,
        submission_created=created,
        sections=sections,
        warnings=warnings,
        actor=actor,
    )

    addressed = sorted(sections)
    unaddressed = sorted(
        section.value for section in DossierSection if section.value not in sections
    )
    source_count = len(sections.get(DossierSection.SOURCES.value) or [])
    unknown_count = len(sections.get(DossierSection.UNKNOWNS.value) or [])
    basis = _basis(results)

    # --- automatic Company Intelligence handoff ----------------------------
    # One idempotent, company-scoped job in the same transaction that commits
    # the dossier, so the standard worker fleet picks it up with no operator
    # step. A dossier committed with insufficient evidence is recorded, not
    # classified: queueing it would spend a model call on evidence Research
    # itself judged too thin.
    if sufficient:
        intelligence = intelligence_handoff.enqueue_after_research(session, company=company)
    else:
        intelligence = intelligence_handoff.skipped(
            intelligence_handoff.OUTCOME_DOSSIER_NOT_USABLE,
            "the dossier was committed with insufficient evidence; "
            "Company Intelligence was not queued",
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
        # --- the vocabulary every downstream reader projects -------------------
        # `workbench_agents.reader._research_outcome` builds the operator-facing
        # view from these exact keys, and a key it cannot find reads as a *zero*,
        # not as an absence. So a run that stored a dossier and three facts would
        # be reported as "0 of 9 sections addressed, 0 sources" — a false report on
        # the one screen whose job is honest state. They are written here, in the
        # stage's durable output, rather than taught to the reader: the Research
        # stage owns what it found, and one shape is easier to keep true than two.
        "dossier_version": version.version_number,
        "summary": _summary(
            results=results, facts_stored=stored, sources=source_count, sufficient=sufficient
        ),
        "sections_present": addressed,
        "sections_unaddressed": unaddressed,
        "source_count": source_count,
        "unknown_count": unknown_count,
        "producer": INTERPRETER,
        # --- execution truth, for the Research report --------------------------
        # What was attempted, what was considered unusable and why, whether the
        # fallback ran, and what the committed dossier actually rests on. Stored
        # here rather than derived later: only this frame knows the difference
        # between "the fallback was not needed" and "the fallback found nothing",
        # and both leave the same rows behind.
        "deterministic": deterministic_summary,
        "fallback": fallback_record.as_dict(),
        "dossier_basis": basis,
        # The automatic Research -> Company Intelligence handoff, recorded on
        # the durable result so the Workbench can show what happened and why.
        "company_intelligence": intelligence.as_dict(),
    }
    return ResearchStep(
        kind=ResearchStepKind.COMPLETE,
        outcome="research_completed" if sufficient else "research_completed_with_warnings",
        result={"domain_outcome": _domain_outcome(basis), **output},
        output_reference=output,
        committed=True,
    )


def _domain_outcome(basis: str) -> str:
    """The one-line outcome, honest about which source answered."""

    if basis == BASIS_FALLBACK:
        return "researched the company through cited public web sources"
    if basis == BASIS_BOTH:
        return "researched the company website and cited public web sources"
    return "researched the company website"
