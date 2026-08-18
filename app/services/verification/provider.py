"""MillionVerifier Single API adapter (VER-001).

Contract (official Single API v3):

    GET https://api.millionverifier.com/api/v3?api=<key>&email=<addr>&timeout=<n>

Response JSON carries ``result`` / ``resultcode`` (1=ok, 2=catch_all, 3=unknown,
4=error, 5=disposable, 6=invalid), plus ``subresult``, ``quality``, ``free``,
``role``, ``didyoumean``, ``credits``, ``executiontime``, ``error`` and
``livemode``. Only ok/invalid/disposable are billed.

Two interchangeable implementations sit behind :class:`VerificationProvider`:

* :class:`SimulatedMillionVerifier` — deterministic, network-free. It honours the
  documented test keys (``API_KEY_FOR_OK`` …) and, for any other key, derives a
  stable outcome from the address so every documented outcome can be demonstrated
  and tested without a live call or a real key. This is the default everywhere,
  including the whole automated suite.
* :class:`HttpMillionVerifier` — the real client. It talks to the documented
  endpoint through an injectable :class:`Transport` seam, so request construction,
  response parsing, and — critically — API-key redaction are unit-tested without
  touching the network. It is selected only when a real (non-test) key is
  configured and live mode is explicitly requested.

The API key never appears in logs, exceptions, stored payloads, or the redacted
request URL exposed for diagnostics.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

# Documented test keys that must always route to the simulator, never the network.
TEST_KEYS: dict[str, str] = {
    "API_KEY_FOR_OK": "ok",
    "API_KEY_FOR_CATCH_ALL": "catch_all",
    "API_KEY_FOR_INVALID": "invalid",
    "API_KEY_FOR_DISPOSABLE": "disposable",
    "API_KEY_FOR_UNKOWN": "unknown",  # documented (sic) spelling
    "API_KEY_FOR_UNKNOWN": "unknown",
    "API_KEY_FOR_UNVERIFIED": "unknown",
    "API_KEY_FOR_TEST": "random",
    "API_KEY_FOR_ERROR_NO_EMAIL": "error:no_email",
    "API_KEY_FOR_ERROR_NO_APIKEY": "error:no_apikey",
    "API_KEY_FOR_ERROR_INVALID_APIKEY": "error:invalid_api_key",
    "API_KEY_FOR_ERROR_INSUFFICIENT_CREDITS": "error:insufficient_credits",
    "API_KEY_FOR_ERROR_IP_ADDRESS_BLOCKED": "error:ip_address_blocked",
    "API_KEY_FOR_ERROR_INTERNAL_ERROR": "error:internal_error",
}

# Evidence-provenance labels stored on each ExactEmailVerification row so a
# simulated outcome is never displayed as though an external provider verified it
# (VER-007). The live client records the neutral vendor label; the simulator
# records an explicit simulated label. These are the values the operator sees.
LIVE_PROVIDER_LABEL = "millionverifier"
SIMULATOR_PROVIDER_LABEL = "millionverifier-simulator"
DEBOUNCE_LIVE_PROVIDER_LABEL = "debounce"
DEBOUNCE_SIMULATOR_PROVIDER_LABEL = "debounce-simulator"

# Every label that means "no external provider actually checked this address".
# Membership is what decides whether an outcome may advance a Campaign Contact,
# so a new simulator that is not listed here would silently become acceptable.
SIMULATOR_PROVIDER_LABELS: frozenset[str] = frozenset(
    {SIMULATOR_PROVIDER_LABEL, DEBOUNCE_SIMULATOR_PROVIDER_LABEL}
)

# Static request headers for the live Single API client. A descriptive User-Agent
# is good API citizenship (and some providers reject header-less requests); Accept
# declares that we parse JSON. The version is kept in sync with pyproject.toml.
_CLIENT_VERSION = "0.0.1"
REQUEST_HEADERS: dict[str, str] = {
    "Accept": "application/json",
    "User-Agent": f"vmr-outbound/{_CLIENT_VERSION}",
}

_RESULTCODE = {"ok": 1, "catch_all": 2, "unknown": 3, "error": 4, "disposable": 5, "invalid": 6}
_ROLE_LOCALPARTS = frozenset(
    {"info", "sales", "support", "admin", "contact", "hello", "team", "office", "help"}
)
_DISPOSABLE_DOMAINS = frozenset({"mailinator.com", "guerrillamail.com", "tempmail.com"})


def _as_optional_bool(value: object) -> bool | None:
    """Read one documented boolean-ish provider field, or fail closed.

    DeBounce's schema types ``success``, ``role`` and ``free_email`` as integers
    or booleans while its published examples render them as strings, so all
    three forms have to be accepted. What must *not* happen is a truthiness
    shortcut: ``2``, ``"yes please"`` and ``[]`` are not documented values, and
    guessing at them is how an unreadable reply turns into a confident verdict.
    Anything outside the documented set returns None and the caller treats the
    response as unusable.
    """

    if isinstance(value, bool):
        return value
    # Checked after bool, because bool is a subclass of int.
    if isinstance(value, int):
        if value in (0, 1):
            return bool(value)
        return None
    if isinstance(value, str):
        normalized = value.casefold().strip()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return None


class ProviderTransientError(Exception):
    """A transport-level failure (no HTTP response): connection error or timeout.

    Distinct from an application-level ``error`` field in a 200 response — this
    means we never reached a verdict and the job may retry.

    ``condition`` names *which* transport failure this was, using the same
    vocabulary the provider registry already declares in ``safe_retry_classes``
    ("transport", "rate_limit", "provider_5xx"). The fallback policy needs to
    tell a rate limit apart from an unreachable host; without this it would have
    to parse the message, and a message is not a contract.
    """

    def __init__(self, message: str, *, condition: str = "transport") -> None:
        super().__init__(message)
        self.condition = condition


@dataclass(frozen=True)
class ProviderResponse:
    """Normalized view of one Single API response (secrets already excluded)."""

    email: str
    result: str | None
    resultcode: int | None
    subresult: str | None = None
    quality: str | None = None
    free: bool | None = None
    role: bool | None = None
    didyoumean: str | None = None
    credits: int | None = None
    execution_time: int | None = None
    error: str | None = None
    livemode: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


class VerificationProvider(Protocol):
    """The replaceable verification provider contract."""

    name: str
    # True for the deterministic network-free simulator, False for the real HTTP
    # client. Callers persist this provenance so a simulated result is never
    # presented as an external MillionVerifier verification (VER-007).
    simulated: bool

    def verify(self, email: str) -> ProviderResponse:
        """Verify one exact address or raise :class:`ProviderTransientError`."""


def evidence_provider_label(provider: VerificationProvider) -> str:
    """The provenance label to store for evidence produced by *provider*.

    Falls back to the live label for structural test doubles that do not declare
    ``simulated``; the two production providers both set it explicitly.
    """

    name = getattr(provider, "name", "millionverifier")
    if name == "debounce":
        return (
            DEBOUNCE_SIMULATOR_PROVIDER_LABEL
            if getattr(provider, "simulated", False)
            else DEBOUNCE_LIVE_PROVIDER_LABEL
        )
    return (
        SIMULATOR_PROVIDER_LABEL if getattr(provider, "simulated", False) else LIVE_PROVIDER_LABEL
    )


REDACTION_PLACEHOLDER = "***REDACTED***"


def redact_secret(text: str, secret: str | None) -> str:
    """Replace *secret* wherever it appears in *text*.

    The one definition of redaction in the codebase, so every layer that stores
    or displays provider text strips the same thing the same way.
    """

    if secret:
        return text.replace(secret, REDACTION_PLACEHOLDER)
    return text


def redact_payload(value: dict[str, Any], secret: str | None) -> dict[str, Any]:
    """Redact a credential anywhere in a JSON-compatible provider payload."""

    if not secret:
        return value
    return cast(dict[str, Any], json.loads(redact_secret(json.dumps(value), secret)))


def redact_for_provider(provider: object, text: str, *, fallback_secret: str | None = None) -> str:
    """Redact *text* using the credential *provider* actually authenticates with.

    Every live client knows its own key and nothing else does. Redacting a
    DeBounce failure with the MillionVerifier key — which is what a single
    hard-coded secret would do — protects nothing. Clients expose ``redact``;
    test doubles that do not fall back to the caller-supplied secret.
    """

    redactor = getattr(provider, "redact", None)
    if callable(redactor):
        return str(redactor(text))
    return redact_secret(text, fallback_secret)


# Historical private alias, kept so the live client's call sites read unchanged.
_redact = redact_secret


# --- Simulator ---------------------------------------------------------------


class SimulatedMillionVerifier:
    """Deterministic, network-free provider used by default and in all tests."""

    name = "millionverifier"
    simulated = True

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key

    def verify(self, email: str) -> ProviderResponse:
        outcome = self._decide(email)
        return self._respond(email, outcome)

    def _decide(self, email: str) -> str:
        key = (self._api_key or "").strip()
        mapped = TEST_KEYS.get(key)
        if mapped == "random":
            digest = int(hashlib.sha256(email.encode()).hexdigest(), 16)
            return ["ok", "invalid", "catch_all", "unknown", "disposable"][digest % 5]
        if mapped:
            return mapped
        # No test key: derive a stable, explainable outcome from the address so
        # every documented outcome is demonstrable with synthetic records.
        local, _, domain = email.partition("@")
        local = local.lower()
        domain = domain.lower()
        if "timeout" in local:
            return "raise:timeout"
        if "nocredits" in local:
            return "error:insufficient_credits"
        if "provider-error" in local or "providererror" in local:
            return "error:internal_error"
        if "servererror" in local or local.endswith("error"):
            return "error"
        if domain in _DISPOSABLE_DOMAINS or "disposable" in local:
            return "disposable"
        if "catchall" in local or domain.startswith("catchall.") or "catch-all" in domain:
            return "catch_all"
        if "unknown" in local:
            return "unknown"
        if "invalid" in local or "bounce" in local or "nomailbox" in local:
            return "invalid"
        return "ok"

    def _respond(self, email: str, outcome: str) -> ProviderResponse:
        if outcome == "raise:timeout":
            raise ProviderTransientError("simulated connection timeout")
        local, _, domain = email.partition("@")
        role = local.lower() in _ROLE_LOCALPARTS
        free = domain.lower() in {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com"}
        if outcome.startswith("error:"):
            err = outcome.split(":", 1)[1]
            return ProviderResponse(
                email=email,
                result=None,
                resultcode=None,
                error=err,
                credits=0 if err == "insufficient_credits" else None,
                raw={"error": err, "email": email},
            )
        if outcome == "error":
            return ProviderResponse(
                email=email,
                result="error",
                resultcode=_RESULTCODE["error"],
                subresult="temporary_failure",
                credits=100,
                raw={"result": "error", "resultcode": 4, "email": email},
            )
        quality = {"ok": "good", "catch_all": "risky", "unknown": "risky"}.get(outcome, "bad")
        return ProviderResponse(
            email=email,
            result=outcome,
            resultcode=_RESULTCODE[outcome],
            subresult="role_account" if role else "none",
            quality=quality,
            free=free,
            role=role,
            credits=100,
            execution_time=1,
            livemode=False,
            raw={
                "result": outcome,
                "resultcode": _RESULTCODE[outcome],
                "role": role,
                "free": free,
                "email": email,
            },
        )


# --- Live HTTP client --------------------------------------------------------


def http_transient_condition(status: int) -> str:
    """Name the transport condition for an HTTP status we could not use.

    Uses the registry's own ``safe_retry_classes`` vocabulary so the fallback
    policy and the retry policy agree on what a 429 is without a second table.
    """

    if status == 429:
        return "rate_limit"
    if status >= 500:
        return "provider_5xx"
    return "transport"


class Transport(Protocol):
    """Minimal HTTP GET seam so the live client is testable without a network."""

    def get(self, url: str, timeout: float) -> str: ...


class UrllibTransport:
    """Default transport backed by the standard library (no third-party dep)."""

    def build_request(self, url: str) -> urllib.request.Request:
        """Build the GET request with our standard headers (pure; no network).

        Split out from :meth:`get` so the header contract is unit-testable offline.
        """

        return urllib.request.Request(url, headers=dict(REQUEST_HEADERS), method="GET")

    def get(self, url: str, timeout: float) -> str:  # pragma: no cover - network
        req = self.build_request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return str(resp.read().decode("utf-8"))


class HttpMillionVerifier:
    """Real Single API client. Used only for the deliberate live smoke test."""

    name = "millionverifier"
    simulated = False

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.millionverifier.com/api/v3",
        timeout_seconds: int = 20,
        transport: Transport | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._timeout = timeout_seconds
        self._transport = transport or UrllibTransport()

    def _build_url(self, email: str) -> str:
        query = urllib.parse.urlencode(
            {"api": self._api_key, "email": email, "timeout": self._timeout}
        )
        return f"{self._base_url}?{query}"

    def redact(self, text: str) -> str:
        """Strip this client's credential from any text before it is stored."""

        return _redact(text, self._api_key)

    def redacted_url(self, email: str) -> str:
        """The request URL with the key redacted, safe to log or show."""

        return self.redact(self._build_url(email))

    def verify(self, email: str) -> ProviderResponse:
        url = self._build_url(email)
        try:
            body = self._transport.get(url, timeout=float(self._timeout) + 1.0)
        except ProviderTransientError:
            raise
        except urllib.error.HTTPError as exc:
            # 401/403 are a definite provider access rejection (a bad or rejected
            # key, an out-of-quota plan, or a blocked IP) — not a transient blip.
            # Surface it as a mapped, non-retryable provider access error; never
            # retry it, and never let the key reach the message.
            if exc.code in (401, 403):
                return ProviderResponse(
                    email=email,
                    result=None,
                    resultcode=None,
                    error="access_rejected",
                    raw={"error": "access_rejected", "http_status": exc.code},
                )
            # Other HTTP statuses: a transient transport failure (may retry). The
            # HTTPError string is "HTTP Error <code>: <reason>" (no URL/key); redact
            # defensively regardless.
            raise ProviderTransientError(
                self.redact(str(exc)), condition=http_transient_condition(exc.code)
            ) from None
        except Exception as exc:  # transport failure: no verdict, may retry
            raise ProviderTransientError(self.redact(str(exc))) from None
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ProviderTransientError(f"malformed provider response: {exc}") from None
        # Never keep the key anywhere in the stored payload.
        data.pop("api", None)
        data = redact_payload(data, self._api_key)
        return ProviderResponse(
            email=str(data.get("email", email)),
            result=data.get("result"),
            resultcode=data.get("resultcode"),
            subresult=data.get("subresult"),
            quality=data.get("quality"),
            free=data.get("free"),
            role=data.get("role"),
            didyoumean=data.get("didyoumean") or None,
            credits=data.get("credits"),
            execution_time=data.get("executiontime"),
            error=data.get("error") or None,
            livemode=bool(data.get("livemode", False)),
            raw={k: v for k, v in data.items() if k != "api"},
        )


