"""VER-004 / UI: derivation of the four visible states from evidence + jobs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.email_evidence import ExactEmailVerification
from app.models.enums import (
    PRECISE_TO_VISUAL,
    EmailPreciseStatus,
    EmailVerificationResult,
    EmailVisualStatus,
)
from app.services.verification import queue as jobs
from app.services.verification.status import derive_status_for_email
from sqlalchemy.orm import Session

POLICY = "ver-1"


def _seed(
    session: Session,
    email: str,
    result: EmailVerificationResult,
    *,
    age_days: int = 0,
    is_role: bool = False,
) -> None:
    session.add(
        ExactEmailVerification(
            email=email,
            result=result,
            provider="millionverifier",
            policy_version=POLICY,
            is_role=is_role,
            checked_at=datetime.now(UTC) - timedelta(days=age_days),
        )
    )
    session.flush()


def test_precise_to_visual_is_total() -> None:
    for precise in EmailPreciseStatus:
        assert precise in PRECISE_TO_VISUAL


def test_unverified_is_pending(db_session: Session) -> None:
    st = derive_status_for_email(db_session, "nobody@acme.com")
    assert st.precise == EmailPreciseStatus.UNVERIFIED
    assert st.visual == EmailVisualStatus.PENDING


def test_fresh_valid_is_successful(db_session: Session) -> None:
    _seed(db_session, "ok@acme.com", EmailVerificationResult.VALID)
    st = derive_status_for_email(db_session, "ok@acme.com")
    assert st.visual == EmailVisualStatus.SUCCESSFUL


def test_fresh_valid_role_is_warning(db_session: Session) -> None:
    _seed(db_session, "info@acme.com", EmailVerificationResult.VALID, is_role=True)
    st = derive_status_for_email(db_session, "info@acme.com")
    assert st.precise == EmailPreciseStatus.ROLE_BASED
    assert st.visual == EmailVisualStatus.WARNING


def test_invalid_is_failure(db_session: Session) -> None:
    _seed(db_session, "bad@acme.com", EmailVerificationResult.INVALID)
    assert derive_status_for_email(db_session, "bad@acme.com").visual == EmailVisualStatus.FAILURE


def test_catch_all_unknown_disposable_are_warning(db_session: Session) -> None:
    for email, res in [
        ("c@acme.com", EmailVerificationResult.CATCH_ALL),
        ("u@acme.com", EmailVerificationResult.UNKNOWN),
        ("d@acme.com", EmailVerificationResult.DISPOSABLE),
    ]:
        _seed(db_session, email, res)
        assert derive_status_for_email(db_session, email).visual == EmailVisualStatus.WARNING


def test_stale_valid_is_warning(db_session: Session) -> None:
    _seed(db_session, "old@acme.com", EmailVerificationResult.VALID, age_days=400)
    st = derive_status_for_email(db_session, "old@acme.com")
    assert st.precise == EmailPreciseStatus.STALE_EVIDENCE
    assert st.visual == EmailVisualStatus.WARNING


def test_queued_job_is_pending(db_session: Session) -> None:
    jobs.enqueue_verification(db_session, email="q@acme.com", policy_version=POLICY, max_attempts=4)
    st = derive_status_for_email(db_session, "q@acme.com")
    assert st.precise == EmailPreciseStatus.QUEUED
    assert st.visual == EmailVisualStatus.PENDING


def test_in_progress_job_is_checking(db_session: Session) -> None:
    jobs.enqueue_verification(db_session, email="p@acme.com", policy_version=POLICY, max_attempts=4)
    jobs.claim_next_job(db_session, worker_id="w", lease_seconds=60)
    st = derive_status_for_email(db_session, "p@acme.com")
    assert st.precise == EmailPreciseStatus.CHECKING


def test_stale_with_active_job_is_recheck_scheduled(db_session: Session) -> None:
    _seed(db_session, "r@acme.com", EmailVerificationResult.VALID, age_days=400)
    jobs.enqueue_verification(db_session, email="r@acme.com", policy_version=POLICY, max_attempts=4)
    st = derive_status_for_email(db_session, "r@acme.com")
    assert st.precise == EmailPreciseStatus.STALE_RECHECK_SCHEDULED
    assert st.visual == EmailVisualStatus.PENDING


def test_conflicting_fresh_evidence_is_warning(db_session: Session) -> None:
    _seed(db_session, "conf@acme.com", EmailVerificationResult.VALID)
    _seed(db_session, "conf@acme.com", EmailVerificationResult.INVALID)
    st = derive_status_for_email(db_session, "conf@acme.com")
    assert st.precise == EmailPreciseStatus.CONFLICTING_EVIDENCE
    assert st.visual == EmailVisualStatus.WARNING


def test_insufficient_credits_surfaced_from_job(db_session: Session) -> None:
    job, _ = jobs.enqueue_verification(
        db_session, email="nc@acme.com", policy_version=POLICY, max_attempts=1
    )
    jobs.claim_next_job(db_session, worker_id="w", lease_seconds=60)
    jobs.mark_failed(
        db_session,
        job,
        reason="no credits",
        outcome_status=EmailPreciseStatus.INSUFFICIENT_CREDITS.value,
    )
    st = derive_status_for_email(db_session, "nc@acme.com")
    assert st.precise == EmailPreciseStatus.INSUFFICIENT_CREDITS
    assert st.visual == EmailVisualStatus.WARNING
