"""The identity-provider seam and the claim rules applied to its assertion.

Two things live here and nothing else: the narrow interface the sign-in routes
depend on, and a pure function that decides whether a returned assertion is
acceptable. Keeping the rules pure and provider-free is what lets the adversarial
tests drive every refusal path — wrong audience, wrong issuer, expired, replayed
nonce, unverified address — without a network, a clock hack or a stub HTTP
server.

The live Google implementation is in ``app/core/auth/google.py``.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Protocol

from app.core.auth.config import normalize_operator_email


class IdentityAssertionError(Exception):
    """Raised when a provider assertion is missing, malformed or unacceptable.

    Message text is safe to log and safe to show: it never contains the token,
    the code, the client secret or the address that failed.
    """


@dataclass(frozen=True)
class IdentityClaims:
    """The subset of an OpenID Connect ID token this application relies on."""

    subject: str
    email: str
    email_verified: bool
    display_name: str
    issuer: str
    audience: str
    expires_at: int
    issued_at: int
    nonce: str | None = None
    hosted_domain: str | None = None


class IdentityProvider(Protocol):
    """The two operations a sign-in needs from an identity provider."""

    def authorization_url(
        self,
        *,
        redirect_uri: str,
        state: str,
        nonce: str,
        code_challenge: str,
    ) -> str:
        """Where to send the browser to obtain an authorization code."""

    async def exchange_code(
        self,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str,
    ) -> IdentityClaims:
        """Trade an authorization code for the caller's validated identity.

        Implementations must perform this exchange server-to-server over TLS and
        must raise :class:`IdentityAssertionError` for every failure mode rather
        than returning a partially populated result.
        """


def validate_identity_claims(
    claims: IdentityClaims,
    *,
    client_id: str,
    accepted_issuers: tuple[str, ...],
    expected_nonce: str,
    now: int,
    leeway_seconds: int = 60,
) -> str:
    """Return the normalised approved-looking email, or raise.

    "Approved-looking" is deliberate: this function decides whether the
    *assertion* is trustworthy, not whether the person is allowed in. The
    allow-list decision is separate and happens after, so a test can prove that
    a perfectly valid Google identity outside the allow-list is still refused.

    Every check below is a refusal an attacker would otherwise get for free:

    ``iss``
        A token minted by some other issuer must not be accepted just because it
        is well formed.
    ``aud``
        A token minted for a *different* OAuth client — including one the
        attacker owns — is a valid Google token and would otherwise sign them
        in. This is the check that stops the classic confused-deputy.
    ``nonce``
        Binds the assertion to the sign-in transaction this browser started, so
        an assertion captured elsewhere cannot be replayed into someone else's
        session.
    ``exp`` / ``iat``
        Bounded freshness, with a small symmetric leeway for clock skew.
    ``email_verified``
        An unverified address is a claim about an address the account holder may
        not control. Matching one against the allow-list would let anyone who can
        create a Google account with an unverified address of an approved
        operator sign in as them.
    """

    if claims.issuer not in accepted_issuers:
        raise IdentityAssertionError("identity assertion has an unexpected issuer")
    if not client_id or not _constant_equal(claims.audience, client_id):
        raise IdentityAssertionError("identity assertion was issued for a different client")
    if not expected_nonce or not _constant_equal(claims.nonce or "", expected_nonce):
        raise IdentityAssertionError("identity assertion does not match this sign-in request")
    if claims.expires_at <= now - leeway_seconds:
        raise IdentityAssertionError("identity assertion has expired")
    if claims.issued_at > now + leeway_seconds:
        raise IdentityAssertionError("identity assertion was issued in the future")
    if not claims.email_verified:
        raise IdentityAssertionError("identity assertion carries an unverified email address")
    if not claims.subject:
        raise IdentityAssertionError("identity assertion has no subject")

    email = normalize_operator_email(claims.email)
    if not email:
        raise IdentityAssertionError("identity assertion has no usable email address")
    return email


def _constant_equal(left: str, right: str) -> bool:
    """Constant-time string comparison for values an attacker may probe."""

    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