class SimulatedDebounce(SimulatedMillionVerifier):
    """Network-free DeBounce adapter with the same deterministic fixtures."""

    name = "debounce"


class HttpDebounce:
    """DeBounce Single Validation adapter normalized to :class:`ProviderResponse`.

    Contract (official Single Validation API):

        GET https://api.debounce.io/v1/?api=<key>&email=<addr>

    The response nests the verdict under ``debounce`` and carries a top-level
    ``success`` flag. ``result`` is the documented human classification — "Safe
    to Send", "Risky", "Invalid", "Unknown" — and ``code`` is its numeric twin.

    Three rules govern the mapping, and all three exist to stop a fallback
    provider from manufacturing a verdict VMR has not earned:

    * ``result`` is authoritative when present, with ``code`` refining "Risky";
      ``code`` alone answers only when ``result`` is absent.
    * Nothing unrecognised degrades into a mailbox verdict. A missing,
      unparseable or unknown classification becomes an explicit unusable-response
      error, never "unknown" — "unknown" is a real thing a provider can say about
      a mailbox and must not double as "we could not read the reply".
    * ``send_transactional`` and the vendor's signup guidance are preserved but
      never consulted. They answer "may this address register an account", which
      is not the question cold outbound asks.

    DeBounce authenticates through the query string, so no complete request URL
    is ever returned, stored, or raised — only :meth:`redacted_url`.
    """

    name = "debounce"
    simulated = False
    _base_url = "https://api.debounce.io/v1/"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        timeout_seconds: int = 20,
        transport: Transport | None = None,
    ) -> None:
        self._api_key = api_key
        if base_url:
            self._base_url = base_url
        self._timeout = timeout_seconds
        self._transport = transport or UrllibTransport()

    def _build_url(self, email: str) -> str:
        query = urllib.parse.urlencode({"api": self._api_key, "email": email})
        return f"{self._base_url}?{query}"

    def redact(self, text: str) -> str:
        """Strip this client's credential from any text before it is stored."""

        return _redact(text, self._api_key)

    def redacted_url(self, email: str) -> str:
        return self.redact(self._build_url(email))

    def _failure(self, email: str, error: str, **extra: Any) -> ProviderResponse:
        """A provider-level failure: never address evidence, never billed here."""

        return ProviderResponse(
            email=email,
            result=None,
            resultcode=None,
            error=error,
            raw={"error": error, **extra},
        )

    def verify(self, email: str) -> ProviderResponse:
        try:
            body = self._transport.get(self._build_url(email), timeout=float(self._timeout))
        except ProviderTransientError:
            raise
        except urllib.error.HTTPError as exc:
            # 401 (rejected key), 402 (no credits) and 403 (forbidden) are settled
            # operator conditions. They map onto the existing non-retryable
            # provider-error family so nothing loops, spends, or hammers against
            # a condition only a human can clear.
            if exc.code in (401, 402, 403):
                error = "insufficient_credits" if exc.code == 402 else "access_rejected"
                return self._failure(email, error, http_status=exc.code)
            raise ProviderTransientError(
                f"HTTP {exc.code}", condition=http_transient_condition(exc.code)
            ) from None
        except Exception as exc:
            raise ProviderTransientError(self.redact(str(exc))) from None
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ProviderTransientError(
                f"malformed provider response: {self.redact(str(exc))}"
            ) from None
        if not isinstance(data, dict):
            return self._failure(email, "unusable_response")
        data.pop("api", None)
        data = redact_payload(data, self._api_key)

        nested_raw = data.get("debounce")
        nested: dict[str, Any] = nested_raw if isinstance(nested_raw, dict) else data

        # An application-level error field outranks any classification present.
        reported_error = nested.get("error") or data.get("error")
        if reported_error:
            return ProviderResponse(
                email=str(nested.get("email", email)),
                result=None,
                resultcode=None,
                error=self.redact(str(reported_error)),
                raw=_debounce_safe_raw(nested),
            )

        # ``success`` is the envelope, not the verdict: success = 0 means no
        # validation was produced at all, which is a provider failure.
        if "success" in data and _as_optional_bool(data.get("success")) is not True:
            return self._failure(email, "unusable_response", success=str(data.get("success")))

        classification = _classify_debounce(nested.get("result"), nested.get("code"))
        if classification is None:
            return self._failure(email, "unusable_response")
        result, code = classification
        return ProviderResponse(
            email=str(nested.get("email", email)),
            result=result,
            resultcode=code,
            subresult=str(nested.get("reason")) if nested.get("reason") else None,
            quality=str(nested.get("result")) if nested.get("result") else None,
            free=_as_optional_bool(nested.get("free_email")),
            role=(True if code == _DEBOUNCE_ROLE_CODE else _as_optional_bool(nested.get("role"))),
            didyoumean=nested.get("did_you_mean") or None,
            credits=int(nested["balance"]) if str(nested.get("balance", "")).isdigit() else None,
            error=None,
            livemode=True,
            raw=_debounce_safe_raw(nested),
        )


