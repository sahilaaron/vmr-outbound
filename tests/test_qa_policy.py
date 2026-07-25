"""Versioned employment QA policy tests (DAT-012F).

Representative scenarios derived from the legacy QA bot's behaviour, with the
deliberately revised assumptions proven: open-to-work, low connections,
hybrid/remote, multiple current roles, and long tenure are configurable REVIEW
signals — never automatic exclusions — and the policy mutates nothing.
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path

from app.models.contact import Contact
from app.models.enums import QAOutcome
from app.models.linkedin_profile import LinkedInProfileSnapshot
from app.models.qa_evaluation import ContactQAEvaluation
from app.services.imports.linkedin_profile_intake import stage_profile_snapshot
from app.services.qa.policy import (
    ACTION_OPERATOR_REVIEW,
    ACTION_PROCEED,
    POLICY_NAME,
    POLICY_VERSION,
    QAPolicyConfig,
    evaluate,
    evaluate_contact_snapshot,
    normalize_title,
    titles_match,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_PAYLOAD = json.loads(
    (
        REPO_ROOT
        / "extensions"
        / "salesnav-capture"
        / "docs"
        / "fixtures"
        / "profile.payload.example.json"
    ).read_text("utf-8")
)


def _exp(
    *,
    company: str | None = "Meridian Works",
    title: str | None = "Director of Operations",
    current: bool = True,
    duration: str | None = "5 yrs 7 mos",
    timeline: str | None = "Jan 2021 - Present",
    employment_type: str | None = "Full-time",
    workplace: str | None = None,
    warnings: list | None = None,
) -> dict:
    return {
        "position_index": 1,
        "layout": "basic",
        "company_name": company,
        "job_title": title,
        "is_current": current,
        "duration_text": duration,
        "timeline_text": timeline,
        "employment_type": employment_type,
        "workplace_type": workplace,
        "warnings": warnings or [],
    }


def _run(
    experiences: list[dict],
    *,
    profile: dict | None = None,
    company: str | None = "Meridian Works",
    title: str | None = "Director of Operations",
    config: QAPolicyConfig | None = None,
    excluded: bool = False,
):
    return evaluate(
        profile_fields=profile or {"connection_count": 500, "open_to_work": False},
        experiences=experiences,
        experience_excluded=excluded,
        expected_company=company,
        expected_title=title,
        config=config or QAPolicyConfig(),
    )


# --- Title normalization (legacy behaviour) -----------------------------------


def test_title_normalization_matches_legacy_equivalences() -> None:
    assert titles_match("Manager of Talent Acquisition", "Manager, Talent Acquisition")
    assert titles_match("Sr. Director - Ops", "Senior Director, Operations")
    assert titles_match("VP Sales", "Vice President Sales")
    assert not titles_match("Director of Operations", "Operations Analyst")
    assert normalize_title(None) is None


# --- Employment classification --------------------------------------------------


def test_live_contact_when_company_and_title_match() -> None:
    r = _run([_exp()])
    assert r.outcome == QAOutcome.LIVE_CONTACT
    assert r.recommended_action == ACTION_PROCEED
    assert r.reason_codes == []


def test_title_changed_at_same_company() -> None:
    r = _run([_exp(title="VP of Operations")])
    assert r.outcome == QAOutcome.TITLE_CHANGED
    assert "title_differs_at_expected_company" in r.reason_codes
    assert r.recommended_action == ACTION_OPERATOR_REVIEW


def test_left_company_when_expected_company_only_in_past() -> None:
    r = _run(
        [
            _exp(company="Somewhere Else Ltd", title="Head of Ops"),
            _exp(
                company="Meridian Works",
                current=False,
                timeline="Mar 2016 - Dec 2020",
                duration="4 yrs 10 mos",
            ),
        ]
    )
    assert r.outcome == QAOutcome.LEFT_COMPANY
    assert "expected_company_only_in_past" in r.reason_codes


def test_company_unresolved_when_title_matches_but_company_does_not() -> None:
    r = _run([_exp(company="Totally Unrelated GmbH")])
    assert r.outcome == QAOutcome.COMPANY_UNRESOLVED


def test_experience_missing_and_unrecognized() -> None:
    assert _run([]).outcome == QAOutcome.EXPERIENCE_MISSING
    r = _run(
        [
            _exp(
                company=None,
                title=None,
                current=None,  # type: ignore[arg-type]
                warnings=[{"code": "unrecognized_layout"}],
            )
        ]
    )
    assert r.outcome == QAOutcome.EXPERIENCE_UNRECOGNIZED


def test_operator_excluded_experience_is_insufficient_evidence() -> None:
    r = _run([_exp()], excluded=True)
    assert r.outcome == QAOutcome.INSUFFICIENT_EVIDENCE


# --- Legacy auto-DQ rules are now configurable review signals -------------------


def test_open_to_work_is_a_review_signal_not_an_exclusion() -> None:
    r = _run([_exp()], profile={"connection_count": 500, "open_to_work": True})
    assert r.outcome == QAOutcome.OPEN_TO_WORK_REVIEW
    assert r.recommended_action == ACTION_OPERATOR_REVIEW  # review, never a hard gate
    # Switching the signal off restores live_contact under the same facts.
    off = _run(
        [_exp()],
        profile={"connection_count": 500, "open_to_work": True},
        config=QAPolicyConfig(review_on_open_to_work=False),
    )
    assert off.outcome == QAOutcome.LIVE_CONTACT


def test_low_connections_threshold_is_configurable() -> None:
    r = _run([_exp()], profile={"connection_count": 9, "open_to_work": False})
    assert r.outcome == QAOutcome.LOW_CONNECTIONS_REVIEW
    relaxed = _run(
        [_exp()],
        profile={"connection_count": 9, "open_to_work": False},
        config=QAPolicyConfig(low_connection_threshold=5),
    )
    assert relaxed.outcome == QAOutcome.LIVE_CONTACT


def test_long_tenure_and_missing_tenure_are_review_not_disqualification() -> None:
    long_r = _run([_exp(duration="16 yrs 2 mos")])
    assert long_r.outcome == QAOutcome.TENURE_REVIEW
    missing = _run([_exp(duration=None, timeline=None)])
    assert missing.outcome == QAOutcome.TENURE_REVIEW
    assert "tenure_unavailable" in missing.reason_codes


def test_non_full_time_and_workplace_are_review_signals() -> None:
    part = _run([_exp(employment_type="Part-time")])
    assert part.outcome == QAOutcome.NON_FULL_TIME_REVIEW
    hybrid = _run([_exp(workplace="Hybrid")])
    assert hybrid.outcome == QAOutcome.NEEDS_REVIEW
    onsite = _run([_exp(workplace="On-site")])
    assert onsite.outcome == QAOutcome.LIVE_CONTACT


def test_multiple_current_roles_flags_review() -> None:
    r = _run(
        [
            _exp(),
            _exp(company="Side Gig LLC", title="Advisor", employment_type="Part-time"),
        ]
    )
    assert r.outcome == QAOutcome.MULTIPLE_CURRENT_ROLES
    assert "multiple_current_roles" in r.reason_codes


def test_signals_are_recorded_even_when_employment_fails() -> None:
    r = _run(
        [_exp(company="Somewhere Else Ltd", workplace="Remote")],
        profile={"connection_count": 3, "open_to_work": True},
    )
    # Primary outcome is the employment classification…
    assert r.outcome in (QAOutcome.LEFT_COMPANY, QAOutcome.COMPANY_UNRESOLVED)
    # …but every triggered signal is still on the record.
    triggered = {s["code"] for s in r.signals if s["triggered"]}
    assert {"open_to_work", "low_connection_count", "workplace_type_review"} <= triggered


# --- Persistence + no-mutation guarantee ----------------------------------------


def test_evaluation_is_stored_versioned_and_mutates_nothing(db_session: Session) -> None:
    contact = Contact(
        first_name="Morgan",
        last_name="Vale",
        company_name="Meridian Works",
        company_domain="example.test",
        title="Director of Operations",
        linkedin_url="https://www.linkedin.com/in/morgan-vale",
        natural_key="morgan|vale|example.test",
    )
    db_session.add(contact)
    db_session.flush()

    payload = copy.deepcopy(EXAMPLE_PAYLOAD)
    payload["client_capture_id"] = str(uuid.uuid4())
    payload["campaign_id"] = None
    staged = stage_profile_snapshot(
        db_session, payload=payload, operator_base_url="http://127.0.0.1:8000"
    )
    snapshot = db_session.get(LinkedInProfileSnapshot, uuid.UUID(staged.snapshot_id))
    assert snapshot is not None

    before_title = contact.title
    before_company = contact.company_name

    evaluation = evaluate_contact_snapshot(db_session, snapshot=snapshot, contact=contact)

    stored = db_session.scalars(select(ContactQAEvaluation)).all()
    assert len(stored) == 1
    assert stored[0].id == evaluation.id
    assert stored[0].policy_name == POLICY_NAME
    assert stored[0].policy_version == POLICY_VERSION
    # The example capture's current role is Hybrid, so the default config
    # flags it for review (employment itself matches).
    assert stored[0].outcome == QAOutcome.NEEDS_REVIEW
    assert "workplace_type_review" in (stored[0].reason_codes or [])
    assert stored[0].evidence_refs is not None
    assert stored[0].evidence_refs["snapshot_id"] == str(snapshot.id)
    assert stored[0].explanation
    assert stored[0].contact_expectation is not None
    assert stored[0].contact_expectation["expected_company"] == "Meridian Works"

    # The QA policy mutated NOTHING beyond its own evaluation row.
    assert contact.title == before_title
    assert contact.company_name == before_company
    assert snapshot.outcome.value == "stored"  # evaluation alone never reconciles
