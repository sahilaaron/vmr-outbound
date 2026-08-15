"""The credential the Google Sheets add-on presents, and the rules it must pass.

The add-on runs inside Google Apps Script. It holds no secret of ours, it cannot
keep a browser cookie, and it must never be handed one: an add-on that stores a
long-lived VMR credential puts that credential inside a document the operator
may share, copy or export. So it presents something it does not own and cannot
forge — a Google-signed OpenID Connect ID token for the person running the
sheet, minted fresh by ``ScriptApp.getIdentityToken()`` on every execution.

Why this is a *narrower* credential than a stored token, not a wider one
-----------------------------------------------------------------------

* **Nothing durable exists to steal.** There is no row, no digest, no refresh
  secret and nothing written into a cell or a script property. A copied
  spreadsheet copies no authority.
* **It expires on its own.** Google mints these with roughly an hour of life,
  and the add-on never persists one, so the replay window is bounded without
  this application operating an expiry schedule.
* **Revocation is immediate and is the account.** The owning ``users`` row is
  re-read on every request, so disabling an account stops the add-on on its next
  call — the same property the extension link gets from ``revoked_at``, obtained
  without a second table to keep in step.
* **It cannot be minted for us by anybody else.** ``aud`` is checked against a
  configured allow-list of the add-on's own OAuth client ids, which is the check
  that stops a valid Google token issued to some other application from being
  replayed here. That is the classic confused-deputy, and it is refused by
  equality against configuration rather than by inspecting the token's contents.

What this module deliberately does not do
-----------------------------------------

It does not decide whether the person may in. That is two further questions,
answered elsewhere and in this order: is there an **active** VMR account for
this Google identity (``app/services/integrations/sheets/identity.py``), and may
that account reach the Campaign it named (``app/services/campaign_access.py``).
Keeping the three apart is what lets a test prove that a perfectly valid Google
assertion from an unknown or disabled account is still refused.

The signature work is not reimplemented here. ``app/core/auth/jwks.py`` already
verifies RS256 against Google's published key set, refuses a token that names
its own key, and parses the payload only after the signature verifies; this
module supplies the claim rules that sit on top of it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

from app.core.auth.google import claims_from_payload
from app.core.auth.identity import IdentityAssertionError, IdentityClaims
from app.core.auth.jwks import JwksClient, verify_id_token
from app.core.auth.session import constant_time_equal

#: Google's two spellings of its own issuer, matching ``AuthSettings``.
DEFAULT_ACCEPTED_ISSUERS: tuple[str, ...] = ("https://accounts.google.com", "accounts.google.com")

#: Symmetric tolerance for clock skew between this host and Google.
DEFAULT_LEEWAY_SECONDS = 60


@dataclass(frozen=True)
class VerifiedAssertion:
    """A Google identity assertion this deployment has accepted."""

    subject: str
    email: str
    display_name: str
    audience: str


class AssertionVerifier(Protocol):
    """The one operation the Sheets routes need from the identity provider."""

    async def verify(self, token: str) -> VerifiedAssertion:
        """Return the verified identity, or raise :class:`IdentityAssertionError`."""


def bearer_token(header_value: str | None) -> str:
    """Extract the token from an ``Authorization`` header, or raise.

    Written as a refusal rather than an ``Optional`` return so that a missing
    header, a wrong scheme and a malformed value all leave by the same door and
    produce the same answer to the caller.
    """

    if not header_value:
        raise IdentityAssertionError("no integration credential was presented")
    scheme, separator, value = header_value.partition(" ")
    if not separator or scheme.strip().lower() != "bearer":
        raise IdentityAssertionError("the integration credential is not a bearer token")
    token = value.strip()
    if not token:
        raise IdentityAssertionError("the integration credential is empty")
    return token


def validate_assertion_claims(
    claims: IdentityClaims,
    *,
    allowed_audiences: tuple[str, ...],
    accepted_issuers: tuple[str, ...] = DEFAULT_ACCEPTED_ISSUERS,
    now: int,
    leeway_seconds: int = DEFAULT_LEEWAY_SECONDS,
) -> VerifiedAssertion:
    """Apply the claim rules to an assertion whose signature already verified.

    Pure: no clock, no network, no database. Every refusal below is one an
    attacker would otherwise get for free.
    """

    if claims.issuer not in accepted_issuers:
        raise IdentityAssertionError("identity assertion has an unexpected issuer")
    if not allowed_audiences:
        # An empty allow-list is a configuration mistake, and the safe reading of
        # a mistake is "nobody", never "anybody".
        raise IdentityAssertionError("this deployment accepts no integration audience")
    if not any(constant_time_equal(claims.audience, allowed) for allowed in allowed_audiences):
        raise IdentityAssertionError("identity assertion was issued for a different client")
    if claims.expires_at <= now - leeway_seconds:
        raise IdentityAssertionError("identity assertion has expired")
    if claims.issued_at > now + leeway_seconds:
        raise IdentityAssertionError("identity assertion was issued in the future")
    if not claims.email_verified:
        raise IdentityAssertionError("identity assertion carries an unverified email address")
    if not claims.subject:
        raise IdentityAssertionError("identity assertion has no subject")
    if not claims.email:
        raise IdentityAssertionError("identity assertion has no usable email address")
    return VerifiedAssertion(
        subject=claims.subject,
        email=claims.email,
        display_name=claims.display_name,
        audience=claims.audience,
    )


class GoogleAssertionVerifier:
    """Verify a Google-minted ID token against the published key set.

    The JWKS client is shared across requests so that a batch of calls costs one
    key fetch, not one per call; ``JwksClient`` already honours the cache headers
    Google sends.
    """

    def __init__(
        self,
        *,
        allowed_audiences: tuple[str, ...],
        accepted_issuers: tuple[str, ...] = DEFAULT_ACCEPTED_ISSUERS,
        jwks: JwksClient | None = None,
        timeout_seconds: float = 10.0,
        leeway_seconds: int = DEFAULT_LEEWAY_SECONDS,
    ) -> None:
        self._allowed_audiences = allowed_audiences
        self._accepted_issuers = accepted_issuers
        self._jwks = jwks or JwksClient(timeout_seconds=timeout_seconds)
        self._leeway_seconds = leeway_seconds

    async def verify(self, token: str) -> VerifiedAssertion:
        payload: dict[str, Any] = await verify_id_token(token, jwks=self._jwks)
        claims = claims_from_payload(payload)
        return validate_assertion_claims(
            claims,
            allowed_audiences=self._allowed_audiences,
            accepted_issuers=self._accepted_issuers,
            now=int(time.time()),
            leeway_seconds=self._leeway_seconds,
        )


__all__ = [
    "DEFAULT_ACCEPTED_ISSUERS",
    "DEFAULT_LEEWAY_SECONDS",
    "AssertionVerifier",
    "GoogleAssertionVerifier",
    "IdentityAssertionError",
    "VerifiedAssertion",
    "bearer_token",
    "validate_assertion_claims",
]