# The fields worth keeping from a DeBounce reply. An allow-list rather than a
# deny-list: an unanticipated field can never smuggle a credential or extra PII
# into durable storage just because the vendor started sending it.
_DEBOUNCE_RAW_FIELDS = frozenset(
    {
        "email",
        "code",
        "result",
        "reason",
        "role",
        "free_email",
        "did_you_mean",
        "send_transactional",
        "balance",
    }
)

# The published DeBounce result-code table, in full: 1 Syntax, 2 Spam Trap,
# 3 Disposable, 4 Accept-All, 5 Valid, 6 Invalid, 7 Unknown, 8 Role. Anything
# outside this set is unrecognised, which is a failure to read the reply rather
# than a statement about the mailbox.
#
# Code 8 maps to the canonical result ``ok`` *carrying the role flag*, because
# "role account" exists in this model only as a valid address that is role-based
# — :meth:`VerificationPolicy.precise_for_result` then yields ROLE_BASED, which
# is a warning and is never an accepted outreach address. It is deliberately not
# left as a bare ``ok``: see :data:`_DEBOUNCE_ROLE_CODE`.
_DEBOUNCE_CODE_RESULT: dict[int, str] = {
    1: "invalid",
    2: "invalid",
    3: "disposable",
    4: "catch_all",
    5: "ok",
    6: "invalid",
    7: "unknown",
    8: "ok",
}

