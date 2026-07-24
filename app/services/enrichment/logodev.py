"""Client for the official logo.dev "Search Brands by Name" API (DAT-010).

This is a thin, deterministic adapter over one documented endpoint. It exists so
the operator can look up candidate companies/domains for a Sales Navigator
company that carries no ``company_domain``. It NEVER:

* chooses a domain — it only returns candidates for the operator to confirm;
* invents a domain, or ranks-then-accepts the first result;
* logs, serializes, echoes, or stores the API key.

The key is used only to build an ``Authorization`` header for the transport call
and is never placed in the URL, the result, or any raised error. The transport is
injectable so tests exercise every branch without a network, and no live call can
happen until a real key is supplied by the operator.

Every provider condition maps to a truthful :class:`~app.models.enums.
EnrichmentLookupStatus`: ``OK`` (candidates present), ``NO_MATCH`` (searched, none
found), ``RATE_LIMITED`` (429), ``API_UNAVAILABLE`` (network error, timeout, 5xx,
or auth/other unexpected status), and ``MALFORMED`` (a 200 whose body is not the
documented array of brand objects).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlencode

from app.models.enums import EnrichmentLookupStatus
from app.services.imports import normalization as norm


@dataclass(frozen=True)
class Candidate:
    """One brand candidate returned by logo.dev, normalized for display.

    ``domain`` is a validated, lower-cased hostname. ``name`` is the provider's
    display name when present. No logo URL, score, or raw provider field is kept:
    the operator confirms by domain, and a leaner record cannot leak anything.
    """

    domain: str
    name: str | None = None


@dataclass(frozen=True)
class LookupResult:
    """The outcome of one logo.dev lookup. Never contains the API key."""

    status: EnrichmentLookupStatus
    candidates: tuple[Candidate, ...] = ()


@dataclass(frozen=True)
class RawResponse:
    """A transport-level HTTP response: status code and text body only."""

    status_code: int
    body: str


class TransportError(Exception):
    """A network-level failure (connection refused, DNS, timeout, TLS).

    Raised by a transport instead of returning a response. The message is
    provider/reason text only and never includes credentials.
    """


# A transport receives (url, headers, timeout_seconds) and returns a
# RawResponse, or raises TransportError for a network-level failure. The default
# uses urllib; tests inject a stub so no network is ever touched.
Transport = Callable[[str, Mapping[str, str], float], RawResponse]

_RETRYABLE_STATUS = 429
_SERVER_ERROR_FLOOR = 500


def _urllib_transport(url: str, headers: Mapping[str, str], timeout: float) -> RawResponse:
    """Default transport over urllib. Never logs; never leaks the auth header."""

    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = response.read().decode("utf-8", errors="replace")
            return RawResponse(status_code=response.status, body=body)
    except urllib.error.HTTPError as exc:  # a status >= 400 with a body
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover - body may be unreadable
            body = ""
        return RawResponse(status_code=exc.code, body=body)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # Reason text only (host/errno) — the auth header is not part of it.
        raise TransportError(f"logo.dev request failed: {exc.__class__.__name__}") from exc


def _parse_candidates(body: str, *, max_candidates: int) -> LookupResult:
    """Parse a 200 body into candidates, or report NO_MATCH / MALFORMED.

    The documented shape is a JSON array of brand objects, each with a
    ``domain`` (and usually a ``name``). An empty array is a truthful NO_MATCH. A
    body that is not an array, or an array whose items are not objects, is
    MALFORMED. Individual items missing a usable domain are skipped; if the array
    was non-empty but yields no usable domain, that is MALFORMED (the provider
    returned something, but nothing we can act on) rather than a silent NO_MATCH.
    """

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return LookupResult(EnrichmentLookupStatus.MALFORMED)

    # Accept either a bare array or a documented ``{"data": [...]}`` envelope.
    if isinstance(payload, dict):
        payload = payload.get("data", payload.get("results"))
    if not isinstance(payload, list):
        return LookupResult(EnrichmentLookupStatus.MALFORMED)
    if not payload:
        return LookupResult(EnrichmentLookupStatus.NO_MATCH)

    candidates: list[Candidate] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            return LookupResult(EnrichmentLookupStatus.MALFORMED)
        domain = norm.normalize_domain(_as_str(item.get("domain")))
        if domain is None or not norm.is_valid_hostname(domain) or domain in seen:
            continue
        seen.add(domain)
        candidates.append(
            Candidate(domain=domain, name=norm.collapse_whitespace(_as_str(item.get("name"))))
        )
        if len(candidates) >= max_candidates:
            break

    if not candidates:
        # The provider returned a non-empty array we could not turn into a single
        # usable domain — treat as malformed, not as "searched and found nothing".
        return LookupResult(EnrichmentLookupStatus.MALFORMED)
    return LookupResult(EnrichmentLookupStatus.OK, tuple(candidates))


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def search_brands(
    query: str,
    *,
    api_key: str,
    search_url: str = "https://api.logo.dev/search",
    timeout: float = 10.0,
    max_candidates: int = 10,
    transport: Transport | None = None,
) -> LookupResult:
    """Look up candidate brands/domains for *query* through logo.dev.

    Returns a :class:`LookupResult`; never raises for a provider condition (each
    maps to a truthful status). ``api_key`` is required and is used only to build
    the ``Authorization`` header — it is never placed in the URL, the result, or a
    raised error. A blank query is treated as NO_MATCH (nothing to search).
    """

    if not api_key or not api_key.strip():
        # A programming error at the call site: the service guards on the key
        # before calling and reports "not configured" as its own state.
        raise ValueError("search_brands requires a non-empty api_key")

    cleaned = query.strip()
    if not cleaned:
        return LookupResult(EnrichmentLookupStatus.NO_MATCH)

    call = transport or _urllib_transport
    url = f"{search_url}?{urlencode({'q': cleaned})}"
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}

    try:
        response = call(url, headers, timeout)
    except TransportError:
        return LookupResult(EnrichmentLookupStatus.API_UNAVAILABLE)

    if response.status_code == _RETRYABLE_STATUS:
        return LookupResult(EnrichmentLookupStatus.RATE_LIMITED)
    if response.status_code >= _SERVER_ERROR_FLOOR:
        return LookupResult(EnrichmentLookupStatus.API_UNAVAILABLE)
    if response.status_code != 200:
        # Auth failures (401/403) and any other unexpected status are reported as
        # unavailable without echoing the response body or the key.
        return LookupResult(EnrichmentLookupStatus.API_UNAVAILABLE)

    return _parse_candidates(response.body, max_candidates=max_candidates)
