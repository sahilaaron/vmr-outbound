"""Which Research knowledge an Insights execution reads, and what it recorded.

Two questions that look alike and are not, answered by two functions:

* :func:`current_state` — *what should this run use?* The Company's **current
  eligible** Research knowledge at the moment Insights executes.
* :func:`recorded` — *what did a run actually use?* Read-only provenance for one
  historical Insights job, taken from what that execution itself recorded.

The distinction is the product contract. Research is not a campaign-execution
artefact that downstream Agents bind to: it is an independent, continuously
enrichable Company knowledge function that may run today, tomorrow, every day,
or outside any campaign. Insights therefore reads the Company's currently
selected dossier and the sourced facts committed alongside it, and records that
selection on its own result. A later Research run does not retroactively
invalidate an older Insights result, and an older Research run is not a
prerequisite an Insights rerun must reproduce.

Lineage answers "what did this run use?". Lineage never answers "may this Agent
run at all?".

*Current* is not *latest-row-blindly*. The authority is the Company's own
selection — the one ``CompanyDossierVersion`` marked ``is_current`` — together
with the immutable submission behind it and the Research execution whose
durable output names exactly that pair. Nothing here relaxes evidence
eligibility, citation rules or Research authority; a Company with no selected
dossier, or a selected dossier no Research execution committed, still has no
usable Research knowledge and Insights still blocks truthfully.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.company_dossier import CompanyDossierVersion, CompanyResearchSubmission
from app.models.enums import AgentIdentifier
from app.models.verification_job import AgentJob
from app.services.companies import dossiers


@dataclass(frozen=True)
class ResearchLineage:
    research_job: AgentJob
    submission: CompanyResearchSubmission
    dossier: CompanyDossierVersion


def _uuid(value: object) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _committing_research_job(
    session: Session,
    *,
    company_id: uuid.UUID,
    dossier: CompanyDossierVersion,
) -> AgentJob | None:
    """The Research execution whose durable output committed exactly this dossier.

    Needed because the sourced facts a dossier rests on are keyed per Research
    execution (``research:<job id>:...``), so reading the dossier without its
    facts would offer Insights a prompt it could not source a single claim from.

    No filter on the job's own terminal status, deliberately. ``job.result`` is
    written by ``jobs.mark_completed`` and by nothing else, so a row that names
    this submission and version *is* a completed commit; re-asserting that
    through the status column would put an execution-status gate back in front
    of Company knowledge. Newest first: a re-run that re-committed the same
    reading also re-read its sources, and its facts are the fresher ones.
    """

    return session.scalars(
        select(AgentJob)
        .where(
            AgentJob.agent_id == AgentIdentifier.RESEARCH,
            AgentJob.result["company_id"].astext == str(company_id),
            AgentJob.result["submission_id"].astext == str(dossier.submission_id),
            AgentJob.result["dossier_version"].astext == str(dossier.version_number),
        )
        .order_by(
            AgentJob.finished_at.desc().nulls_last(),
            AgentJob.created_at.desc(),
            AgentJob.id.desc(),
        )
    ).first()


def current_state(session: Session, *, company_id: uuid.UUID) -> ResearchLineage | None:
    """The Company's current eligible Research knowledge, as one coherent snapshot.

    Read once at the start of an execution and used for its duration, so a
    Research run that commits mid-execution cannot split one Insights result
    across two states of the world.
    """

    dossier = dossiers.current_version(session, company_id=company_id)
    if dossier is None:
        return None
    submission = session.get(CompanyResearchSubmission, dossier.submission_id)
    if submission is None or submission.company_id != company_id:
        return None
    research_job = _committing_research_job(session, company_id=company_id, dossier=dossier)
    if research_job is None:
        return None
    return ResearchLineage(research_job=research_job, submission=submission, dossier=dossier)


def recorded(
    session: Session,
    *,
    insights_job: AgentJob,
    company_id: uuid.UUID,
) -> ResearchLineage | None:
    """The Research artefacts one Insights execution recorded having used.

    Historical, never a substitute for :func:`current_state`: the answer here is
    whatever that run wrote down, so a later Research run cannot change it. The
    durable result is preferred over the queued input reference, because only
    the result was written *after* the input was selected.

    An execution that recorded nothing has no provenance, and that is reported
    as unavailable rather than reconstructed from the Company's present state.
    """

    if insights_job.agent_id is not AgentIdentifier.INSIGHTS:
        return None
    for source in (insights_job.result, insights_job.input_reference):
        if not isinstance(source, dict):
            continue
        lineage = _from_record(session, source, company_id=company_id)
        if lineage is not None:
            return lineage
    return None


#: The result key and the input-reference key for the same fact. Insights writes
#: the first pair on its own output; queued jobs written before this contract
#: carry the second, and both must still read.
_SUBMISSION_KEYS = ("submission_id", "research_submission_id")
_DOSSIER_KEYS = ("dossier_version_id", "research_dossier_version_id")


def _first_uuid(source: dict[str, object], keys: tuple[str, ...]) -> uuid.UUID | None:
    for key in keys:
        value = _uuid(source.get(key))
        if value is not None:
            return value
    return None


def _from_record(
    session: Session,
    source: dict[str, object],
    *,
    company_id: uuid.UUID,
) -> ResearchLineage | None:
    research_job_id = _uuid(source.get("research_job_id"))
    submission_id = _first_uuid(source, _SUBMISSION_KEYS)
    dossier_id = _first_uuid(source, _DOSSIER_KEYS)
    if research_job_id is None or submission_id is None or dossier_id is None:
        return None
    research_job = session.get(AgentJob, research_job_id)
    submission = session.get(CompanyResearchSubmission, submission_id)
    dossier = session.get(CompanyDossierVersion, dossier_id)
    if research_job is None or submission is None or dossier is None:
        return None
    if research_job.agent_id is not AgentIdentifier.RESEARCH:
        return None
    if submission.company_id != company_id or dossier.company_id != company_id:
        return None
    if dossier.submission_id != submission.id:
        return None
    return ResearchLineage(research_job=research_job, submission=submission, dossier=dossier)