#: The one code that *is itself* the role signal. When DeBounce answers 8 the
#: role flag is set from the code rather than read from the ``role`` field, so a
#: reply that classifies an address as Role can never be recorded as an ordinary
#: valid mailbox because the separate flag happened to be absent.
_DEBOUNCE_ROLE_CODE = 8


def _debounce_safe_raw(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key in _DEBOUNCE_RAW_FIELDS}


def _debounce_code(value: object) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _classify_debounce(result: object, code: object) -> tuple[str, int | None] | None:
    """Map one DeBounce classification onto the canonical result vocabulary.

    Returns ``None`` when the reply carries nothing this adapter recognises, so
    the caller raises an unusable-response failure instead of inventing a verdict.

    "Risky" is the only DeBounce class with no exact counterpart in the VMR
    model: one word covering accept-all, role and disposable addresses. The
    numeric code separates all three — 4 accept-all, 3 disposable, 8 role — and
    where it cannot, Risky maps to ``unknown``: uncertain, never accepted, and
    carrying the shortest reuse TTL of the uncertain states.

    Risky never yields an *accepted* address. Code 8 returns ``ok`` only so the
    caller can set the role flag beside it, which is the single way this model
    expresses a role account; the precise status that results is ROLE_BASED, a
    warning that no Campaign Contact advances on.
    """

    numeric = _debounce_code(code)
    label = " ".join(str(result).strip().casefold().split()) if result is not None else ""

    if label in {"safe to send", "safe_to_send", "deliverable"}:
        return "ok", numeric
    if label == "invalid":
        return "invalid", numeric
    if label == "unknown":
        return "unknown", numeric
    if label == "risky":
        if numeric == 4:
            return "catch_all", numeric
        if numeric == 3:
            return "disposable", numeric
        if numeric == _DEBOUNCE_ROLE_CODE:
            return "ok", numeric
        return "unknown", numeric
    if label:
        # A classification string we do not recognise: unreadable, not a verdict.
        return None
    if numeric in _DEBOUNCE_CODE_RESULT:
        return _DEBOUNCE_CODE_RESULT[numeric], numeric
    return None


