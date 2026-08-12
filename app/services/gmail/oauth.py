"""The Gmail mailbox authorization client.

This is *not* the sign-in client. ``app/core/auth/google.py`` proves who an
operator is with ``openid email profile``, stores nothing, and is reached from
``/auth/*``. This module asks a human for permission to write a draft into a
mailbox, stores a refresh token, and is reached only from ``/gmail/*`` after an
already-authenticated operator explicitly asked for it. The two share no client
id, no client secret, no cookie and no code path.

What is proven before a mailbox is bound
----------------------------------------
``state``     compared against the signed, single-use, ``HttpOnly`` transaction
              cookie minted for *this* browser -- checked in the route before
              this module is reached.
``operator``  the transaction cookie also carries the operator subject the
              authorization was started by, and the route refuses if the
              signed-in operator is not that operator. A callback replayed into
              a second operator's browser therefore cannot bind a mailbox to
              them.
``PKCE``      the code is bound to a verifier only this process ever held.
``ID token``  RS256 against Google's published JWKS, ``aud`` equal to the *Gmail*
              client id, ``iss`` a documented Google issuer, bounded freshness,
              ``email_verified`` true -- reusing ``app/core/auth/jwks.py`` and
              ``app/core/auth/identity.py`` verbatim rather than writing a
              second, weaker verifier.
``scope``     the granted scope list is read from Google's response and checked
              for the compose scope. What was *asked for* proves nothing: a
              consent screen where the operator unticks the mailbox permission
              returns a narrower grant, and recording the request would claim a
              capability the grant does not carry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx

from app.core.auth.identity import IdentityAssertionError, IdentityClaims, validate_identity_claims
from app.core.auth.jwks import JwksClient, verify_id_token
from app.core.gmail_config import (
    GMAIL_AUTHORIZATION_SCOPES,
    GMAIL_COMPOSE_SCOPE,
    GmailSettings,
)

#: Google's documented issuers, duplicated from ``AuthSettings`` rather than
#: read from it: this client validates a token minted for a *different*
#: audience, and coupling it to the identity block's configuration would let an
#: edit intended for sign-in change what a mailbox grant accepts.
GOOGLE_ISSUERS: tuple[str, ...] = ("https://accounts.google.com", "accounts.google.com")


class GmailAuthorizationError(Exception):
    """A mailbox authorization could not be completed.

    One exception type, and the message is always a fixed operator-readable
    sentence. A provider's own error text can echo the submitted authorization
    code or refresh token, so none of it is ever propagated.
    """


@dataclass(frozen=True)
class GmailTokenGrant:
    """What Google returned for one authorization code or refresh."""

    access_token: str
    #: ``None`` on a refresh: Google returns a new refresh token only when it
    #: decides to rotate one, and the absence of it means "keep the one you
    #: have", never "the grant has no refresh token".
    refresh_token: str | None
    expires_in: int
    granted_scopes: tuple[str, ...]
    #: Present on the initial exchange, absent on a refresh.
    claims: IdentityClaims | None

    def has_compose_scope(self) -> bool:
        return GMAIL_COMPOSE_SCOPE in self.granted_scopes


class GmailOAuthClient(Protocol):
    """The seam a test replaces with a deterministic stub."""

    def authorization_url(
        self,
        *,
        redirect_uri: str,
        state: str,
        nonce: str,
        code_challenge: str,
        login_hint: str | None,
    ) -> str: ...

    def exchange_code(
        self, *, code: str, redirect_uri: str, code_verifier: str
    ) -> GmailTokenGrant: ...

    def refresh(self, *, refresh_token: str) -> GmailTokenGrant: ...

    def revoke(self, *, token: str) -> None: ...


def _scopes_from(payload: dict[str, Any]) -> tuple[str, ...]:
    raw = payload.get("scope")
    if not isinstance(raw, str):
        return ()
    return tuple(item for item in raw.split(" ") if item)


class GoogleGmailOAuthClient:
    """The live Gmail authorization-code client."""

    def __init__(
        self,
        settings: GmailSettings,
        *,
        client: httpx.Client | None = None,
        jwks: JwksClient | None = None,
    ) -> None:
        if not settings.has_client():
            raise ValueError("Gmail authorization requires both a client id and a client secret")
        self._settings = settings
        self._client = client
        self._jwks = jwks or JwksClient(timeout_seconds=settings.request_timeout_seconds)

    # --- consent ------------------------------------------------------------

    def authorization_url(
        self,
        *,
        redirect_uri: str,
        state: str,
        nonce: str,
        code_challenge: str,
        login_hint: str | None,
    ) -> str:
        parameters = {
            "client_id": self._settings.client_id or "",
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(GMAIL_AUTHORIZATION_SCOPES),
            "state": state,
            # Binds the returned ID token to *this* browser's authorization, so
            # an assertion captured elsewhere cannot be replayed to bind a
            # mailbox here. Verified in `verify_mailbox_identity`.
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            # A refresh token is genuinely required: the operator clicks "create
            # drafts" minutes or days after connecting, and an access token
            # lasts an hour. This is the one place in the application that asks
            # for offline access, and it asks because the feature cannot work
            # without it -- not for convenience.
            "access_type": "offline",
            # Always show the consent screen. Google returns a refresh token
            # only on a *new* consent, so an operator reconnecting after a
            # revocation would otherwise get an access token with nothing behind
            # it and a connection that dies in an hour.
            "prompt": "consent",
            # Ask Google to include the granted scopes in the token response, so
            # the check below is against what was granted rather than what was
            # requested.
            "include_granted_scopes": "false",
        }
        if login_hint:
            # A hint only, and never a security control: which account the
            # operator chooses is verified from the returned ID token.
            parameters["login_hint"] = login_hint
        return f"{self._settings.authorization_endpoint}?{urlencode(parameters)}"

    # --- token endpoint -----------------------------------------------------

    def _post(self, url: str, payload: dict[str, str]) -> dict[str, Any]:
        timeout = self._settings.request_timeout_seconds
        try:
            if self._client is not None:
                response = self._client.post(url, data=payload, timeout=timeout)
            else:
                with httpx.Client(timeout=timeout) as client:
                    response = client.post(url, data=payload)
        except httpx.HTTPError as exc:
            raise GmailAuthorizationError("Google could not be reached.") from exc
        if response.status_code != 200:
            # The provider's own body can echo the submitted code or token.
            raise GmailAuthorizationError("Google refused this Gmail authorization.")
        try:
            body = response.json()
        except ValueError as exc:
            raise GmailAuthorizationError("Google returned a malformed response.") from exc
        if not isinstance(body, dict):
            raise GmailAuthorizationError("Google returned a malformed response.")
        return body

    def _verified_claims(self, body: dict[str, Any]) -> IdentityClaims:
        id_token = body.get("id_token")
        if not isinstance(id_token, str):
            raise GmailAuthorizationError("Google did not say which mailbox was connected.")
        try:
            payload = _run_sync(verify_id_token(id_token, jwks=self._jwks))
        except IdentityAssertionError as exc:
            raise GmailAuthorizationError("Google's mailbox assertion did not verify.") from exc
        from app.core.auth.google import claims_from_payload

        return claims_from_payload(payload)

    def exchange_code(self, *, code: str, redirect_uri: str, code_verifier: str) -> GmailTokenGrant:
        body = self._post(
            self._settings.token_endpoint,
            {
                "code": code,
                "client_id": self._settings.client_id or "",
                "client_secret": self._settings.client_secret or "",
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
            },
        )
        access_token = body.get("access_token")
        refresh_token = body.get("refresh_token")
        if not isinstance(access_token, str) or not access_token:
            raise GmailAuthorizationError("Google returned no usable mailbox authorization.")
        if not isinstance(refresh_token, str) or not refresh_token:
            # Without one, the connection would stop working within the hour and
            # the operator would have no way to tell why. Refusing here makes
            # that a clear failure at connect time instead.
            raise GmailAuthorizationError(
                "Google returned no durable mailbox authorization. Remove VMR from your "
                "Google account permissions and connect again."
            )
        return GmailTokenGrant(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=_expires_in(body),
            granted_scopes=_scopes_from(body),
            claims=self._verified_claims(body),
        )

    def refresh(self, *, refresh_token: str) -> GmailTokenGrant:
        body = self._post(
            self._settings.token_endpoint,
            {
                "client_id": self._settings.client_id or "",
                "client_secret": self._settings.client_secret or "",
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        access_token = body.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise GmailAuthorizationError("Google returned no usable mailbox authorization.")
        rotated = body.get("refresh_token")
        return GmailTokenGrant(
            access_token=access_token,
            refresh_token=rotated if isinstance(rotated, str) and rotated else None,
            expires_in=_expires_in(body),
            granted_scopes=_scopes_from(body),
            claims=None,
        )

    def revoke(self, *, token: str) -> None:
        """Ask Google to invalidate the grant. Best effort, by design.

        A revocation that fails must not stop VMR forgetting the token: the
        local state is the one this application controls, and leaving a
        decryptable refresh token behind because Google was briefly unreachable
        would be the worse outcome. The caller therefore treats every failure
        here as non-fatal and says so to the operator.
        """

        timeout = self._settings.request_timeout_seconds
        try:
            if self._client is not None:
                self._client.post(
                    self._settings.revocation_endpoint, data={"token": token}, timeout=timeout
                )
            else:
                with httpx.Client(timeout=timeout) as client:
                    client.post(self._settings.revocation_endpoint, data={"token": token})
        except httpx.HTTPError as exc:
            raise GmailAuthorizationError("Google could not be reached to revoke access.") from exc


def _expires_in(body: dict[str, Any]) -> int:
    value = body.get("expires_in")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        # A missing or nonsensical lifetime is treated as the shortest sane one
        # rather than as forever. Being early about a refresh costs one request;
        # being late costs a failed operator action.
        return 300
    return value


def _run_sync(awaitable: Any) -> Any:
    """Run one coroutine from synchronous code.

    ``verify_id_token`` is async because the identity sign-in path is, and this
    module is synchronous because every Gmail route and the draft service are.
    Reimplementing JWKS verification synchronously would mean two verifiers for
    one security property, which is how the weaker one eventually gets used.
    Running the existing one on a private event loop keeps exactly one.

    Safe because ``JwksClient`` holds no loop-bound state when it owns its own
    transport: it opens and closes an ``httpx.AsyncClient`` inside the
    coroutine, and its cache holds parsed RSA public keys, which belong to no
    loop. The production path never injects a client, so nothing here is
    carried between the private loops these calls create.
    """

    import asyncio

    return asyncio.run(awaitable)


def verify_mailbox_identity(
    grant: GmailTokenGrant, *, settings: GmailSettings, expected_nonce: str, now: int
) -> tuple[str, str]:
    """Return ``(mailbox_address, google_account_subject)`` for a verified grant.

    Applies the same provider-independent claim rules the sign-in path applies,
    against the *Gmail* client id. It deliberately does not consult the operator
    allow-list: the mailbox an operator drafts from is not required to be their
    VMR sign-in address, and refusing that would be a policy nobody asked for.
    """

    claims = grant.claims
    if claims is None:
        raise GmailAuthorizationError("Google did not say which mailbox was connected.")
    try:
        address = validate_identity_claims(
            claims,
            client_id=settings.client_id or "",
            accepted_issuers=GOOGLE_ISSUERS,
            expected_nonce=expected_nonce,
            now=now,
        )
    except IdentityAssertionError as exc:
        raise GmailAuthorizationError("Google's mailbox assertion did not verify.") from exc
    return address, claims.subject
