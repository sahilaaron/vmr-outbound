"""Versioned verification policy: outcome mapping and freshness (VER-002 / VER-003).

The policy is deterministic backend logic (AGENTS.md) with an explicit version
string stamped onto every piece of evidence and every job, so a later policy
change never silently reinterprets old evidence. It answers two questions:

* How does a raw provider response map to an internal outcome? (VER-002)
* Is a stored address result still fresh under the active TTLs? (VER-003)

Safety invariants encoded here:

* Only ok/invalid/catch_all/unknown/disposable are *address evidence*. A provider
  ``error`` result, a transport timeout, an insufficient-credit condition, and a
  configuration error are **not** evidence about the mailbox and never create an
  :class:`ExactEmailVerification` row.
* catch_all, unknown, and disposable never map to a "valid" state.
* Only ok/invalid/disposable are billable; catch_all/unknown are free.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.config import Settings
from app.models.enums import EmailPreciseStatus, EmailVerificationResult
from app.services.verification.provider import ProviderResponse

POLICY_VERSION = "ver-1"

# Transient application-level provider errors that justify a bounded retry.
_TRANSIENT_ERRORS = frozenset({"ip_address_blocked", "internal_error", "timeout"})
# Configuration/operational errors that must NOT auto-retry (a retry cannot help
# until a human acts) and are never address evidence. ``access_rejected`` is a
# transport-level 401/403 from the provider (rejected key/plan/IP).
_CONFIG_ERRORS = frozenset({"invalid_api_key", "no_apikey", "no_email", "access_rejected"})

_KIND_TRANSIENT = "transient"
_KIND_ADDRESS = "address"
_KIND_INSUFFICIENT_CREDITS = "insufficient_credits"
_KIND_PROVIDER_ERROR = "provider_error"

_RESULT_MAP = {
    "ok": EmailVerificationResult.VALID,
    "invalid": EmailVerificationResult.INVALID,
    "catch_all": EmailVerificationResult.CATCH_ALL,
    "unknown": EmailVerificationResult.UNKNOWN,
    "disposable": EmailVerificationResult.DISPOSABLE,
}
_BILLABLE = frozenset(
    {
        EmailVerificationResult.VALID,
        EmailVerificationResult.INVALID,
        EmailVerificationResult.DISPOSABLE,
    }
)


@dataclass(frozen=True)
class MappedOutcome:
    """The internal interpretation of one provider response."""

    kind: str
    result: EmailVerificationResult | None
    precise: EmailPreciseStatus
    credited: bool
    retryable: bool
    reason: str
    is_role: bool = False

    @property
    def is_address_evidence(self) -> bool:
        return self.kind == _KIND_ADDRESS


class VerificationPolicy:
    """Active, versioned verification policy."""

    version = POLICY_VERSION

    def __init__(self, settings: Settings) -> None:
        self._ttl_days = {
            EmailVerificationResult.VALID: settings.verification_ttl_valid_days,
            EmailVerificationResult.INVALID: settings.verification_ttl_invalid_days,
            EmailVerificationResult.CATCH_ALL: settings.verification_ttl_catch_all_days,
            EmailVerificationResult.UNKNOWN: settings.verification_ttl_unknown_days,
            EmailVerificationResult.DISPOSABLE: settings.verification_ttl_disposable_days,
        }

    def ttl(self, result: EmailVerificationResult) -> timedelta:
        return timedelta(days=self._ttl_days[result])

    def is_fresh(
        self, result: EmailVerificationResult, checked_at: datetime, now: datetime
    ) -> bool:
        """True when evidence of *result* checked at *checked_at* is still fresh."""

        return (now - checked_at) <= self.ttl(result)

    def map_response(self, response: ProviderResponse) -> MappedOutcome:
        """Map a raw provider response to an internal outcome (never network)."""

        # Application-level error field takes precedence over any result value.
        if response.error:
            err = response.error.strip().lower()
            if err == "insufficient_credits":
                return MappedOutcome(
                    kind=_KIND_INSUFFICIENT_CREDITS,
                    result=None,
                    precise=EmailPreciseStatus.INSUFFICIENT_CREDITS,
                    credited=False,
                    retryable=False,
                    reason="provider reported insufficient credits — top up and re-run",
                )
            if err in _TRANSIENT_ERRORS:
                return MappedOutcome(
                    kind=_KIND_TRANSIENT,
                    result=None,
                    precise=EmailPreciseStatus.PROVIDER_ERROR,
                    credited=False,
                    retryable=True,
                    reason=f"transient provider error ({err}); will retry with backoff",
                )
            # Configuration errors: no auto-retry, never address evidence.
            if err == "access_rejected":
                reason = "provider rejected access (HTTP 401/403) — verify the API key and plan"
            else:
                reason = f"provider error ({err}) requires operator action"
            return MappedOutcome(
                kind=_KIND_PROVIDER_ERROR,
                result=None,
                precise=EmailPreciseStatus.PROVIDER_ERROR,
                credited=False,
                retryable=False,
                reason=reason,
            )

        result_str = (response.result or "").strip().lower()

        # A verification "error" result (resultcode 4): no verdict, retryable,
        # never address evidence.
        if result_str == "error":
            return MappedOutcome(
                kind=_KIND_TRANSIENT,
                result=None,
                precise=EmailPreciseStatus.PROVIDER_ERROR,
                credited=False,
                retryable=True,
                reason="provider could not complete verification (result=error); will retry",
            )

        mapped = _RESULT_MAP.get(result_str)
        if mapped is None:
            # Unrecognised result: treat conservatively as a retryable provider
            # error, never as a mailbox verdict.
            return MappedOutcome(
                kind=_KIND_TRANSIENT,
                result=None,
                precise=EmailPreciseStatus.PROVIDER_ERROR,
                credited=False,
                retryable=True,
                reason=f"unrecognised provider result {result_str!r}; will retry",
            )

        is_role = bool(response.role)
        precise = self.precise_for_result(mapped, is_role=is_role)
        return MappedOutcome(
            kind=_KIND_ADDRESS,
            result=mapped,
            precise=precise,
            credited=mapped in _BILLABLE,
            retryable=False,
            reason=self._address_reason(mapped, is_role=is_role),
            is_role=is_role,
        )

    @staticmethod
    def precise_for_result(result: EmailVerificationResult, *, is_role: bool) -> EmailPreciseStatus:
        """The precise status a *fresh* address result maps to.

        A valid mailbox that is role-based is a Warning, not a Success: role
        addresses are not safe individual-outreach targets (#137 warning list).
        """

        if result == EmailVerificationResult.VALID:
            return EmailPreciseStatus.ROLE_BASED if is_role else EmailPreciseStatus.VALID
        return {
            EmailVerificationResult.INVALID: EmailPreciseStatus.INVALID,
            EmailVerificationResult.CATCH_ALL: EmailPreciseStatus.CATCH_ALL,
            EmailVerificationResult.UNKNOWN: EmailPreciseStatus.UNKNOWN,
            EmailVerificationResult.DISPOSABLE: EmailPreciseStatus.DISPOSABLE,
        }[result]

    @staticmethod
    def _address_reason(result: EmailVerificationResult, *, is_role: bool) -> str:
        base = {
            EmailVerificationResult.VALID: "mailbox exists (provider: ok)",
            EmailVerificationResult.INVALID: "mailbox does not exist (provider: invalid)",
            EmailVerificationResult.CATCH_ALL: "domain accepts all mail; mailbox unproven",
            EmailVerificationResult.UNKNOWN: "provider could not determine the mailbox (unknown)",
            EmailVerificationResult.DISPOSABLE: "disposable/temporary mailbox (disposable)",
        }[result]
        if is_role and result == EmailVerificationResult.VALID:
            return base + "; role-based address — treated as a warning, not scheduling-ready"
        return base


def get_policy(settings: Settings) -> VerificationPolicy:
    return VerificationPolicy(settings)
