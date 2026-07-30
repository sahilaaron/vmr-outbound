"""Automatic company-domain resolution at intake.

The capture endpoint used to stage a person and stop, leaving an operator to open
each one and press "resolve automatically" — a button whose decision the policy
had already made. These tests protect the automation and, more importantly, the
isolation around it: a provider failure on one person must not lose the
submission that saved everyone else.
"""

from __future__ import annotations

import pytest
from app.services.captures import intake as captures_intake


class _Deadline:
    def check(self) -> None:
        return None


def test_resolution_is_skipped_while_the_switches_are_off(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off must mean untouched, not "attempted and failed"."""

    from app.core.config import get_settings

    monkeypatch.delenv("FEATURES__AUTOMATIC_COMPANY_DOMAIN_RESOLUTION", raising=False)
    get_settings.cache_clear()
    called: list[object] = []

    def _boom(*args: object, **kwargs: object) -> None:
        called.append(args)
        raise AssertionError("resolution must not run while the switch is off")

    from app.services.resolution import service as resolution_service

    monkeypatch.setattr(resolution_service, "resolve", _boom)
    resolved = captures_intake._auto_resolve_captures(
        db_session, snapshots=[object()], actor="test", deadline=_Deadline()
    )
    assert resolved == 0
    assert called == []
    get_settings.cache_clear()


def test_one_failing_capture_does_not_abandon_the_others(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider or policy failure on one person is contained to that person.

    This is the property that makes running resolution inside the intake request
    acceptable at all: the submission has already saved every capture, and a
    resolution attempt is an improvement on top. If it throws, the capture stays
    staged and resolvable by hand — exactly where it would have been before.
    """

    from app.core.config import get_settings

    monkeypatch.setenv("FEATURES__AUTOMATIC_COMPANY_DOMAIN_RESOLUTION", "true")
    monkeypatch.setenv("FEATURES__CONTACT_CAPTURE_PROMOTION", "true")
    monkeypatch.setenv("FEATURES__SALESNAV_DOMAIN_ENRICHMENT", "true")
    monkeypatch.setenv("LOGO_DEV_API_KEY", "test-key-not-used")
    get_settings.cache_clear()

    seen: list[object] = []

    class _Outcome:
        auto_promoted = True

    def _sometimes(session, *, snapshot, access, actor, force):
        seen.append(snapshot)
        if snapshot == "second":
            raise RuntimeError("provider exploded")
        return _Outcome()

    from app.services.resolution import service as resolution_service

    monkeypatch.setattr(resolution_service, "resolve", _sometimes)
    resolved = captures_intake._auto_resolve_captures(
        db_session,
        snapshots=["first", "second", "third"],
        actor="test",
        deadline=_Deadline(),
    )
    assert seen == ["first", "second", "third"], "every capture must still be attempted"
    assert resolved == 2, "the two that succeeded are counted; the failure is not fatal"
    get_settings.cache_clear()


def test_resolution_is_never_forced_over_an_operator_decision(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator correction is a decision; recalculating over it would discard it."""

    from app.core.config import get_settings

    monkeypatch.setenv("FEATURES__AUTOMATIC_COMPANY_DOMAIN_RESOLUTION", "true")
    monkeypatch.setenv("FEATURES__CONTACT_CAPTURE_PROMOTION", "true")
    monkeypatch.setenv("FEATURES__SALESNAV_DOMAIN_ENRICHMENT", "true")
    monkeypatch.setenv("LOGO_DEV_API_KEY", "test-key-not-used")
    get_settings.cache_clear()
    forces: list[bool] = []

    class _Outcome:
        auto_promoted = False

    def _record(session, *, snapshot, access, actor, force):
        forces.append(force)
        return _Outcome()

    from app.services.resolution import service as resolution_service

    monkeypatch.setattr(resolution_service, "resolve", _record)
    captures_intake._auto_resolve_captures(
        db_session, snapshots=["one"], actor="test", deadline=_Deadline()
    )
    assert forces == [False]
    get_settings.cache_clear()


def test_no_attempt_is_made_without_a_usable_provider(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A keyless install must stage the capture, not record a non-decision.

    Without a provider the policy can only conclude "the lookup was not run",
    which is the absence of a decision rather than a decision. Worse, a recorded
    decision is not recalculated without an explicit force — so persisting that
    non-decision would stop the capture from ever resolving automatically later,
    including after a key is finally configured.
    """

    from app.core.config import get_settings

    monkeypatch.setenv("FEATURES__AUTOMATIC_COMPANY_DOMAIN_RESOLUTION", "true")
    monkeypatch.setenv("FEATURES__CONTACT_CAPTURE_PROMOTION", "true")
    monkeypatch.delenv("LOGO_DEV_API_KEY", raising=False)
    get_settings.cache_clear()

    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("resolution must not be attempted without a provider")

    from app.services.resolution import service as resolution_service

    monkeypatch.setattr(resolution_service, "resolve", _boom)
    assert (
        captures_intake._auto_resolve_captures(
            db_session, snapshots=[object()], actor="test", deadline=_Deadline()
        )
        == 0
    )
    get_settings.cache_clear()
