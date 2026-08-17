"""``POST /extension/token`` finishes its own database work, off the event loop.

The staging outage of 2026-08-17 (release ``e7e8c76a``) was this endpoint. Two
token requests for the same link arrived within a second. The first flushed the
reuse-detection ``UPDATE extension_sessions SET revoked_at …`` and answered 400
**without** completing its transaction — the commit was left to the ``get_db``
teardown, which is scheduled on the event loop *after* the response is sent. The
second request then ran its own synchronous ``UPDATE`` on the same row directly
on the event-loop thread and blocked on the first request's row lock. The loop
never got back to the first request's teardown, so the lock was never released,
``uvicorn`` stopped accepting connections and nginx answered 504 for every route
until the process was killed. PostgreSQL showed one backend ``idle in
transaction`` and one ``active`` waiting on its ``transactionid`` — both owned by
the one web process.

Two invariants follow, and this file proves each of them as a real request over
the real hosted stack against the real database:

* **The handler completes its transaction on every exit path.** A refusal that
  revoked a link commits that revocation itself; a plain refusal, a success, and
  an exception each leave the session with no transaction open by the time the
  handler returns. Nothing about the security outcome may depend on teardown code
  that another request can starve.
* **The database work never runs on the event-loop thread.** A token request
  that is blocked on a row lock leaves the rest of the server answering, and
  concurrent refresh attempts against one link finish rather than deadlock.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from app.api.deps import get_db
from app.db.session import SessionLocal
from app.main import create_app
from app.models.extension_session import ExtensionSession
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from tests.test_extension_account_linking import (
    EXTENSION_ID,
    INSTALLATION_ID,
    ORIGIN,
    _AlwaysReadyProbe,
    _apply,
    _connect,
    _env,
    _token,
)

TOKEN_PATH = "/extension/token"

#: How long an unrelated request may take while a token request is blocked. On a
#: healthy server ``/healthz`` answers in milliseconds; a loop blocked in a
#: synchronous database call answers never.
RESPONSIVE_SECONDS = 5.0

#: How long the whole concurrent exercise may take before it counts as a hang.
COMPLETION_SECONDS = 30.0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def shared_loop_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The hosted application on **one** event loop for the whole test.

    ``TestClient`` outside a ``with`` block spins up a fresh loop per request,
    which would hide exactly the failure this file is about: the loop-blocking
    only matters when several requests share a loop, as they do under uvicorn.
    Entering the client keeps a single portal for its lifetime, so requests made
    from several threads here contend for one loop just as they do in staging.
    """

    _apply(monkeypatch, _env())
    app = create_app(readiness_probe=_AlwaysReadyProbe())
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
        yield client
    from app.core.config import get_settings

    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _refresh_payload(refresh_token: str) -> dict[str, Any]:
    return {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "extension_id": EXTENSION_ID,
        "installation_id": INSTALLATION_ID,
    }


def _exchange_payload(code: str, verifier: str) -> dict[str, Any]:
    return {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "extension_id": EXTENSION_ID,
        "installation_id": INSTALLATION_ID,
    }


def _link_rows_for(user_id: str) -> list[ExtensionSession]:
    """This test's own link rows, whatever an interrupted earlier run left behind."""

    with SessionLocal() as session:
        return list(
            session.scalars(
                select(ExtensionSession).where(ExtensionSession.user_id == uuid.UUID(user_id))
            ).all()
        )


def _backends_waiting_on_extension_sessions() -> int:
    """How many server backends are queued behind a lock on the link table."""

    with SessionLocal() as session:
        return int(
            session.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE wait_event_type = 'Lock' "
                    "AND query ILIKE '%extension_sessions%'"
                )
            ).scalar_one()
        )


def _backends_idle_in_transaction_on_extension_sessions() -> int:
    """The signature of the outage: a flushed change nobody ever completed."""

    with SessionLocal() as session:
        return int(
            session.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE state = 'idle in transaction' "
                    "AND query ILIKE '%extension_sessions%'"
                )
            ).scalar_one()
        )