def build_provider(
    *,
    api_key: str | None,
    base_url: str,
    timeout_seconds: int,
    live: bool = False,
) -> VerificationProvider:
    """Choose the provider implementation.

    The simulator is returned unless a *real* (non-test) key is configured and
    ``live`` is explicitly requested — so no automated path can reach the network.
    """

    key = (api_key or "").strip()
    is_test_key = key in TEST_KEYS
    if live and key and not is_test_key:
        return HttpMillionVerifier(key, base_url=base_url, timeout_seconds=timeout_seconds)
    return SimulatedMillionVerifier(api_key=key or None)


def build_provider_by_id(
    provider_id: str,
    *,
    api_key: str | None,
    timeout_seconds: int = 20,
    live: bool = False,
    base_url: str | None = None,
) -> VerificationProvider:
    """Build only a provider declared in the fixed registry.

    ``base_url`` exists so an operator setting (and a test stub) can override the
    documented endpoint. Omitting it keeps each adapter's documented default.
    """

    key = (api_key or "").strip()
    if provider_id == "millionverifier":
        return build_provider(
            api_key=key or None,
            base_url=base_url or "https://api.millionverifier.com/api/v3",
            timeout_seconds=timeout_seconds,
            live=live,
        )
    if provider_id == "debounce":
        if live and key:
            return HttpDebounce(key, base_url=base_url, timeout_seconds=timeout_seconds)
        return SimulatedDebounce(api_key=key or None)
    raise ValueError("unknown verification provider")
