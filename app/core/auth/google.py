"""The live Google OAuth 2.0 / OpenID Connect client for VMR application identity.

Scope of this module, stated so it cannot drift: it obtains *who the operator is*
and nothing else. It requests ``openid email profile``, it never asks for a Gmail
scope, it never persists a token of any kind, and the access token it receives is
discarded the moment the ID token has been read. Mailbox authorization is a
separate grant with separate credentials and is not implemented here.

What is proven about an assertion before anyone is signed in
------------------------------------------------------------
Every one of these must pass; none of them is optional and none is inferred:

``signature``  RS256 against Google's published JWKS, key selected by exact
               ``kid``, single accepted algorithm — see ``app/core/auth/jwks.py``.
``iss``        one of the two documented Google issuer strings.
``aud``        constant-time equality with this deployment's client id.
``nonce``      constant-time equality with the nonce minted for *this* browser's
               sign-in transaction.
``exp``/``iat`` bounded freshness with a small symmetric clock leeway.
``email_verified`` explicitly true.
``state``      checked by the callback route against the signed, single-use
               transaction cookie before this module is reached at all.
``PKCE``       the authorization code is bound to a verifier only this process
               ever held.

The claim rules live in ``app/core/auth/identity.py`` so they are
provider-independent and directly testable; the signature work lives in
``app/core/auth/jwks.py`` for the same reason. No email, name or identifier is
ever taken from a request parameter, a form field or a header — only from a
verified assertion.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.auth.config import GOOGLE_IDENTITY_SCOPES, AuthSettings
from app.core.auth.identity import IdentityAssertionError, IdentityClaims
from app.core.auth.jwks import JwksClient, verify_id_token


def generate_code_verifier() -> str:
    """A fresh PKCE code verifier (RFC 7636 §4.1)."""

    return secrets.token_urlsafe(64)


def code_challenge_for(verifier: str) -> str:
    """The S256 PKCE challenge for ``verifier`` (RFC 7636 §4.2)."""

    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def claims_from_payload(payload: dict[str, Any]) -> IdentityClaims:
    """Shape a *verified* claim payload into :class:`IdentityClaims`.

    Only ever called with a payload whose signature has already been checked —
    there is deliberately no code path in this package that turns an unverified
    token into claims.
    """

    issuer = payload.get("iss")
    subject = payload.get("sub")
    audience = payload.get("aud")
    email = payload.get("email")
    expires_at = payload.get("exp")
    issued_at = payload.get("iat")

    # `aud` may legitimately be a single string or an array. An array is only
    # unambiguous when it names exactly one client; anything else is refused
    # rather than guessed at.
    if isinstance(audience, list):
        if len(audience) != 1 or not isinstance(audience[0], str):
            raise IdentityAssertionError("identity assertion has an ambiguous audience")
        audience = audience[0]

    if not isinstance(issuer, str) or not isinstance(subject, str) or not isinstance(audience, str):
        raise IdentityAssertionError("identity assertion is missing required claims")
    if not isinstance(email, str):
        raise IdentityAssertionError("identity assertion carries no email address")
    if not isinstance(expires_at, int) or isinstance(expires_at, bool):
        raise IdentityAssertionError("identity assertion has no bounded expiry")
    if not isinstance(issued_at, int) or isinstance(issued_at, bool):
        raise IdentityAssertionError("identity assertion has no issue time")

    raw_verified = payload.get("email_verified")
    # Google sends a JSON boolean; some providers send the string form. Anything
    # that is not an explicit affirmative is treated as unverified.
    email_verified = raw_verified is True or (
        isinstance(raw_verified, str) and raw_verified.strip().lower() == "true"
    )

    nonce = payload.get("nonce")
    hosted_domain = payload.get("hd")
    display_name = payload.get("name")

    return IdentityClaims(
        subject=subject,
        email=email,
        email_verified=email_verified,
        display_name=display_name if isinstance(display_name, str) else "",
        issuer=issuer,
        audience=audience,
        expires_at=expires_at,
        issued_at=issued_at,
        nonce=nonce if isinstance(nonce, str) else None,
        hosted_domain=hosted_domain if isinstance(hosted_domain, str) else None,
    )


class GoogleIdentityProvider:
    """Google Sign-In restricted to VMR application identity."""

    def __init__(
        self,
        settings: AuthSettings,
        *,
        client: httpx.AsyncClient | None = None,
        jwks: JwksClient | None = None,
    ) -> None:
        if not settings.has_google_client():
            raise ValueError("Google identity requires both a client id and a client secret")
        self._settings = settings
        self._client = client
        self._jwks = jwks or JwksClient(
            timeout_seconds=settings.google_request_timeout_seconds, client=client
        )

    def authorization_url(
        self,
        *,
        redirect_uri: str,
        state: str,
        nonce: str,
        code_challenge: str,
    ) -> str:
        parameters = {
            "client_id": self._settings.google_client_id or "",
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(GOOGLE_IDENTITY_SCOPES),
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            # Always show the chooser. Operators routinely have a personal and a
            # work account signed in, and silently reusing the wrong one produces
            # an access-denied screen that looks like a fault.
            "prompt": "select_account",
            # No refresh token is wanted: this is a sign-in, not a delegation.
            "access_type": "online",
        }
        if self._settings.allowed_google_domain:
            # A hint only. It is not a security control — the real domain check
            # runs on the returned claims — but it removes a class of avoidable
            # access-denied screens.
            parameters["hd"] = self._settings.allowed_google_domain
        return f"{self._settings.google_authorization_endpoint}?{urlencode(parameters)}"

    async def exchange_code(
        self,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str,
    ) -> IdentityClaims:
        payload = {
            "code": code,
            "client_id": self._settings.google_client_id or "",
            "client_secret": self._settings.google_client_secret or "",
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": code_verifier,
        }
        timeout = self._settings.google_request_timeout_seconds
        try:
            if self._client is not None:
                response = await self._client.post(
                    self._settings.google_token_endpoint, data=payload, timeout=timeout
                )
            else:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(self._settings.google_token_endpoint, data=payload)
        except httpx.HTTPError as exc:
            # The provider's own text can echo the submitted code; keep it out.
            raise IdentityAssertionError("the identity provider could not be reached") from exc

        if response.status_code != 200:
            raise IdentityAssertionError("the identity provider refused the authorization code")
        try:
            body = response.json()
        except ValueError as exc:
            raise IdentityAssertionError(
                "the identity provider returned a malformed response"
            ) from exc
        if not isinstance(body, dict):
            raise IdentityAssertionError("the identity provider returned a malformed response")
        id_token = body.get("id_token")
        if not isinstance(id_token, str):
            raise IdentityAssertionError("the identity provider returned no identity assertion")
        # Signature first. Nothing downstream ever sees the claims of a token
        # whose RS256 signature did not verify against Google's published JWKS.
        payload = await verify_id_token(id_token, jwks=self._jwks)
        return claims_from_payload(payload)
