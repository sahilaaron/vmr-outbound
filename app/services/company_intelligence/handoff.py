"""Automatic Research → Company Intelligence handoff.

The normal operating path for Company Intelligence is nobody's job:

    Research commits a new usable Company dossier
    → one idempotent, company-scoped Company Intelligence job is enqueued here
    → the standard worker fleet processes it
    → the resulting version is readable for every Contact linked to the Company.

This module is the first arrow. It is called by the Research Agent in the same
transaction that commits the dossier, so the dossier and the intent to classify
it are one atomic fact — a crash between them cannot happen.

What keeps it safe to call on every Research completion:

* **Digest idempotency.** The job key is ``(company, input digest)``, where the
  digest covers the dossier version, the exact sourced facts, the active
  taxonomy editions and the producer/policy versions. Unchanged input → the
  same key → ``jobs.enqueue`` returns the existing row. A new job appears only
  when the Research input or the producer policy/version actually changed.
* **Answered questions stay answered.** An existing
  :class:`~app.models.company_intelligence.CompanyIntelligenceVersion` for the
  digest short-circuits before any job is queued, so evidence that was already
  paid for is never queued again — the same pre-model check the runner makes.
* **Company-scoped.** One Company, one job, however many Campaign Contacts
  share the Company. Nothing here is per-Contact, and this module must never
  become a pipeline stage.
* **Feature-gated and truthful.** With ``company_intelligence`` off, or with a
  Company whose evidence cannot be assembled, the outcome says so; nothing is
  queued and nothing pretends to be.

Backfill (:mod:`.backfill`) remains the deliberate tool for historical
Companies, recovery and policy-version migrations. It is no longer part of the
normal path.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.company import Company
from app.services.company_intelligence import inputs as inputs_module
from app.services.company_intelligence import jobs as jobs_module
from app.services.company_intelligence import producer as producer_module
from app.services.company_intelligence.inputs import IntelligenceInputError
from app.services.operations import settings as operational

#: Recorded as ``requested_by`` on every automatically enqueued job, so the
#: queue, the audit trail and the Workbench can tell the automatic handoff from
#: an operator's button press or a backfill batch.
RESEARCH_HANDOFF_ACTOR = "research_handoff"

OUTCOME_QUEUED = "queued"
OUTCOME_ALREADY_QUEUED = "already_queued"
OUTCOME_ALREADY_ANSWERED = "already_answered"
OUTCOME_FEATURE_DISABLED = "feature_disabled"
OUTCOME_DOSSIER_NOT_USABLE = "dossier_not_usable"


@dataclass(frozen=True)
class HandoffOutcome:
    """What the automatic handoff did, in a shape the Research result can keep."""

    enqueued: bool
    outcome: str
    reason: str
    job_id: uuid.UUID | None = None
    input_digest: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "enqueued": self.enqueued,
            "outcome": self.outcome,
            "reason": self.reason,
            "job_id": str(self.job_id) if self.job_id else None,
            "input_digest": self.input_digest,
        }


def skipped(outcome: str, reason: str) -> HandoffOutcome:
    return HandoffOutcome(enqueued=False, outcome=outcome, reason=reason)


def enqueue_after_research(
    session: Session,
    *,
    company: Company,
    actor: str = RESEARCH_HANDOFF_ACTOR,
    settings: Settings | None = None,
) -> HandoffOutcome:
    """Queue Company Intelligence for a freshly committed dossier, idempotently.

    Never raises for an expected condition: a disabled feature, unassemblable
    evidence, an already-answered digest and an already-queued job are all
    ordinary answers the Research result records, not errors.
    """

    resolved = settings or get_settings()
    # The effective control rather than the environment's default, so that
    # turning Company Intelligence on takes effect for the next dossier committed
    # instead of the next deployment.
    if not operational.enabled(session, "company_intelligence", resolved):
        return skipped(
            OUTCOME_FEATURE_DISABLED,
            "Company Intelligence is switched off; nothing was queued.",
        )

    # The same input assembly the runner uses, so the digest here and the digest
    # at execution time are the same question by construction.
    from app.services.company_intelligence.runner import (  # local: avoids importing
        PRODUCER,  # the thinking seam at research-module import time
        PRODUCER_VERSION,
    )

    try:
        source = inputs_module.assemble(
            session,
            company=company,
            producer=PRODUCER,
            producer_version=PRODUCER_VERSION,
            policy_version=producer_module.POLICY_VERSION,
        )
    except IntelligenceInputError as exc:
        return skipped(exc.reason_code, exc.message)

    already = producer_module.existing_version(
        session, company_id=company.id, input_digest=source.digest
    )
    if already is not None:
        return HandoffOutcome(
            enqueued=False,
            outcome=OUTCOME_ALREADY_ANSWERED,
            reason=(
                f"version {already.version_number} already covers this exact evidence; "
                "no job was queued and no model call will be spent"
            ),
            input_digest=source.digest,
        )

    job, created = jobs_module.enqueue(
        session,
        company=company,
        input_digest=source.digest,
        producer_version=f"{PRODUCER}/{PRODUCER_VERSION}",
        policy_version=producer_module.POLICY_VERSION,
        input_reference={"origin": RESEARCH_HANDOFF_ACTOR},
        requested_by=actor,
        actor=actor,
    )
    return HandoffOutcome(
        enqueued=created,
        outcome=OUTCOME_QUEUED if created else OUTCOME_ALREADY_QUEUED,
        reason=(
            "Company Intelligence queued for the standard worker fleet"
            if created
            else "an equivalent Company Intelligence job is already queued or running"
        ),
        job_id=job.id,
        input_digest=source.digest,
    )
