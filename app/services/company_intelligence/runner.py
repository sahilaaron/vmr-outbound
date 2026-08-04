"""Execute one Company Intelligence job end to end (CI-001).

The only place in this package that touches a language model, and the seam is
narrow on purpose: assemble the input, ask one question through the existing
``thinking`` contract, hand the answer to the deterministic producer, close the
job truthfully. Everything interesting is on either side of it.

Four properties worth stating plainly, because each is a rule the rest of the
system depends on.

**The model gets no tools.** ``allowed_tools=()``. A lookup here would produce a
classification citing a source that never entered the dossier, so it could never
be shown next to the evidence it claims to rest on, and nothing downstream could
tell it apart from one that was.

**The model's confidence is an opinion, never verification.** It is stored as a
number and banded for display, and no code path anywhere treats a high one as
grounds to skip review, release a suppression, or make a Contact eligible.

**Nothing runs unless it is switched on.** The ``company_intelligence`` feature
flag is default-off (FND-007), and this module refuses rather than quietly
producing nothing — a silent no-op is the failure mode where an operator watches
an empty queue and concludes the software is broken.

**Prompts and raw answers are not persisted.** A digest of the answer is stored
so two runs can be compared; the text is not, because it contains the prompt's
framing and would be the one place configuration could leak into the database.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.company import Company
from app.models.company_intelligence import CompanyIntelligenceJob
from app.services.company_intelligence import inputs as inputs_module
from app.services.company_intelligence import jobs as jobs_module
from app.services.company_intelligence import producer as producer_module
from app.services.company_intelligence import prompts
from app.services.company_intelligence.inputs import IntelligenceInputError
from app.services.company_intelligence.producer import (
    POLICY_VERSION,
    IntelligenceMalformed,
    IntelligenceProducerError,
)
from app.services.thinking.claude_cli import ClaudeCliThinker
from app.services.thinking.contracts import Thinker, ThinkingError, ThinkingRequest

#: The opaque producer name recorded on every version. Provider-neutral: nothing
#: in the schema or the read model branches on it, so replacing the model behind
#: it is a version bump rather than a migration.
PRODUCER = "company-intelligence"
PRODUCER_VERSION = "1"

DEFAULT_TIMEOUT_SECONDS = 240.0

FEATURE_DISABLED_CODE = "feature_disabled"


class IntelligenceDisabled(IntelligenceProducerError):
    """The Company Intelligence feature switch is off."""

    retryable = False
    code = FEATURE_DISABLED_CODE


@dataclass(frozen=True)
class RunOutcome:
    """What executing one job did, in the vocabulary the queue records."""

    succeeded: bool
    company_id: uuid.UUID
    code: str
    message: str
    intelligence_version_id: uuid.UUID | None = None
    version_number: int | None = None
    created: bool = False
    retryable: bool = False
    detail: dict[str, Any] | None = None

    def as_result(self) -> dict[str, Any]:
        return {
            "company_id": str(self.company_id),
            "outcome": self.code,
            "intelligence_version_id": (
                str(self.intelligence_version_id) if self.intelligence_version_id else None
            ),
            "version_number": self.version_number,
            "created": self.created,
            "summary": self.message,
        }


ThinkerFactory = Callable[[Settings], Thinker]


def default_thinker_factory(settings: Settings) -> Thinker:
    return ClaudeCliThinker(settings=settings)


def produce_for_company(
    session: Session,
    *,
    company: Company,
    thinker_factory: ThinkerFactory | None = None,
    settings: Settings | None = None,
    job: CompanyIntelligenceJob | None = None,
    actor: str = producer_module.PRODUCER_ACTOR,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> RunOutcome:
    """Classify one Company from its committed evidence.

    Returns a :class:`RunOutcome` rather than raising for expected conditions —
    no dossier, no facts, a refused model call — because every one of those is
    something an operator needs to read, not an exception to swallow.
    """

    resolved_settings = settings or get_settings()
    if not resolved_settings.features.company_intelligence:
        return RunOutcome(
            succeeded=False,
            company_id=company.id,
            code=FEATURE_DISABLED_CODE,
            message=(
                "Company Intelligence is switched off. Set FEATURES__COMPANY_INTELLIGENCE=true "
                "to enable it; nothing is produced while it is off."
            ),
            retryable=False,
        )

    try:
        source = inputs_module.assemble(
            session,
            company=company,
            producer=PRODUCER,
            producer_version=PRODUCER_VERSION,
            policy_version=POLICY_VERSION,
        )
    except IntelligenceInputError as exc:
        return RunOutcome(
            succeeded=False,
            company_id=company.id,
            code=exc.reason_code,
            message=exc.message,
            retryable=False,
        )

    # The identical question may already have an answer. Checking before the
    # model call, not after, is the difference between an idempotent re-run and
    # an idempotent re-run that still costs a model invocation.
    already = producer_module.existing_version(
        session, company_id=company.id, input_digest=source.digest
    )
    if already is not None:
        return RunOutcome(
            succeeded=True,
            company_id=company.id,
            code="reused_existing_version",
            message=(
                f"version {already.version_number} already covers this exact evidence "
                "under this producer; nothing was re-run"
            ),
            intelligence_version_id=already.id,
            version_number=already.version_number,
            created=False,
        )

    vocabularies = producer_module.vocabulary_for_prompt(session)
    request = ThinkingRequest(
        prompt=prompts.classification_prompt(source, vocabularies=vocabularies),
        purpose="company_intelligence",
        timeout_seconds=timeout_seconds,
        # No tools. See the module docstring.
        allowed_tools=(),
    )
    thinker = (thinker_factory or default_thinker_factory)(resolved_settings)
    try:
        answer = thinker.think(request)
    except ThinkingError as exc:
        return RunOutcome(
            succeeded=False,
            company_id=company.id,
            code=exc.code,
            message=str(exc),
            retryable=exc.retryable,
            detail=exc.detail,
        )

    try:
        result = producer_module.produce(
            session,
            company=company,
            source=source,
            answer=answer.payload,
            raw_answer=answer.raw,
            job_id=job.id if job is not None else None,
            created_by=f"{answer.producer}/{answer.producer_version}",
            actor=actor,
        )
    except IntelligenceMalformed as exc:
        return RunOutcome(
            succeeded=False,
            company_id=company.id,
            code=exc.code,
            message=exc.message,
            retryable=True,
            detail=exc.detail,
        )
    except IntelligenceProducerError as exc:
        return RunOutcome(
            succeeded=False,
            company_id=company.id,
            code=exc.code,
            message=exc.message,
            retryable=False,
            detail=exc.detail,
        )

    version = result.version
    return RunOutcome(
        succeeded=True,
        company_id=company.id,
        code="intelligence_produced" if result.created else "reused_existing_version",
        message=(
            f"{result.classifications} classification(s): {result.supported} evidence-backed, "
            f"{result.unresolved} unresolved or conflicted, {result.conflicts} conflict(s)"
        ),
        intelligence_version_id=version.id,
        version_number=version.version_number,
        created=result.created,
        detail={"warnings": list(result.warnings)[:10]},
    )


def execute_job(
    session: Session,
    *,
    job: CompanyIntelligenceJob,
    thinker_factory: ThinkerFactory | None = None,
    settings: Settings | None = None,
    worker_id: str = "worker",
) -> RunOutcome:
    """Run one leased job and record its outcome on the queue."""

    jobs_module.mark_running(session, job=job)
    company = session.get(Company, job.company_id)
    if company is None:
        outcome = RunOutcome(
            succeeded=False,
            company_id=job.company_id,
            code=inputs_module.REASON_COMPANY_MISSING,
            message="the company this job was queued for no longer exists",
            retryable=False,
        )
    else:
        outcome = produce_for_company(
            session,
            company=company,
            thinker_factory=thinker_factory,
            settings=settings,
            job=job,
            actor=worker_id,
        )

    if outcome.succeeded:
        jobs_module.mark_succeeded(session, job=job, result=outcome.as_result(), actor=worker_id)
    else:
        jobs_module.mark_failed(
            session,
            job=job,
            code=outcome.code,
            message=outcome.message,
            retryable=outcome.retryable,
            detail=outcome.detail,
            actor=worker_id,
        )
    return outcome


def run_next(
    session: Session,
    *,
    worker_id: str,
    lease_seconds: float = 300.0,
    thinker_factory: ThinkerFactory | None = None,
    settings: Settings | None = None,
) -> RunOutcome | None:
    """Claim and execute at most one job. Returns None when the queue is idle."""

    job = jobs_module.claim_next(session, worker_id=worker_id, lease_seconds=lease_seconds)
    if job is None:
        return None
    return execute_job(
        session,
        job=job,
        thinker_factory=thinker_factory,
        settings=settings,
        worker_id=worker_id,
    )
