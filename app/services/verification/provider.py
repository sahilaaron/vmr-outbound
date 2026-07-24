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
from typing import Any, Protocol

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


class ProviderTransientError(Exception):
    """A transport-level failure (no HTTP response): connection error or timeout.

    Distinct from an application-level ``error`` field in a 200 response — this
    means we never reached a verdict and the job may retry.
    """


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

    return (
        SIMULATOR_PROVIDER_LABEL if getattr(provider, "simulated", False) else LIVE_PROVIDER_LABEL
    )


def _redact(text: str, secret: str | None) -> str:
    if secret:
        return text.replace(secret, "***REDACTED***")
    return text


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

    def redacted_url(self, email: str) -> str:
        """The request URL with the key redacted, safe to log or show."""

        return _redact(self._build_url(email), self._api_key)

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
            raise ProviderTransientError(_redact(str(exc), self._api_key)) from None
        except Exception as exc:  # transport failure: no verdict, may retry
            raise ProviderTransientError(_redact(str(exc), self._api_key)) from None
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ProviderTransientError(f"malformed provider response: {exc}") from None
        # Never keep the key anywhere in the stored payload.
        data.pop("api", None)
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
