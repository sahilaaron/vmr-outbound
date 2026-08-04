"""Evidence-bound structured Employee Size derivation (INS-002).

Research owns collection.  This module receives only committed Research Insight
evidence handles, validates those handles against the subject Company, parses
numeric wording deterministically, and appends one typed Insight describing the
settled, conflicted, stale, unresolved, or unavailable result.
"""

from __future__ import annotations

import enum
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.company_dossier import CompanyDossierVersion
from app.models.enums import InsightKind, InsightState
from app.models.insight import Insight, InsightEvidence
from app.models.verification_job import AgentJob
from app.services.insights import evidence as insight_service

EMPLOYEE_SIZE_TYPE = "employee_size"
DERIVATION_VERSION = "employee-size/v1"
MAX_EVIDENCE_AGE_DAYS = 365


class EmployeeSizeBand(enum.StrEnum):
    ONE_TO_TEN = "1_10"
    ELEVEN_TO_FIFTY = "11_50"
    FIFTY_ONE_TO_ONE_HUNDRED = "51_100"
    ONE_HUNDRED_ONE_TO_TWO_FIFTY = "101_250"
    TWO_FIFTY_ONE_TO_FIVE_HUNDRED = "251_500"
    FIVE_HUNDRED_ONE_TO_ONE_THOUSAND = "501_1000"
    ONE_THOUSAND_ONE_TO_FIVE_THOUSAND = "1001_5000"
    FIVE_THOUSAND_ONE_TO_TEN_THOUSAND = "5001_10000"
    TEN_THOUSAND_ONE_PLUS = "10001_plus"
    UNKNOWN = "unknown"


class EmployeeSizeStatus(enum.StrEnum):
    SUPPORTED = "supported"
    UNRESOLVED = "unresolved"
    CONFLICTED = "conflicted"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


_BANDS: tuple[tuple[int, int | None, EmployeeSizeBand], ...] = (
    (1, 10, EmployeeSizeBand.ONE_TO_TEN),
    (11, 50, EmployeeSizeBand.ELEVEN_TO_FIFTY),
    (51, 100, EmployeeSizeBand.FIFTY_ONE_TO_ONE_HUNDRED),
    (101, 250, EmployeeSizeBand.ONE_HUNDRED_ONE_TO_TWO_FIFTY),
    (251, 500, EmployeeSizeBand.TWO_FIFTY_ONE_TO_FIVE_HUNDRED),
    (501, 1_000, EmployeeSizeBand.FIVE_HUNDRED_ONE_TO_ONE_THOUSAND),
    (1_001, 5_000, EmployeeSizeBand.ONE_THOUSAND_ONE_TO_FIVE_THOUSAND),
    (5_001, 10_000, EmployeeSizeBand.FIVE_THOUSAND_ONE_TO_TEN_THOUSAND),
    (10_001, None, EmployeeSizeBand.TEN_THOUSAND_ONE_PLUS),
)

_NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)"
_PEOPLE = r"(?:employees?|people|staff(?:\s+members?)?|workers?|team(?:\s+members?)?|workforce)"
_APPROX = r"(?:approximately|approx\.?|about|around|roughly|nearly|circa)"
_RANGE = re.compile(
    rf"(?:between\s+)?(?P<low>{_NUMBER})\s*(?:-|to|and)\s*(?P<high>{_NUMBER})"
    rf"\s*{_PEOPLE}\b",
    re.IGNORECASE,
)
_LOWER = re.compile(
    rf"(?P<op>more\s+than|over|above|at\s+least|minimum\s+of)\s+"
    rf"(?P<number>{_NUMBER})\s*{_PEOPLE}\b",
    re.IGNORECASE,
)
_UPPER = re.compile(
    rf"(?P<op>fewer\s+than|less\s+than|under|below|at\s+most|up\s+to)\s+"
    rf"(?P<number>{_NUMBER})\s*{_PEOPLE}\b",
    re.IGNORECASE,
)
_COUNT_BEFORE_TERM = re.compile(
    rf"(?P<approx>{_APPROX}\s+)?(?P<number>{_NUMBER})\s*{_PEOPLE}\b",
    re.IGNORECASE,
)
_COUNT_AFTER_VERB = re.compile(
    rf"(?:employs?|employed|has|comprises?|consists\s+of|workforce\s+of|staff\s+of|"
    rf"team\s+of)\s+(?P<approx>{_APPROX}\s+)?(?P<number>{_NUMBER})(?:\s*{_PEOPLE}\b)?",
    re.IGNORECASE,
)
_HISTORICAL = re.compile(
    r"\b(?:formerly|previously|at\s+the\s+time|then\s+employed|"
    r"as\s+of\s+(?:19|20)\d{2}|in\s+(?:19|20)\d{2})\b",
    re.IGNORECASE,
)
_EMPLOYEE_LANGUAGE = re.compile(_PEOPLE, re.IGNORECASE)
_EXCLUDED = re.compile(
    r"\b(?:customer|client|partner|parent\s+company|subsidiar(?:y|ies)|portfolio\s+companies|"
    r"across\s+(?:its|the)\s+(?:portfolio|group)|group-wide|office-specific|"
    r"(?:at|in)\s+(?:the\s+)?[A-Z][A-Za-z.-]+\s+office|contractors?|"
    r"plans?\s+to\s+hire|planned\s+hiring|will\s+hire|hiring\s+target|"
    r"laid\s+off|layoffs?|jobs?\s+to\s+be\s+cut|founded\s+by\s+a\s+team)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ResearchEvidenceHandle:
    handle: uuid.UUID
    research_insight_id: uuid.UUID
    company_id: uuid.UUID
    claim: str
    evidence: InsightEvidence

    def prompt_value(self) -> dict[str, object]:
        return {
            "handle": str(self.handle),
            "claim": self.claim[:2_000],
            "source_url": self.evidence.source_url,
            "source_title": (
                self.evidence.source_title[:1_000] if self.evidence.source_title else None
            ),
            "published_at": (
                self.evidence.published_at.isoformat() if self.evidence.published_at else None
            ),
            "retrieved_at": (
                self.evidence.retrieved_at.isoformat() if self.evidence.retrieved_at else None
            ),
            "evidence_summary": (
                self.evidence.evidence_summary[:2_000] if self.evidence.evidence_summary else None
            ),
            "excerpt": self.evidence.excerpt[:2_000] if self.evidence.excerpt else None,
            "confidence": self.evidence.confidence,
        }


@dataclass(frozen=True)
class ParsedObservation:
    handle: uuid.UUID
    source_wording: str
    exact_count: int | None
    approximate_count: int | None
    lower_bound: int | None
    upper_bound: int | None
    normalized_band: EmployeeSizeBand
    temporal_status: str
    observation_date: datetime | None
    confidence: float | None
    rationale: str

    def payload(self) -> dict[str, object]:
        return {
            "evidence_handles": [str(self.handle)],
            "source_wording": self.source_wording,
            "exact_count": self.exact_count,
            "approximate_count": self.approximate_count,
            "lower_bound": self.lower_bound,
            "upper_bound": self.upper_bound,
            "normalized_band": self.normalized_band.value,
            "temporal_status": self.temporal_status,
            "observation_date": (
                self.observation_date.isoformat() if self.observation_date else None
            ),
            "confidence": self.confidence,
            "rationale": self.rationale,
        }


def band_for_count(value: int) -> EmployeeSizeBand:
    if value < 1:
        return EmployeeSizeBand.UNKNOWN
    for lower, upper, band in _BANDS:
        if value >= lower and (upper is None or value <= upper):
            return band
    return EmployeeSizeBand.UNKNOWN


def _band_for_interval(lower: int | None, upper: int | None) -> EmployeeSizeBand:
    effective_lower = lower if lower is not None else 1
    for band_lower, band_upper, band in _BANDS:
        if effective_lower < band_lower:
            continue
        if band_upper is None:
            return band if upper is None or upper >= effective_lower else EmployeeSizeBand.UNKNOWN
        if upper is not None and upper <= band_upper:
            return band
    return EmployeeSizeBand.UNKNOWN


def _number(value: str) -> int:
    return int(value.replace(",", ""))


def _observation_date(item: ResearchEvidenceHandle) -> datetime | None:
    return item.evidence.published_at or item.evidence.freshness_at or item.evidence.retrieved_at


def _material(item: ResearchEvidenceHandle) -> str:
    return "\n".join(
        part for part in (item.claim, item.evidence.excerpt, item.evidence.evidence_summary) if part
    )


def _source_wording(item: ResearchEvidenceHandle, proposed: object) -> str:
    material = _material(item)
    if isinstance(proposed, str):
        cleaned = proposed.strip()
        if cleaned and cleaned.casefold() in material.casefold():
            return cleaned[:1_000]
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", material):
        if _EMPLOYEE_LANGUAGE.search(sentence) and re.search(_NUMBER, sentence):
            return sentence.strip()[:1_000]
    return material.strip()[:1_000]


def _parse_observation(
    item: ResearchEvidenceHandle,
    *,
    proposed_wording: object,
    proposed_context: object,
    now: datetime,
) -> ParsedObservation | None:
    wording = _source_wording(item, proposed_wording)
    material = _material(item)
    if not wording or not _EMPLOYEE_LANGUAGE.search(material):
        return None
    context = proposed_context.strip().casefold() if isinstance(proposed_context, str) else ""
    if context in {
        "parent",
        "subsidiary",
        "portfolio",
        "office",
        "customer",
        "partner",
        "planned",
        "layoff",
        "contractor",
    } or _EXCLUDED.search(material):
        return None

    normalized = wording.replace("\u2013", "-").replace("\u2014", "-").replace("\u2212", "-")
    exact: int | None = None
    approximate: int | None = None
    lower: int | None = None
    upper: int | None = None
    rationale: str

    match = _RANGE.search(normalized)
    if match:
        lower, upper = _number(match.group("low")), _number(match.group("high"))
        if lower > upper:
            lower, upper = upper, lower
        rationale = "The source states an explicit workforce range."
    else:
        match = _LOWER.search(normalized)
        if match:
            raw = _number(match.group("number"))
            lower = raw if match.group("op").casefold() in {"at least", "minimum of"} else raw + 1
            rationale = "The source states only a lower workforce bound."
        else:
            match = _UPPER.search(normalized)
            if match:
                raw = _number(match.group("number"))
                upper = raw if match.group("op").casefold() in {"at most", "up to"} else raw - 1
                rationale = "The source states only an upper workforce bound."
            else:
                match = _COUNT_AFTER_VERB.search(normalized) or _COUNT_BEFORE_TERM.search(
                    normalized
                )
                if not match:
                    return None
                raw = _number(match.group("number"))
                is_approximate = bool(match.groupdict().get("approx"))
                if is_approximate:
                    approximate = raw
                    band = band_for_count(raw)
                    lower, upper = next(
                        (
                            (band_lower, band_upper)
                            for band_lower, band_upper, candidate in _BANDS
                            if candidate is band
                        ),
                        (None, None),
                    )
                    rationale = (
                        "The source gives an approximate count; only its normalized band "
                        "is settled."
                    )
                else:
                    exact = raw
                    lower = upper = raw
                    rationale = "The source states an exact workforce count."

    if (lower is not None and lower < 1) or (upper is not None and upper < 1):
        return None
    band = _band_for_interval(lower, upper)
    observed = _observation_date(item)
    historical = context == "historical" or bool(_HISTORICAL.search(material))
    if observed is not None and observed < now - timedelta(days=MAX_EVIDENCE_AGE_DAYS):
        historical = True
    return ParsedObservation(
        handle=item.handle,
        source_wording=wording,
        exact_count=exact,
        approximate_count=approximate,
        lower_bound=lower,
        upper_bound=upper,
        normalized_band=band,
        temporal_status="historical" if historical else "current",
        observation_date=observed,
        confidence=item.evidence.confidence,
        rationale=rationale,
    )


def research_evidence_catalog(
    session: Session,
    *,
    research_job_id: uuid.UUID,
    company_id: uuid.UUID,
) -> tuple[ResearchEvidenceHandle, ...]:
    """Every committed sourced fact produced by one exact Research job."""

    insights = session.scalars(
        select(Insight)
        .where(
            Insight.company_id == company_id,
            Insight.idempotency_key.like(f"research:{research_job_id}:%"),
        )
        .order_by(Insight.created_at, Insight.id)
    ).all()
    catalog: list[ResearchEvidenceHandle] = []
    for insight in insights:
        rows = session.scalars(
            select(InsightEvidence)
            .where(InsightEvidence.insight_id == insight.id)
            .order_by(InsightEvidence.created_at, InsightEvidence.id)
        ).all()
        catalog.extend(
            ResearchEvidenceHandle(
                handle=row.id,
                research_insight_id=insight.id,
                company_id=company_id,
                claim=insight.claim,
                evidence=row,
            )
            for row in rows
        )
    return tuple(catalog)


def bounded_prompt_catalog(
    catalog: tuple[ResearchEvidenceHandle, ...], *, maximum_bytes: int = 16_000
) -> tuple[ResearchEvidenceHandle, ...]:
    """Return a complete-JSON evidence prefix small enough for one bounded prompt."""

    selected: list[ResearchEvidenceHandle] = []
    for item in catalog[:100]:
        encoded = json.dumps(
            [entry.prompt_value() for entry in (*selected, item)],
            default=str,
            ensure_ascii=False,
        ).encode("utf-8")
        if len(encoded) > maximum_bytes:
            break
        selected.append(item)
    return tuple(selected)


def _candidate_rows(raw: object) -> tuple[dict[str, object], ...]:
    if not isinstance(raw, dict):
        return ()
    candidates = raw.get("candidates")
    if not isinstance(candidates, list):
        return ()
    return tuple(item for item in candidates[:20] if isinstance(item, dict))


def _handles(raw: object) -> tuple[uuid.UUID, ...] | None:
    values = raw if isinstance(raw, list) else []
    parsed: list[uuid.UUID] = []
    for value in values[:10]:
        if not isinstance(value, str):
            return None
        try:
            parsed.append(uuid.UUID(value))
        except ValueError:
            return None
    return tuple(dict.fromkeys(parsed)) if parsed else None


def _derived_evidence(
    catalog: dict[uuid.UUID, ResearchEvidenceHandle], handles: tuple[uuid.UUID, ...]
) -> list[insight_service.EvidenceInput]:
    output: list[insight_service.EvidenceInput] = []
    seen: set[tuple[str, int]] = set()
    for handle in handles:
        source = catalog[handle].evidence
        assert source.retrieved_at is not None
        assert source.evidence_summary is not None
        assert source.confidence is not None
        assert source.extraction_method is not None
        identity = (source.source_url, source.version)
        if identity in seen:
            continue
        seen.add(identity)
        output.append(
            insight_service.EvidenceInput(
                source_url=source.source_url,
                source_title=source.source_title,
                published_at=source.published_at,
                retrieved_at=source.retrieved_at,
                excerpt=source.excerpt,
                evidence_summary=source.evidence_summary,
                confidence=source.confidence,
                extraction_method=source.extraction_method,
                freshness_at=source.freshness_at,
                source_record_type="insight_evidence",
                source_record_id=source.id,
                version=source.version,
            )
        )
    return output


def _valid_evidence(item: ResearchEvidenceHandle) -> bool:
    """A handle is usable only when the existing evidence contract is complete."""

    evidence = item.evidence
    return bool(
        evidence.source_url
        and evidence.retrieved_at is not None
        and evidence.evidence_summary
        and evidence.evidence_summary.strip()
        and evidence.confidence is not None
        and evidence.extraction_method
        and evidence.extraction_method.strip()
    )


def _claim(payload: dict[str, object]) -> str:
    status = payload["status"]
    band = str(payload.get("normalized_band") or EmployeeSizeBand.UNKNOWN.value)
    if status == EmployeeSizeStatus.SUPPORTED.value:
        exact = payload.get("exact_count")
        approximate = payload.get("approximate_count")
        if isinstance(exact, int):
            return f"Employee size: {exact:,} employees ({band})."
        if isinstance(approximate, int):
            return f"Employee size: approximately {approximate:,} employees ({band})."
        return f"Employee size: {band}."
    if status == EmployeeSizeStatus.CONFLICTED.value:
        return "Employee size is conflicted across current Research evidence."
    if status == EmployeeSizeStatus.STALE.value:
        return "Only historical or stale Employee Size evidence is available."
    if status == EmployeeSizeStatus.UNRESOLVED.value:
        return "Employee Size evidence does not settle one normalized band."
    return "Employee Size is unavailable from the committed Research evidence."


def derive_and_store(
    session: Session,
    *,
    company_id: uuid.UUID,
    insights_job: AgentJob,
    dossier: CompanyDossierVersion,
    catalog: tuple[ResearchEvidenceHandle, ...],
    model_output: object,
    actor: str,
    now: datetime | None = None,
) -> Insight:
    """Append one deterministic Employee Size aggregate for an Insights execution."""

    existing = session.scalars(
        select(Insight).where(
            Insight.company_id == company_id,
            Insight.idempotency_key == f"insights-agent:{insights_job.id}:employee-size",
        )
    ).one_or_none()
    if existing is not None:
        if (
            existing.insight_type != EMPLOYEE_SIZE_TYPE
            or existing.producer_job_id != insights_job.id
            or existing.dossier_version_id != dossier.id
        ):
            raise insight_service.InsightError(
                "Employee Size idempotency key has inconsistent immutable lineage"
            )
        return existing

    derived_at = now or datetime.now(UTC)
    by_handle = {
        item.handle: item
        for item in catalog
        if item.company_id == company_id and _valid_evidence(item)
    }
    observations: list[ParsedObservation] = []
    invalid_handle = False
    ambiguous_employee_wording = False
    requested_handles: list[uuid.UUID] = []

    for candidate in _candidate_rows(model_output):
        handles = _handles(candidate.get("evidence_handles"))
        if handles is None or any(handle not in by_handle for handle in handles):
            invalid_handle = True
            continue
        requested_handles.extend(handles)
        for handle in handles:
            source = by_handle[handle]
            if _EMPLOYEE_LANGUAGE.search(_material(source)):
                ambiguous_employee_wording = True
            parsed = _parse_observation(
                source,
                proposed_wording=candidate.get("source_wording"),
                proposed_context=candidate.get("observation_context"),
                now=derived_at,
            )
            if parsed is not None:
                observations.append(parsed)

    unique_observations = {item.handle: item for item in observations}
    observations = list(unique_observations.values())
    current = [item for item in observations if item.temporal_status == "current"]
    historical = [item for item in observations if item.temporal_status == "historical"]

    status: EmployeeSizeStatus
    exact_count: int | None = None
    approximate_count: int | None = None
    lower_bound: int | None = None
    upper_bound: int | None = None
    band = EmployeeSizeBand.UNKNOWN
    temporal_status = "unknown"
    rationale: str
    evidence_handles: tuple[uuid.UUID, ...] = ()

    if invalid_handle:
        status = EmployeeSizeStatus.UNAVAILABLE
        rationale = "At least one proposed evidence handle was invalid; no value was settled."
    elif current:
        temporal_status = "current"
        evidence_handles = tuple(item.handle for item in current + historical)
        lower_values = [item.lower_bound for item in current if item.lower_bound is not None]
        upper_values = [item.upper_bound for item in current if item.upper_bound is not None]
        lower_bound = max(lower_values) if lower_values else None
        upper_bound = min(upper_values) if upper_values else None
        if lower_bound is not None and upper_bound is not None and lower_bound > upper_bound:
            status = EmployeeSizeStatus.CONFLICTED
            rationale = "Current Research observations have incompatible workforce values."
        else:
            band = _band_for_interval(lower_bound, upper_bound)
            if band is EmployeeSizeBand.UNKNOWN:
                status = EmployeeSizeStatus.UNRESOLVED
                rationale = (
                    "The current evidence is numeric but spans more than one normalized band."
                )
            else:
                status = EmployeeSizeStatus.SUPPORTED
                exact_values = {
                    item.exact_count for item in current if item.exact_count is not None
                }
                if len(exact_values) == 1 and all(item.exact_count is not None for item in current):
                    exact_count = next(iter(exact_values))
                approximate_values = {
                    item.approximate_count for item in current if item.approximate_count is not None
                }
                if exact_count is None and len(approximate_values) == 1 and len(current) == 1:
                    approximate_count = next(iter(approximate_values))
                rationale = "Current Company-bound Research evidence settles one normalized band."
    elif historical:
        status = EmployeeSizeStatus.STALE
        temporal_status = "historical"
        evidence_handles = tuple(item.handle for item in historical)
        rationale = "Only explicitly historical or older-than-policy evidence is available."
    elif ambiguous_employee_wording:
        status = EmployeeSizeStatus.UNRESOLVED
        temporal_status = "current"
        evidence_handles = tuple(dict.fromkeys(requested_handles))
        rationale = "Employee-related wording was present, but it did not support a numeric value."
    else:
        status = EmployeeSizeStatus.UNAVAILABLE
        rationale = "No valid Company-bound Employee Size evidence was supplied."

    confidence_values = [
        item.confidence
        for item in observations
        if item.handle in evidence_handles and item.confidence is not None
    ]
    confidence = min(confidence_values) if confidence_values else None
    source_wording = (
        observations[0].source_wording
        if len(observations) == 1
        else ("Multiple Research observations." if observations else None)
    )
    payload: dict[str, object] = {
        "schema_version": DERIVATION_VERSION,
        "status": status.value,
        "exact_count": exact_count,
        "approximate_count": approximate_count,
        "lower_bound": lower_bound,
        "upper_bound": upper_bound,
        "normalized_band": band.value,
        "source_wording": source_wording,
        "evidence_handles": [str(item) for item in evidence_handles],
        "observation_date": (
            max(
                item.observation_date
                for item in observations
                if item.handle in evidence_handles and item.observation_date is not None
            ).isoformat()
            if any(
                item.handle in evidence_handles and item.observation_date is not None
                for item in observations
            )
            else None
        ),
        "derived_at": derived_at.isoformat(),
        "derivation_version": DERIVATION_VERSION,
        "confidence": confidence,
        "temporal_status": temporal_status,
        "rationale": rationale,
        "observations": [item.payload() for item in observations],
        "conflicts": (
            [item.payload() for item in current] if status is EmployeeSizeStatus.CONFLICTED else []
        ),
    }
    evidence_inputs = (
        _derived_evidence(by_handle, evidence_handles)
        if status is not EmployeeSizeStatus.UNAVAILABLE
        else []
    )
    state = {
        EmployeeSizeStatus.SUPPORTED: InsightState.SUPPORTED,
        EmployeeSizeStatus.CONFLICTED: InsightState.CONFLICTING,
    }.get(status, InsightState.UNKNOWN)
    insight = insight_service.create_insight(
        session,
        claim=_claim(payload),
        kind=InsightKind.FACT,
        state=state,
        evidence=evidence_inputs,
        company_id=company_id,
        idempotency_key=f"insights-agent:{insights_job.id}:employee-size",
        actor=actor,
        insight_type=EMPLOYEE_SIZE_TYPE,
        structured_payload=payload,
        producer_job_id=insights_job.id,
        dossier_version_id=dossier.id,
        derivation_version=DERIVATION_VERSION,
    )
    # PostgreSQL transaction time is stable for the whole transaction.  An
    # application timestamp preserves deterministic append order when several
    # derivations are created before commit.
    insight.created_at = derived_at
    session.flush()
    return insight


def current_derivation(session: Session, *, company_id: uuid.UUID) -> Insight | None:
    """Latest append-only Employee Size derivation; older rows remain historical."""

    return session.scalars(
        select(Insight)
        .where(Insight.company_id == company_id, Insight.insight_type == EMPLOYEE_SIZE_TYPE)
        .order_by(Insight.created_at.desc(), Insight.id.desc())
    ).first()


def downstream_eligible(insight: Insight) -> tuple[bool, str]:
    payload = insight.structured_payload or {}
    if insight.insight_type != EMPLOYEE_SIZE_TYPE:
        return False, "This is not a structured Employee Size Insight."
    if payload.get("status") != EmployeeSizeStatus.SUPPORTED.value:
        return False, f"Employee Size is {payload.get('status') or 'unavailable'}."
    if payload.get("temporal_status") != "current":
        return False, "Employee Size is historical or stale."
    if payload.get("normalized_band") == EmployeeSizeBand.UNKNOWN.value:
        return False, "No normalized Employee Size band is settled."
    if not payload.get("evidence_handles"):
        return False, "No valid Research evidence handle supports this value."
    return True, "Settled, current and supported by valid Research evidence."
