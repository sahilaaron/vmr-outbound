"""Resolving the captures an intake request deliberately left alone.

Intake resolves what it can inside a hard share of its own request budget, because
a hundred-capture submission would otherwise spend a hundred provider lookups
inside one HTTP request. This is the other half of that decision: the worker
finishes the rest, where time is not bounded by a request.
"""

from __future__ import annotations

import uuid

import pytest
from app.core.config import get_settings
from app.models.company_domain_resolution import CompanyDomainResolution
from app.models.contact import Contact
from app.models.enums import DomainResolutionKind, DomainResolutionState
from app.models.linkedin_profile import LinkedInProfileSnapshot
from app.services.resolution import pending
from sqlalchemy.orm import Session


@pytest.fixture()
def live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURES__AUTOMATIC_COMPANY_DOMAIN_RESOLUTION", "true")
    monkeypatch.setenv("FEATURES__CONTACT_CAPTURE_PROMOTION", "true")
    monkeypatch.setenv("FEATURES__SALESNAV_DOMAIN_ENRICHMENT", "true")
    monkeypatch.setenv("LOGO_DEV_API_KEY", "test-key-not-used")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _capture(db: Session, **kwargs: object) -> LinkedInProfileSnapshot:
    snapshot = LinkedInProfileSnapshot(
        client_capture_id=f"cap-{uuid.uuid4()}",
        content_hash=str(uuid.uuid4()),
        schema_version="linkedin-contact-capture/2.1.0",
        source="test",
        extraction_status="ok",
        payload={},
        profile_fields={"full_name": "Ada Lovelace"},
        **kwargs,  # type: ignore[arg-type]
    )
    db.add(snapshot)
    db.flush()
    return snapshot


def test_a_capture_with_no_decision_is_picked_up(db_session: Session, live: None) -> None:
    snapshot = _capture(db_session)
    assert snapshot.id in pending.pending_capture_ids(db_session)


def test_a_capture_that_already_became_a_contact_is_left_alone(
    db_session: Session, live: None
) -> None:
    contact = Contact(
        first_name="Ada",
        last_name="Lovelace",
        company_name="Kiln",
        company_domain="kiln.example",
        natural_key="ada|lovelace|kiln.example",
    )
    db_session.add(contact)
    db_session.flush()
    snapshot = _capture(db_session, matched_contact_id=contact.id)
    assert snapshot.id not in pending.pending_capture_ids(db_session)


def test_a_capture_the_policy_already_decided_is_not_re_decided(
    db_session: Session, live: None
) -> None:
    """A recorded UNRESOLVED means the policy looked and could not conclude.

    Re-running it without new evidence reaches the same answer; re-running it with
    ``force`` would overwrite an operator's correction. Those captures belong to the
    operator, not to a background pass.
    """

    snapshot = _capture(db_session)
    db_session.add(
        CompanyDomainResolution(
            capture_id=snapshot.id,
            decision_number=1,
            is_current=True,
            state=DomainResolutionState.UNRESOLVED,
            decision_kind=DomainResolutionKind.AUTOMATIC,
            policy_version="test",
            reasons=["provider_returned_no_candidates"],
        )
    )
    db_session.flush()
    assert snapshot.id not in pending.pending_capture_ids(db_session)


def test_nothing_is_attempted_without_a_usable_provider(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same rule as intake: recording "the lookup was not run" would permanently
    stop the capture from resolving automatically later."""

    monkeypatch.setenv("FEATURES__AUTOMATIC_COMPANY_DOMAIN_RESOLUTION", "true")
    monkeypatch.setenv("FEATURES__CONTACT_CAPTURE_PROMOTION", "true")
    monkeypatch.delenv("LOGO_DEV_API_KEY", raising=False)
    get_settings.cache_clear()
    _capture(db_session)

    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not attempt resolution without a provider")

    from app.services.resolution import service as resolution_service

    monkeypatch.setattr(resolution_service, "resolve", _boom)
    assert pending.resolve_pending(db_session).did_work is False
    get_settings.cache_clear()


def test_one_failure_does_not_stop_the_pass(
    db_session: Session, live: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _capture(db_session)
    second = _capture(db_session)
    seen: list[uuid.UUID] = []

    class _Outcome:
        auto_promoted = True
        provider_call_made = True

    def _sometimes(session, *, snapshot, access, actor, force):
        seen.append(snapshot.id)
        if snapshot.id == first.id:
            raise RuntimeError("provider exploded")
        return _Outcome()

    from app.services.resolution import service as resolution_service

    monkeypatch.setattr(resolution_service, "resolve", _sometimes)
    result = pending.resolve_pending(db_session, limit=10)

    assert set(seen) == {first.id, second.id}
    assert result.failed == 1
    assert result.promoted == 1


def test_the_limit_is_honoured(
    db_session: Session, live: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each capture may cost a provider lookup, so a pass is bounded by design."""

    for _ in range(5):
        _capture(db_session)

    calls: list[object] = []

    class _Outcome:
        auto_promoted = False
        provider_call_made = True

    def _count(session, *, snapshot, access, actor, force):
        calls.append(snapshot.id)
        return _Outcome()

    from app.services.resolution import service as resolution_service

    monkeypatch.setattr(resolution_service, "resolve", _count)
    result = pending.resolve_pending(db_session, limit=2)
    assert len(calls) == 2
    assert result.considered == 2
