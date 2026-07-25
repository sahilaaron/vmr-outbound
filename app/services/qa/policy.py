"""Versioned employment QA policy over profile snapshots (DAT-012F).

The legacy QA bot's validation logic, remodelled as a deterministic, versioned
BACKEND policy — not extension logic and not direct field mutation. The policy
separates four things the legacy program blended together:

1. **Factual observations** — what the snapshot actually shows (current roles,
   timelines, employment types, connection count, open-to-work).
2. **Deterministic classification** — does the expected company/title match the
   observed current employment (legacy ``company_name_identifier`` /
   ``_norm_title`` behaviour, reimplemented deterministically).
3. **QA recommendation** — one primary :class:`~app.models.enums.QAOutcome`
   plus reason codes, review signals, a human-readable explanation and a
   recommended next action.
4. **Hard eligibility gates** — NOT here. A QA evaluation can never unsuppress,
   mark an email valid, approve a draft, alter sending limits, or schedule
   outreach; it writes an append-only evaluation record and nothing else.

Deliberately revised legacy assumptions (now configurable review signals, not
automatic exclusions): open-to-work, fewer than N connections, hybrid/remote
workplace, multiple current roles, and tenure over N years each *flag for
operator review* instead of auto-disqualifying.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from sqlalchemy.orm import Session

from app.models.contact import Contact
from app.models.enums import QAOutcome
from app.models.linkedin_profile import LinkedInProfileSnapshot
from app.models.qa_evaluation import ContactQAEvaluation
from app.services.audit import record_audit_event

POLICY_NAME = "profile-employment-qa"
POLICY_VERSION = f"{POLICY_NAME}/1.0.0"
QA_AUDIT_ACTION = "contact.qa_evaluated"
_QA_ACTOR = "qa-policy"

# Recommended next actions (advisory strings, stable for the workbench).
ACTION_PROCEED = "proceed"
ACTION_OPERATOR_REVIEW = "operator_review"
ACTION_RECAPTURE = "recapture_profile"


# --- Configuration -----------------------------------------------------------


@dataclass(frozen=True)
class QAPolicyConfig:
    """Tunable review thresholds. Every signal is review-only, never a gate."""

    low_connection_threshold: int = 15
    tenure_review_years: int = 15
    review_on_open_to_work: bool = True
    review_on_non_full_time: bool = True
    review_on_multiple_current_roles: bool = True
    review_on_missing_tenure: bool = True
    review_workplace_types: tuple[str, ...] = ("Hybrid", "Remote", "Global")

    def as_dict(self) -> dict[str, Any]:
        return {
            "low_connection_threshold": self.low_connection_threshold,
            "tenure_review_years": self.tenure_review_years,
            "review_on_open_to_work": self.review_on_open_to_work,
            "review_on_non_full_time": self.review_on_non_full_time,
            "review_on_multiple_current_roles": self.review_on_multiple_current_roles,
            "review_on_missing_tenure": self.review_on_missing_tenure,
            "review_workplace_types": list(self.review_workplace_types),
        }


DEFAULT_CONFIG = QAPolicyConfig()


# --- Deterministic matchers (legacy-informed, reimplemented) ------------------

_TITLE_PUNCT = re.compile(r"[,.\-/]")
_TITLE_STOP = re.compile(r"\b(of|the|a|an|and|&)\b", re.IGNORECASE)
_TITLE_SUBS = (
    (re.compile(r"\bsr\b\.?"), "senior"),
    (re.compile(r"\bassoc\b\.?"), "associate"),
    (re.compile(r"\bvp\b\.?"), "vice president"),
    (re.compile(r"\bevp\b\.?"), "executive vice president"),
    (re.compile(r"\bdir\b\.?"), "director"),
    (re.compile(r"\bops\b"), "operations"),
)


def normalize_title(value: str | None) -> str | None:
    """Normalize a job title for comparison (legacy ``_norm_title`` behaviour).

    Lower-cases, expands a few unambiguous abbreviations, strips punctuation,
    and drops stopwords/conjunctions so "Manager of Talent Acquisition" equals
    "Manager, Talent Acquisition". Deterministic; no translation, no guessing.
    """

    if value is None:
        return None
    t = value.strip().lower().replace("&", " and ")
    for pattern, repl in _TITLE_SUBS:
        t = pattern.sub(repl, t)
    t = _TITLE_PUNCT.sub(" ", t)
    t = _TITLE_STOP.sub("", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t or None


def titles_match(a: str | None, b: str | None) -> bool:
    na, nb = normalize_title(a), normalize_title(b)
    return na is not None and na == nb


def _token_overlap(a: str, b: str) -> float:
    tokens = [t for t in a.casefold().split() if t]
    if not tokens:
        return 0.0
    other = b.casefold()
    return sum(1 for t in tokens if t in other) / len(tokens)


def company_names_match(expected: str | None, observed: str | None) -> bool:
    """Legacy ``company_name_identifier``: >50% token containment, either way."""

    if not expected or not observed:
        return False
    return _token_overlap(expected, observed) > 0.5 or _token_overlap(observed, expected) > 0.5


_YEARS_RE = re.compile(r"(\d+)\s*(?:yr|yrs|year|years)\b", re.IGNORECASE)


def tenure_years_from_duration(duration_text: str | None) -> int | None:
    """Deterministically read whole years from "16 yrs 3 mos"; None otherwise."""

    if not duration_text:
        return None
    match = _YEARS_RE.search(duration_text)
    return int(match.group(1)) if match else None


# --- Result ------------------------------------------------------------------


@dataclass
class QAResult:
    """The full, explainable result of one policy evaluation."""

    outcome: QAOutcome
    reason_codes: list[str] = dc_field(default_factory=list)
    signals: list[dict[str, Any]] = dc_field(default_factory=list)
    explanation: str = ""
    recommended_action: str = ACTION_OPERATOR_REVIEW
    observations: dict[str, Any] = dc_field(default_factory=dict)


# --- Pure evaluation ---------------------------------------------------------


def _signal(
    signals: list[dict[str, Any]],
    reason_codes: list[str],
    *,
    code: str,
    triggered: bool,
    detail: Any = None,
    threshold: Any = None,
) -> None:
    signals.append({"code": code, "triggered": triggered, "detail": detail, "threshold": threshold})
    if triggered:
        reason_codes.append(code)


def evaluate(
    *,
    profile_fields: dict[str, Any],
    experiences: list[dict[str, Any]],
    experience_excluded: bool,
    expected_company: str | None,
    expected_title: str | None,
    config: QAPolicyConfig = DEFAULT_CONFIG,
) -> QAResult:
    """Evaluate one snapshot against a contact expectation. Pure and deterministic.

    Precedence: structural problems (missing/unrecognized experience) beat
    employment classification, which beats review signals; review signals beat
    a clean ``live_contact`` only in the primary outcome — every triggered
    signal is always listed in ``reason_codes``/``signals`` regardless.
    """

    reasons: list[str] = []
    signals: list[dict[str, Any]] = []

    current = [e for e in experiences if e.get("is_current") is True]
    current_company_matches = [
        e for e in current if company_names_match(expected_company, e.get("company_name"))
    ]
    any_company_match = any(
        company_names_match(expected_company, e.get("company_name")) for e in experiences
    )
    title_match_current = any(titles_match(expected_title, e.get("job_title")) for e in current)

    observations: dict[str, Any] = {
        "experience_count": len(experiences),
        "current_role_count": len(current),
        "current_companies": [e.get("company_name") for e in current],
        "current_titles": [e.get("job_title") for e in current],
        "connection_count": profile_fields.get("connection_count"),
        "open_to_work": profile_fields.get("open_to_work"),
        "expected_company_matched_current": bool(current_company_matches),
        "expected_company_matched_anywhere": any_company_match,
        "expected_title_matched_current": title_match_current,
    }

    # --- Structural checks ----------------------------------------------------
    if experience_excluded:
        return QAResult(
            outcome=QAOutcome.INSUFFICIENT_EVIDENCE,
            reason_codes=["experience_section_excluded"],
            signals=signals,
            explanation=(
                "The operator excluded the experience section from this capture, so "
                "employment cannot be evaluated from this snapshot."
            ),
            recommended_action=ACTION_RECAPTURE,
            observations=observations,
        )
    if not experiences:
        return QAResult(
            outcome=QAOutcome.EXPERIENCE_MISSING,
            reason_codes=["no_experience_captured"],
            signals=signals,
            explanation=(
                "No experience entries were captured from the profile. The section may "
                "be absent or was not loaded when the page was read."
            ),
            recommended_action=ACTION_RECAPTURE,
            observations=observations,
        )
    unrecognized = [
        e
        for e in experiences
        if any(w.get("code") == "unrecognized_layout" for w in e.get("warnings") or [])
    ]
    if unrecognized and not current:
        return QAResult(
            outcome=QAOutcome.EXPERIENCE_UNRECOGNIZED,
            reason_codes=["experience_layout_unrecognized"],
            signals=signals,
            explanation=(
                "The experience section was found but its layout could not be parsed "
                "into usable entries. Nothing was classified."
            ),
            recommended_action=ACTION_RECAPTURE,
            observations=observations,
        )

    # --- Review signals (configurable; review-only, never exclusions) ---------
    _signal(
        signals,
        reasons,
        code="multiple_current_roles",
        triggered=config.review_on_multiple_current_roles and len(current) > 1,
        detail=len(current),
        threshold=1,
    )
    top_current = (
        current_company_matches[0] if current_company_matches else (current[0] if current else None)
    )
    tenure_years = (
        tenure_years_from_duration(top_current.get("duration_text")) if top_current else None
    )
    _signal(
        signals,
        reasons,
        code="tenure_exceeds_review_threshold",
        triggered=tenure_years is not None and tenure_years > config.tenure_review_years,
        detail=tenure_years,
        threshold=config.tenure_review_years,
    )
    missing_tenure = bool(
        top_current
        and not top_current.get("timeline_text")
        and not top_current.get("duration_text")
    )
    _signal(
        signals,
        reasons,
        code="tenure_unavailable",
        triggered=config.review_on_missing_tenure and missing_tenure,
        detail=None,
        threshold=None,
    )
    employment_type = top_current.get("employment_type") if top_current else None
    _signal(
        signals,
        reasons,
        code="non_full_time_role",
        triggered=(
            config.review_on_non_full_time
            and employment_type is not None
            and employment_type != "Full-time"
        ),
        detail=employment_type,
        threshold="Full-time",
    )
    _signal(
        signals,
        reasons,
        code="open_to_work",
        triggered=bool(config.review_on_open_to_work and profile_fields.get("open_to_work")),
        detail=profile_fields.get("open_to_work"),
        threshold=None,
    )
    connections = profile_fields.get("connection_count")
    _signal(
        signals,
        reasons,
        code="low_connection_count",
        triggered=(isinstance(connections, int) and connections < config.low_connection_threshold),
        detail=connections,
        threshold=config.low_connection_threshold,
    )
    workplace = top_current.get("workplace_type") if top_current else None
    _signal(
        signals,
        reasons,
        code="workplace_type_review",
        triggered=workplace is not None and workplace in config.review_workplace_types,
        detail=workplace,
        threshold=list(config.review_workplace_types),
    )

    # --- Employment classification ---------------------------------------------
    if not expected_company:
        outcome = QAOutcome.COMPANY_UNRESOLVED
        explanation = "No expected company was available to compare against the profile."
        reasons.insert(0, "no_expected_company")
    elif current_company_matches:
        if len(current_company_matches) > 1:
            outcome = QAOutcome.MULTIPLE_CURRENT_ROLES
            explanation = (
                f"More than one current role matches “{expected_company}”; the profile's "
                "current employment is ambiguous."
            )
            reasons.insert(0, "expected_company_matched_multiple_current")
        elif titles_match(expected_title, current_company_matches[0].get("job_title")):
            outcome = QAOutcome.LIVE_CONTACT
            explanation = (
                f"The profile shows a current role at “{expected_company}” with the expected title."
            )
        else:
            outcome = QAOutcome.TITLE_CHANGED
            explanation = (
                f"The profile shows a current role at “{expected_company}”, but the title "
                f"({current_company_matches[0].get('job_title') or 'unknown'!s}) no longer "
                f"matches the expected “{expected_title or 'unknown'}”."
            )
            reasons.insert(0, "title_differs_at_expected_company")
    elif any_company_match:
        outcome = QAOutcome.LEFT_COMPANY
        explanation = (
            f"“{expected_company}” appears only in past experience; the profile's "
            "current employment is elsewhere."
        )
        reasons.insert(0, "expected_company_only_in_past")
    elif title_match_current:
        outcome = QAOutcome.COMPANY_UNRESOLVED
        explanation = (
            "The expected title matches a current role, but the expected company could "
            "not be resolved against any observed employer."
        )
        reasons.insert(0, "expected_company_unmatched_title_matched")
    elif not current:
        outcome = QAOutcome.COMPANY_UNRESOLVED
        explanation = (
            "No current employment is visible on the profile and the expected company "
            "was not found in its history."
        )
        reasons.insert(0, "no_current_employment_observed")
    else:
        outcome = QAOutcome.LEFT_COMPANY
        explanation = (
            "The profile's current employment does not match the expected company "
            f"“{expected_company}”."
        )
        reasons.insert(0, "current_employment_elsewhere")

    # --- Signal-driven review outcomes (only demote a clean live_contact) ------
    if outcome == QAOutcome.LIVE_CONTACT:
        triggered = {s["code"] for s in signals if s["triggered"]}
        if "multiple_current_roles" in triggered:
            outcome = QAOutcome.MULTIPLE_CURRENT_ROLES
        elif "tenure_exceeds_review_threshold" in triggered or "tenure_unavailable" in triggered:
            outcome = QAOutcome.TENURE_REVIEW
        elif "non_full_time_role" in triggered:
            outcome = QAOutcome.NON_FULL_TIME_REVIEW
        elif "open_to_work" in triggered:
            outcome = QAOutcome.OPEN_TO_WORK_REVIEW
        elif "low_connection_count" in triggered:
            outcome = QAOutcome.LOW_CONNECTIONS_REVIEW
        elif "workplace_type_review" in triggered:
            outcome = QAOutcome.NEEDS_REVIEW
        if outcome != QAOutcome.LIVE_CONTACT:
            explanation += (
                " Employment matches, but review signals were triggered: "
                + ", ".join(sorted(triggered))
                + "."
            )

    recommended = ACTION_PROCEED if outcome == QAOutcome.LIVE_CONTACT else ACTION_OPERATOR_REVIEW
    return QAResult(
        outcome=outcome,
        reason_codes=reasons,
        signals=signals,
        explanation=explanation,
        recommended_action=recommended,
        observations=observations,
    )


# --- Persistence -------------------------------------------------------------


def evaluate_contact_snapshot(
    session: Session,
    *,
    snapshot: LinkedInProfileSnapshot,
    contact: Contact,
    config: QAPolicyConfig = DEFAULT_CONFIG,
    actor: str = _QA_ACTOR,
) -> ContactQAEvaluation:
    """Run the policy for one (contact, snapshot) pair and store the evaluation.

    Append-only: each run adds a new evaluation row carrying the policy name and
    version, the evaluated expectation, evidence references, outcome, reason
    codes, signals (with thresholds in force), explanation, and recommended next
    action. It mutates NOTHING else — no contact field, no workflow state, no
    suppression, no verification, no approval, no schedule.
    """

    payload = snapshot.payload or {}
    extraction = payload.get("extraction") or {}
    excluded = "experience" in (extraction.get("excluded_sections") or [])
    result = evaluate(
        profile_fields=snapshot.profile_fields or {},
        experiences=payload.get("experiences") or [],
        experience_excluded=excluded,
        expected_company=contact.company_name,
        expected_title=contact.title,
        config=config,
    )

    evaluation = ContactQAEvaluation(
        policy_name=POLICY_NAME,
        policy_version=POLICY_VERSION,
        contact_id=contact.id,
        snapshot_id=snapshot.id,
        contact_expectation={
            "expected_company": contact.company_name,
            "expected_title": contact.title,
            "config": config.as_dict(),
        },
        evidence_refs={
            "snapshot_id": str(snapshot.id),
            "normalized_profile_url": snapshot.normalized_profile_url,
            "captured_at": snapshot.captured_at.isoformat() if snapshot.captured_at else None,
            "observations": result.observations,
        },
        outcome=result.outcome,
        reason_codes=result.reason_codes,
        signals=result.signals,
        explanation=result.explanation,
        recommended_action=result.recommended_action,
    )
    session.add(evaluation)
    session.flush()

    record_audit_event(
        session,
        actor=actor,
        action=QA_AUDIT_ACTION,
        entity_type="contact",
        entity_id=str(contact.id),
        new_state=result.outcome.value,
        reason=f"{POLICY_VERSION}: {result.outcome.value}",
        context={
            "evaluation_id": str(evaluation.id),
            "snapshot_id": str(snapshot.id),
            "policy_version": POLICY_VERSION,
            "outcome": result.outcome.value,
            "reason_codes": result.reason_codes,
            "recommended_action": result.recommended_action,
        },
    )
    return evaluation