def _wait_until(predicate: Any, *, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class _Observed:
    """What the token handler leaves behind, seen from the session's teardown."""

    def __init__(self) -> None:
        self.open_after_handler: list[tuple[str, bool]] = []


def _observing_get_db(observed: _Observed) -> Any:
    """``get_db`` with one difference: for token requests it commits *nothing*.

    The real dependency commits in its teardown, which is what masked the defect
    — a handler that returned with a flushed-but-uncommitted revocation looked
    correct in every test because the teardown quietly finished the job. Here
    the teardown records whether a transaction is still open when the handler
    is done and, for the token endpoint, closes without committing. If the
    revocation still persists, the handler committed it itself.
    """

    def dependency(request: Request) -> Iterator[Session]:
        session = SessionLocal()
        is_token = request.url.path == TOKEN_PATH
        try:
            yield session
            observed.open_after_handler.append((request.url.path, session.in_transaction()))
            if not is_token:
                session.commit()
        except Exception:
            observed.open_after_handler.append((request.url.path, session.in_transaction()))
            session.rollback()
            raise
        finally:
            session.close()

    return dependency


def _token_calls_left_open(observed: _Observed) -> list[bool]:
    return [open_ for path, open_ in observed.open_after_handler if path == TOKEN_PATH]


# ---------------------------------------------------------------------------
# A. Every exit path completes its transaction
# ---------------------------------------------------------------------------


def test_the_reuse_refusal_commits_the_revocation_before_it_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 400 that revokes a link must not leave the revocation for later.

    This is the request that held the row lock in staging. Its answer was
    already on the wire while its ``UPDATE`` was still uncommitted, waiting on a
    teardown that a second request on the same loop then starved.
    """

    _apply(monkeypatch, _env())
    app = create_app(readiness_probe=_AlwaysReadyProbe())
    observed = _Observed()
    app.dependency_overrides[get_db] = _observing_get_db(observed)
    client = TestClient(app, base_url=ORIGIN, follow_redirects=False)

    issued = _connect(client, email="commit-on-refusal@vmr.example")
    first_refresh = issued["refresh_token"]

    rotated = _token(client, _refresh_payload(first_refresh))
    assert rotated.status_code == 200
    replayed = _token(client, _refresh_payload(first_refresh))
    assert replayed.status_code == 400
    assert replayed.json() == {"error": "invalid_grant"}

    # The security outcome persisted without any help from teardown.
    rows = _link_rows_for(issued["user_id"])
    assert len(rows) == 1
    assert rows[0].revoked_at is not None
    assert rows[0].revoked_reason == "refresh_token_reuse"
    # And no token call — exchange, rotation or refusal — returned mid-transaction.
    assert _token_calls_left_open(observed) == [False, False, False]


def test_every_other_token_exit_path_returns_with_no_transaction_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refusals that read the table, refusals that never touch it, and success."""

    _apply(monkeypatch, _env())
    app = create_app(readiness_probe=_AlwaysReadyProbe())
    observed = _Observed()
    app.dependency_overrides[get_db] = _observing_get_db(observed)
    client = TestClient(app, base_url=ORIGIN, follow_redirects=False)

    issued = _connect(client, email="every-exit@vmr.example")
    calls_so_far = len(_token_calls_left_open(observed))

    # A refusal decided without any database work at all.
    assert _token(client, {"grant_type": "password"}).status_code == 400
    # A refusal decided after reading the table: a code nobody issued.
    assert _token(client, _exchange_payload("code-nobody-issued", "verifier")).status_code == 400
    # A refresh secret that names no row.
    assert _token(client, _refresh_payload("vmrr1.not-a-real-token")).status_code == 400
    # A live-link refusal: the wrong install for a real secret.
    wrong_install = dict(_refresh_payload(issued["refresh_token"]), installation_id="install-other")
    assert _token(client, wrong_install).status_code == 400
    # And success, which is committed by the handler as well.
    assert _token(client, _refresh_payload(issued["refresh_token"])).status_code == 200

    outcomes = _token_calls_left_open(observed)[calls_so_far:]
    assert len(outcomes) == 5
    assert outcomes == [False] * 5


def test_an_exception_after_the_rotation_is_flushed_rolls_it_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure between flush and answer must undo the flush, not park it."""

    from app.web import extension_link_routes

    _apply(monkeypatch, _env())
    app = create_app(readiness_probe=_AlwaysReadyProbe())
    observed = _Observed()
    app.dependency_overrides[get_db] = _observing_get_db(observed)
    client = TestClient(app, base_url=ORIGIN, follow_redirects=False, raise_server_exceptions=False)

    issued = _connect(client, email="exception-path@vmr.example")
    (before,) = _link_rows_for(issued["user_id"])
    real_rotate = extension_link_routes.rotate_refresh_token

    def rotate_then_fail(session: Session, **kwargs: Any) -> Any:
        real_rotate(session, **kwargs)
        raise RuntimeError("the response could not be built")

    monkeypatch.setattr(extension_link_routes, "rotate_refresh_token", rotate_then_fail)
    failed = _token(client, _refresh_payload(issued["refresh_token"]))
    assert failed.status_code == 500
    monkeypatch.setattr(extension_link_routes, "rotate_refresh_token", real_rotate)

    (after,) = _link_rows_for(issued["user_id"])
    assert after.refresh_token_hash == before.refresh_token_hash
    assert after.access_token_hash == before.access_token_hash
    assert after.revoked_at is None
    assert _token_calls_left_open(observed)[-1] is False
    # The rolled-back secret is therefore still the current one.
    assert _token(client, _refresh_payload(issued["refresh_token"])).status_code == 200


# ---------------------------------------------------------------------------
# B. The database work stays off the event loop
# ---------------------------------------------------------------------------


def test_a_token_request_blocked_on_a_row_lock_does_not_stall_the_server(
    shared_loop_client: TestClient,
) -> None:
    """The outage, replayed: one link row locked, one token request waiting on it.

    Under the defect the waiting request sat inside a synchronous ``UPDATE`` on
    the event-loop thread, so nothing else on the server could be answered.
    Fixed, the wait happens on a worker thread, ``/healthz`` keeps answering,
    and the token request completes as soon as the lock is released.
    """

    client = shared_loop_client
    issued = _connect(client, email="row-lock@vmr.example")
    (row,) = _link_rows_for(issued["user_id"])

    holder = SessionLocal()
    token_result: list[Any] = []
    probe_result: list[Any] = []
    try:
        # Another transaction owns the row, exactly as the first staging request
        # did while its teardown waited for a loop that never came back.
        holder.execute(
            select(ExtensionSession.id).where(ExtensionSession.id == row.id).with_for_update()
        ).all()

        refresher = threading.Thread(
            target=lambda: token_result.append(_token(client, _refresh_payload(issued["refresh_token"]))),
            daemon=True,
        )
        refresher.start()
        assert _wait_until(
            lambda: _backends_waiting_on_extension_sessions() >= 1, seconds=RESPONSIVE_SECONDS
        ), "the refresh request never reached the row lock"

        probe = threading.Thread(
            target=lambda: probe_result.append(client.get("/healthz")), daemon=True
        )
        probe.start()
        probe.join(RESPONSIVE_SECONDS)
        assert not probe.is_alive(), (
            "an unrelated request hung while a token request waited on a row lock: "
            "the token endpoint is doing its database work on the event loop"
        )
        assert probe_result[0].status_code == 200
    finally:
        holder.rollback()
        holder.close()

    refresher.join(COMPLETION_SECONDS)
    assert not refresher.is_alive(), "the token request never completed after the lock was released"
    assert token_result[0].status_code == 200
    assert _backends_idle_in_transaction_on_extension_sessions() == 0


def test_concurrent_refresh_attempts_against_one_link_all_complete(
    shared_loop_client: TestClient,
) -> None:
    """Rapid repeated presentation of one refresh secret finishes, every time.

    Whatever the interleaving, each attempt is answered in the endpoint's one
    voice, none of them waits forever, and no transaction is left open behind
    them. The reuse rule still stands: once the row is revoked it stays revoked.
    """

    client = shared_loop_client
    issued = _connect(client, email="concurrent-refresh@vmr.example")
    payload = _refresh_payload(issued["refresh_token"])
    barrier = threading.Barrier(5)
    results: list[Any] = []
    lock = threading.Lock()

    def attempt() -> None:
        barrier.wait(timeout=RESPONSIVE_SECONDS)
        response = _token(client, payload)
        with lock:
            results.append(response)

    workers = [threading.Thread(target=attempt, daemon=True) for _ in range(5)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(COMPLETION_SECONDS)
    assert all(not worker.is_alive() for worker in workers), (
        "concurrent refresh attempts did not all complete: the token endpoint deadlocked"
    )
    assert len(results) == 5
    for response in results:
        assert response.status_code in {200, 400}, response.text
        if response.status_code == 400:
            assert response.json() == {"error": "invalid_grant"}
    assert _backends_idle_in_transaction_on_extension_sessions() == 0

    (row,) = _link_rows_for(issued["user_id"])
    if row.revoked_at is not None:
        # A detected reuse revoked the whole family, and that must persist.
        assert row.revoked_reason == "refresh_token_reuse"
        assert _token(client, payload).status_code == 400
