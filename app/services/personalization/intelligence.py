"""Company Intelligence as a bounded Personalization input.

Company Intelligence is a company-scoped, versioned synthesis of the same
evidence Personalization already trusts — every classification's evidence links
point back at the exact ``Insight`` / ``InsightEvidence`` rows the Insights
Agent stored from the Research dossier. This module projects the **current**
version into a typed, bounded snapshot copy generation can read:

* **Structured context, never new claims.** Accepted values reach the prompt as
  read-only orientation. They carry no citable evidence ids, so the existing
  citation allow-list makes it impossible for a classification label to become
  a cited fact — an output citing one is refused exactly as any other
  unsupplied citation is.
* **Strict eligibility.** Only classifications from the current version that
  are resolved, evidence-backed, supported, normalized and free of conflict are
  accepted. Everything else is carried as *excluded, with its reason* — visible
  lineage, never silently downgraded into a fact.
* **Research stays authoritative.** A classification whose stored evidence
  contradicts the Research evidence it rests on is excluded outright.
* **Optional and non-blocking.** No Company Intelligence, a disabled feature,
  or a version with nothing eligible all produce a truthful snapshot and
  generation continues exactly as before. This is enrichment for a Campaign
  Contact's draft, not a pipeline stage.

The snapshot's :meth:`IntelligenceInputSnapshot.summary` is recorded inside the
draft's ``personalization_decision`` JSONB (and the stage output reference), so
every output can answer: was intelligence available, was it used, which exact
version, which values were accepted, and why the rest were not. Outputs written
before this integration simply lack the key — readers report that as
*lineage unavailable*, never as a fabricated answer.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.company import Company
from app.models.enums import (
    IntelligenceEvidenceStatus,
    IntelligenceNormalization,
    IntelligenceValueState,
)
from app.services.company_intelligence import read as intelligence_read
from app.services.operations import settings as operational

#: Bounds. The snapshot is a record, not a dump: enough accepted values to
#: orient copy, enough exclusions to explain the gaps, nothing unbounded.
MAX_ACCEPTED = 16
MAX_EXCLUDED = 24
MAX_PROMPT_VALUES = 8
MAX_TEXT = 200

STATUS_USED = "used"
STATUS_ELIGIBLE_UNUSED = "eligible_but_not_used"
STATUS_WITHHELD_WEAK_EVIDENCE = "withheld_weak_evidence_fallback"
STATUS_WITHHELD_POLICY = "withheld_company_context_minimum"
STATUS_NO_ELIGIBLE = "no_eligible_classifications"
STATUS_NO_VERSION = "no_current_version"
STATUS_FEATURE_DISABLED = "feature_disabled"

REASON_ACCEPTED = "resolved, evidence-backed and supported in the current version"
REASON_UNRESOLVED = "unresolved value"
REASON_CONFLICTED = "conflicted value"
REASON_UNKNOWN = "unknown value"
REASON_UNMAPPED = "unmapped value (no controlled term behind it)"
REASON_UNSUPPORTED = "not evidence-backed"
REASON_CONTRADICTED = "stored evidence contradicts the Research evidence; Research is authoritative"
REASON_NO_PROVENANCE = "no traceable evidence provenance"
REASON_OPERATOR_ASSERTION = "operator assertion without stored evidence"


def _clip(value: str | None) -> str | None:
    if value is None:
        return None
    return value[:MAX_TEXT]


@dataclass(frozen=True)
class IntelligenceClassificationInput:
    """One classification, as Personalization is allowed to see it."""

    dimension: str
    label: str
    model_value: str
    accepted: bool
    reason: str
    state: str
    evidence_status: str
    normalization: str
    source: str
    confidence: float | None
    confidence_band: str | None
    is_primary: bool
    #: Provenance back to the exact Insight rows the value rests on. Carried for
    #: lineage; deliberately NOT exposed as citable evidence ids to the model.
    evidence_insight_ids: tuple[str, ...] = ()
    evidence_source_urls: tuple[str, ...] = ()
    geo_relationship: str | None = None
    presence_kind: str | None = None

    def as_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "dimension": self.dimension,
            "label": _clip(self.label),
            "accepted": self.accepted,
            "reason": self.reason,
        }
        if self.accepted:
            record["confidence_band"] = self.confidence_band
            record["evidence_insight_ids"] = list(self.evidence_insight_ids[:8])
            if self.geo_relationship:
                record["geo_relationship"] = self.geo_relationship
            if self.presence_kind:
                record["presence_kind"] = self.presence_kind
            if self.is_primary:
                record["is_primary"] = True
        return record

    def prompt_line(self) -> str:
        parts = [f"{self.dimension}: {_clip(self.label)}"]
        if self.is_primary:
            parts.append("primary")
        if self.geo_relationship:
            parts.append(str(self.geo_relationship).replace("_", " "))
        if self.confidence_band:
            parts.append(f"{self.confidence_band} confidence")
        return f"- {parts[0]}" + (f" ({', '.join(parts[1:])})" if len(parts) > 1 else "")


@dataclass(frozen=True)
class IntelligenceInputSnapshot:
    """What Company Intelligence offered this generation, and what happened to it.

    ``status`` is the single truthful label the surfaces render; ``used`` is
    True only when at least one accepted value actually reached the prompt —
    availability alone never implies usage.
    """

    available: bool
    used: bool
    status: str
    version_id: uuid.UUID | None = None
    version_number: int | None = None
    producer_version: str | None = None
    input_digest: str | None = None
    accepted: tuple[IntelligenceClassificationInput, ...] = ()
    excluded: tuple[IntelligenceClassificationInput, ...] = ()

    @property
    def accepted_count(self) -> int:
        return len(self.accepted)

    @property
    def excluded_count(self) -> int:
        return len(self.excluded)

    def with_status(self, status: str, *, used: bool) -> IntelligenceInputSnapshot:
        return IntelligenceInputSnapshot(
            available=self.available,
            used=used,
            status=status,
            version_id=self.version_id,
            version_number=self.version_number,
            producer_version=self.producer_version,
            input_digest=self.input_digest,
            accepted=self.accepted,
            excluded=self.excluded,
        )

    def summary(self) -> dict[str, Any]:
        """The bounded lineage record persisted with every generation."""

        return {
            "available": self.available,
            "used": self.used,
            "status": self.status,
            "version_id": str(self.version_id) if self.version_id else None,
            "version_number": self.version_number,
            "producer_version": self.producer_version,
            "input_digest": self.input_digest,
            "accepted_count": self.accepted_count,
            "excluded_count": self.excluded_count,
            "accepted": [item.as_dict() for item in self.accepted],
            "excluded": [item.as_dict() for item in self.excluded],
        }

    def prompt_values(self) -> tuple[IntelligenceClassificationInput, ...]:
        return self.accepted[:MAX_PROMPT_VALUES]


def unavailable(status: str) -> IntelligenceInputSnapshot:
    return IntelligenceInputSnapshot(available=False, used=False, status=status)


def _exclusion_reason(view: intelligence_read.ClassificationView) -> str | None:
    """Why this classification may not reach Personalization, or None if it may.

    Ordered by decisiveness. Rejected classifications never appear here at all:
    the effective read model already removes them, which is the correct
    provenance-preserving behaviour (a rejected value is not part of the
    current understanding).
    """

    if view.state is IntelligenceValueState.CONFLICTED:
        return REASON_CONFLICTED
    if view.operator_only:
        # An operator assertion the current model version does not propose:
        # deliberately stored with no evidence. Real, but not evidence-backed.
        return REASON_OPERATOR_ASSERTION
    if view.normalization is IntelligenceNormalization.UNMAPPED:
        # An unmapped value is always also unresolved under the persisted
        # contract; the more specific reason is the useful one.
        return REASON_UNMAPPED
    if view.state is IntelligenceValueState.UNRESOLVED:
        return REASON_UNRESOLVED
    if view.state is not IntelligenceValueState.RESOLVED:
        return REASON_UNKNOWN
    if view.evidence_status is not IntelligenceEvidenceStatus.SUPPORTED:
        return REASON_UNSUPPORTED
    if any(item.contradicts for item in view.evidence):
        return REASON_CONTRADICTED
    if not view.evidence:
        return REASON_NO_PROVENANCE
    if not any(item.insight_id or item.source_url for item in view.evidence):
        return REASON_NO_PROVENANCE
    return None


def _as_input(
    view: intelligence_read.ClassificationView, *, accepted: bool, reason: str
) -> IntelligenceClassificationInput:
    return IntelligenceClassificationInput(
        dimension=view.dimension.value,
        label=view.display_value,
        model_value=view.model_value,
        accepted=accepted,
        reason=reason,
        state=view.state.value,
        evidence_status=view.evidence_status.value,
        normalization=view.normalization.value,
        source=view.source.value,
        confidence=view.confidence,
        confidence_band=view.confidence_band.value if view.confidence_band else None,
        is_primary=view.is_primary,
        evidence_insight_ids=tuple(
            str(item.insight_id) for item in view.evidence if item.insight_id
        )[:8]
        if accepted
        else (),
        evidence_source_urls=tuple(item.source_url for item in view.evidence if item.source_url)[:8]
        if accepted
        else (),
        geo_relationship=view.geo_relationship.value if view.geo_relationship else None,
        presence_kind=view.presence_kind.value if view.presence_kind else None,
    )


def assemble(
    session: Session,
    *,
    company: Company,
    settings: Settings | None = None,
) -> IntelligenceInputSnapshot:
    """Project the current Company Intelligence version for one generation.

    Read-only (safe for previews). Company-scoped: every Contact at the same
    Company sees the same current version, and each generation records only the
    shared version id it read. Only the current version is ever consulted —
    superseded versions are unreachable by construction, because the effective
    read model loads classifications for the current version alone.
    """

    resolved = settings or get_settings()
    # The effective control rather than the environment's default: whether
    # drafting may read intelligence is an administrator's durable setting now,
    # and this read already has the session it takes to answer that.
    if not operational.enabled(session, "company_intelligence", resolved):
        return unavailable(STATUS_FEATURE_DISABLED)

    read = intelligence_read.get_company_intelligence(session, company_id=company.id)
    if read is None or read.current_version is None:
        return unavailable(STATUS_NO_VERSION)

    accepted: list[IntelligenceClassificationInput] = []
    excluded: list[IntelligenceClassificationInput] = []
    for view in read.classifications:
        reason = _exclusion_reason(view)
        if reason is None:
            if len(accepted) < MAX_ACCEPTED:
                accepted.append(_as_input(view, accepted=True, reason=REASON_ACCEPTED))
        elif len(excluded) < MAX_EXCLUDED:
            excluded.append(_as_input(view, accepted=False, reason=reason))

    version = read.current_version
    snapshot = IntelligenceInputSnapshot(
        available=True,
        used=False,
        status=STATUS_NO_ELIGIBLE if not accepted else STATUS_ELIGIBLE_UNUSED,
        version_id=version.version_id,
        version_number=version.version_number,
        producer_version=version.producer_version,
        input_digest=version.input_digest,
        accepted=tuple(accepted),
        excluded=tuple(excluded),
    )
    return snapshot
