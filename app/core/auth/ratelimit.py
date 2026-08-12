"""Rate limiting for the password login form.

The shape of the control matters more than the numbers, so the reasoning is
here rather than in a comment on a magic value.

**Throttling, never locking.** A lockout that an attacker can trigger is a
denial-of-service tool aimed at a named colleague: knowing somebody's address
would be enough to keep them out of the application indefinitely. So there is no
account lock, no administrator unlock, and no state that outlives the window. A
bucket simply refuses further attempts until its window rolls over, and a person
who waits is let back in with no intervention.

**Two buckets, and the second is the one that matters.** Counting failures per
address alone would let one attacker work through a list of colleagues at full
speed, one attempt each. Counting per client address alone would let a
distributed attacker grind a single account. Both are counted, and either can
refuse.

**Successful sign-in clears the address bucket.** Otherwise somebody who mistypes
their password four times and then gets it right is still throttled, which
punishes exactly the person the control is not aimed at. The client bucket is not
cleared, because one success does not make the rest of that client's traffic
trustworthy.

**In-process, deliberately.** A shared store (Redis, a table) would survive a
restart and be consistent across workers, and neither property is worth a new
piece of infrastructure for a Beta running a single uvicorn process behind nginx.
The limitation is written down rather than hidden: with multiple workers each
holds its own counters, so the effective limit is the configured limit multiplied
by the worker count. The 15-character minimum, not this bucket, is what makes
guessing hopeless; this is here to make it *slow* and *loud* rather than to be the
last line.

The window is a fixed window rather than a sliding one. A fixed window admits up
to twice the limit across a boundary, which for a login form is a difference
nobody can exploit into anything, and it costs one integer per bucket instead of
a timestamp list per bucket.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

#: Failed attempts allowed for one email address within one window.
DEFAULT_EMAIL_ATTEMPT_LIMIT = 5
#: Failed attempts allowed from one client address within one window. Higher than
#: the per-address limit so that several people behind one office NAT do not
#: throttle each other with ordinary typos.
DEFAULT_CLIENT_ATTEMPT_LIMIT = 20
#: Length of one window, in seconds.
DEFAULT_WINDOW_SECONDS = 300

#: A hard ceiling on how many distinct buckets are tracked. Without it, an
#: attacker who varies the address on every attempt would grow the dictionary
#: without bound and turn a defence into a memory leak. When the ceiling is
#: reached the oldest windows are dropped first; dropping a bucket is the safe
#: direction because the worst case is that an attacker regains a few attempts.
MAX_TRACKED_BUCKETS = 4096


@dataclass
class _Bucket:
    window_started_at: int = 0
    failures: int = 0
    last_seen_at: int = field(default=0)


class LoginRateLimiter:
    """Fixed-window failure counters for the password login form.

    Every method takes ``now`` explicitly. Time is an argument rather than a
    global so a test can prove the window rolls over without sleeping through it,
    and so two calls within one request cannot disagree about the current second.
    """

    def __init__(
        self,
        *,
        email_limit: int = DEFAULT_EMAIL_ATTEMPT_LIMIT,
        client_limit: int = DEFAULT_CLIENT_ATTEMPT_LIMIT,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
        max_buckets: int = MAX_TRACKED_BUCKETS,
    ) -> None:
        self._email_limit = email_limit
        self._client_limit = client_limit
        self._window = window_seconds
        self._max_buckets = max_buckets
        self._buckets: dict[str, _Bucket] = {}
        # uvicorn serves requests on a thread pool for sync endpoints, so the
        # dictionary is guarded. The critical section is a handful of integer
        # operations; contention is not a consideration at this scale.
        self._lock = threading.Lock()

    # --- queries -------------------------------------------------------------

    def is_blocked(self, *, email: str, client: str | None, now: int) -> bool:
        """Whether this attempt should be refused before any password is checked.

        ``client`` may be ``None`` when the caller's address could not be
        resolved; the per-client bucket is then skipped rather than shared. See
        :func:`client_fingerprint` for why a shared bucket would be a denial of
        service rather than a defence.
        """

        with self._lock:
            if self._over_limit(f"e:{email}", self._email_limit, now):
                return True
            if client is None:
                return False
            return self._over_limit(f"c:{client}", self._client_limit, now)

    def retry_after_seconds(self, *, email: str, client: str | None, now: int) -> int:
        """Whole seconds until the most restrictive relevant window rolls over."""

        keys = [f"e:{email}"] + ([f"c:{client}"] if client is not None else [])
        with self._lock:
            remaining = 0
            for key in keys:
                bucket = self._buckets.get(key)
                if bucket is None:
                    continue
                left = bucket.window_started_at + self._window - now
                remaining = max(remaining, left)
            return max(0, remaining)

    # --- updates -------------------------------------------------------------

    def record_failure(self, *, email: str, client: str | None, now: int) -> None:
        """Count one failed attempt against both buckets."""

        with self._lock:
            self._increment(f"e:{email}", now)
            if client is not None:
                self._increment(f"c:{client}", now)
            self._evict_if_needed()

    def record_success(self, *, email: str, now: int) -> None:
        """Forget this address's failures. The client's are deliberately kept."""

        with self._lock:
            self._buckets.pop(f"e:{email}", None)

    def reset(self) -> None:
        """Drop every counter. For tests and for a deliberate operator action."""

        with self._lock:
            self._buckets.clear()

    # --- internals -----------------------------------------------------------

    def _over_limit(self, key: str, limit: int, now: int) -> bool:
        bucket = self._buckets.get(key)
        if bucket is None:
            return False
        if now - bucket.window_started_at >= self._window:
            return False
        return bucket.failures >= limit

    def _increment(self, key: str, now: int) -> None:
        bucket = self._buckets.get(key)
        if bucket is None or now - bucket.window_started_at >= self._window:
            bucket = _Bucket(window_started_at=now)
            self._buckets[key] = bucket
        bucket.failures += 1
        bucket.last_seen_at = now

    def _evict_if_needed(self) -> None:
        if len(self._buckets) <= self._max_buckets:
            return
        # Oldest first. Sorting a bounded dictionary on the rare occasion it
        # overflows is cheaper and far easier to reason about than maintaining an
        # LRU structure for a defence that sees a handful of entries in practice.
        ordered = sorted(self._buckets.items(), key=lambda item: item[1].last_seen_at)
        for key, _ in ordered[: len(self._buckets) - self._max_buckets]:
            self._buckets.pop(key, None)


def client_fingerprint(request_state: Mapping[str, Any]) -> str | None:
    """The client identity a bucket is keyed on, or ``None`` for "do not count".

    Read from ``state["client_ip"]``, which the production hardening middleware
    published after applying this project's one forwarded-header trust rule. It
    is deliberately *not* re-derived here: a login form inventing a second, laxer
    rule is how an attacker ends up choosing their own bucket key, and an attacker
    who can choose their bucket has no rate limit at all.

    ``None`` — no resolvable caller address — means the per-client bucket is
    **skipped for this request**, and that is the important decision in this
    function. The obvious alternative, a shared ``"unknown"`` bucket, is worse
    than useless behind a reverse proxy: uvicorn runs with ``--no-proxy-headers``,
    so if the forwarded client cannot be resolved then it cannot be resolved for
    *anybody*, and every request in the deployment lands in one bucket. An
    anonymous caller could then spend that bucket's whole allowance in a few
    seconds and lock every colleague out of password sign-in — converting a
    throttle into the site-wide denial of service this module's docstring
    promises it is not. Skipping leaves the per-address bucket, which is the
    limit that actually protects an account, doing its job alone.
    """

    raw = request_state.get("client_ip")
    if not isinstance(raw, str):
        return None
    candidate = raw.strip().lower()
    return candidate or None
