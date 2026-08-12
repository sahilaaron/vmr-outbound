"""The signed, stateless operator session cookie.

Why stateless rather than a database session table
--------------------------------------------------
Three reasons, in order of weight.

1. **Revocation is already strong without one.** Approval is re-checked against
   the configured allow-list on *every* request, not just at sign-in. Removing
   an address from ``AUTH__ALLOWED_OPERATOR_EMAILS`` and restarting therefore
   invalidates that operator's existing session on their next request. A
   session table would add a second, weaker revocation path — not a stronger
   one.
2. **Authentication must not depend on the database.** A readiness blip or a
   migration window would otherwise lock every operator out of the very screens
   they need to diagnose it, and would put a database round trip in front of
   every static asset.
3. **Nothing to leak or migrate.** No table, no cleanup job, no reversible
   migration, and no second copy of an operator's identity at rest.

The accepted cost is stated plainly in the threat analysis: a cookie that is
*stolen* stays valid until its absolute expiry (12 hours by default) or until
the allow-list changes. For an internal Beta with a handful of named operators,
behind HTTPS with ``HttpOnly``/``Secure``/``SameSite=Lax`` cookies, that is the
right trade. It is also the reason the lifetime is absolute with no sliding
renewal: a session cannot be kept alive indefinitely by using it.

Format
------
``v1.<payload>.<signature>`` where ``payload`` is base64url(JSON) and
``signature`` is base64url(HMAC-SHA256) over the ASCII bytes of
``"v1." + payload``. The version prefix is inside the signed material, so it
cannot be downgraded. Signing keys are derived from the configured secret with
separate labels per purpose, so the session key, the CSRF key and the sign-in
transaction key are independent even though one secret configures them.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from typing import Any

SESSION_COOKIE_NAME = "vmr_session"
LOGIN_TRANSACTION_COOKIE_NAME = "vmr_login"

_TOKEN_VERSION = "v1"
# Keys are derived per purpose so that a token minted for one use can never be
# presented as another, even though a single operator secret configures all of
# them.
_SESSION_KEY_LABEL = b"vmr.hosted-auth.session.v1"
_CSRF_KEY_LABEL = b"vmr.hosted-auth.csrf.v1"
_LOGIN_KEY_LABEL = b"vmr.hosted-auth.login-transaction.v1"
# The Gmail mailbox authorization transaction (#267). A key of its own rather
# than a `kind` field inside the login-transaction payload: a discriminator only
# helps the side that checks it, so with one shared key a Gmail transaction
# stays a structurally valid sign-in transaction. A separate derived key makes
# each token verify for exactly one purpose, in both directions, without either
# side having to remember to look.
_GMAIL_KEY_LABEL = b"vmr.gmail-authorization.transaction.v1"

# A bounded ceiling on anything presented as a token. Signature verification is
# cheap, but there is no reason to base64-decode a megabyte of attacker-supplied
# cookie before rejecting it.
MAX_TOKEN_CHARS = 4096


class SessionDecodeError(Exception):
    """Raised when a presented token is absent, malformed, forged or expired.

    One exception type on purpose. A caller must not be able to tell a forged
    signature from an expired session from a truncated cookie, and every call
    site treats all of them identically: no session.
    """


def constant_time_equal(left: str, right: str) -> bool:
    """Constant-time comparison that *refuses* rather than raising.

    ``hmac.compare_digest`` raises ``TypeError`` the moment either ``str``
    argument contains a non-ASCII character. Every value compared on this
    boundary — a session signature, a CSRF token, an audience claim — arrives as
    attacker-controlled text: header and cookie values reach ASGI as
    latin-1-decoded strings, so a single non-ASCII byte on the wire would
    otherwise turn a designed refusal into an unhandled 500 on a security path.

    Comparing the encoded bytes fixes that without changing what is accepted:

    * the comparison stays constant-time, because ``compare_digest`` is;
    * nothing is normalised, folded or transcoded into another representation,
      so an attacker-supplied token can never be turned into a valid one — a
      malformed value simply becomes an ordinary mismatch;
    * ``surrogatepass`` is used so that no input a Python ``str`` can hold, not
      even a lone surrogate from a decoded body, can make the encode step raise.

    The one asymmetry worth naming: byte length is not hidden, and never was.
    ``compare_digest`` short-circuits on unequal lengths by design.
    """

    return hmac.compare_digest(
        left.encode("utf-8", "surrogatepass"), right.encode("utf-8", "surrogatepass")
    )


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except (ValueError, TypeError) as exc:  # pragma: no cover - defensive
        raise SessionDecodeError("token payload is not valid base64url") from exc


def derive_key(secret: str, label: bytes) -> bytes:
    """One purpose-specific key from the configured secret."""

    return hmac.new(secret.encode("utf-8"), label, hashlib.sha256).digest()


@dataclass(frozen=True)
class OperatorSession:
    """The authenticated identity carried by one session cookie.

    ``subject`` is Google's stable account identifier and ``email`` is the
    approved address. Both come from the validated provider assertion; neither
    is ever taken from a request field the client controls.
    """

    email: str
    subject: str
    display_name: str
    session_id: str
    issued_at: int
    expires_at: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "sub": self.subject,
            "name": self.display_name,
            "sid": self.session_id,
            "iat": self.issued_at,
            "exp": self.expires_at,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> OperatorSession:
        try:
            email = payload["email"]
            subject = payload["sub"]
            display_name = payload["name"]
            session_id = payload["sid"]
            issued_at = payload["iat"]
            expires_at = payload["exp"]
        except (KeyError, TypeError) as exc:
            raise SessionDecodeError("session payload is missing required claims") from exc
        if not isinstance(email, str) or not isinstance(subject, str):
            raise SessionDecodeError("session payload has malformed identity claims")
        if not isinstance(display_name, str) or not isinstance(session_id, str):
            raise SessionDecodeError("session payload has malformed identity claims")
        if not isinstance(issued_at, int) or not isinstance(expires_at, int):
            raise SessionDecodeError("session payload has malformed lifetime claims")
        if isinstance(issued_at, bool) or isinstance(expires_at, bool):
            raise SessionDecodeError("session payload has malformed lifetime claims")
        return cls(
            email=email,
            subject=subject,
            display_name=display_name,
            session_id=session_id,
            issued_at=issued_at,
            expires_at=expires_at,
        )


def new_session_id() -> str:
    """A fresh, unguessable session identifier.

    Minted on every successful sign-in, which is what makes login a rotation:
    the identifier — and therefore the derived CSRF token — is different after
    authentication than before, so a token observed against a pre-login state
    is useless afterwards.
    """

    return secrets.token_urlsafe(24)


class SessionCodec:
    """Signs and verifies the session and sign-in-transaction cookies."""

    def __init__(self, secret: str) -> None:
        self._session_key = derive_key(secret, _SESSION_KEY_LABEL)
        self._csrf_key = derive_key(secret, _CSRF_KEY_LABEL)
        self._login_key = derive_key(secret, _LOGIN_KEY_LABEL)
        self._gmail_key = derive_key(secret, _GMAIL_KEY_LABEL)

    # --- generic signed envelope -------------------------------------------

    @staticmethod
    def _sign(key: bytes, payload: str) -> str:
        # `surrogatepass` rather than `ascii`: `_decode` already refuses a
        # non-ASCII token outright, but this function must not be the thing that
        # raises if a future caller ever reaches it with one. Signing bytes that
        # cannot be produced by any legitimately minted token simply yields a
        # signature that matches nothing.
        signed_material = f"{_TOKEN_VERSION}.{payload}".encode("utf-8", "surrogatepass")
        return _b64encode(hmac.new(key, signed_material, hashlib.sha256).digest())

    def _encode(self, key: bytes, payload: dict[str, Any]) -> str:
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        encoded = _b64encode(raw)
        return f"{_TOKEN_VERSION}.{encoded}.{self._sign(key, encoded)}"

    def _decode(self, key: bytes, token: str | None) -> dict[str, Any]:
        if not token or len(token) > MAX_TOKEN_CHARS:
            raise SessionDecodeError("token is absent or oversized")
        if not token.isascii():
            # Every token this codec mints is `v1.<base64url>.<base64url>`, which
            # is ASCII by construction. A non-ASCII byte therefore proves the
            # value was never minted here, and refusing it up front — as a
            # refusal, never an exception — keeps the rest of this function
            # working on the only shape it was written for.
            raise SessionDecodeError("token contains characters no minted token can hold")
        parts = token.split(".")
        if len(parts) != 3:
            raise SessionDecodeError("token is not a three-part signed envelope")
        version, payload, signature = parts
        if version != _TOKEN_VERSION:
            raise SessionDecodeError("token version is not supported")
        expected = self._sign(key, payload)
        # Constant-time: a timing oracle on the signature would let an attacker
        # forge one byte at a time. `constant_time_equal` also keeps a malformed
        # presented signature a mismatch rather than a raised exception.
        if not constant_time_equal(expected, signature):
            raise SessionDecodeError("token signature does not verify")
        try:
            decoded = json.loads(_b64decode(payload))
        except (ValueError, UnicodeDecodeError) as exc:
            raise SessionDecodeError("token payload is not valid JSON") from exc
        if not isinstance(decoded, dict):
            raise SessionDecodeError("token payload is not an object")
        return decoded

    # --- operator session ---------------------------------------------------

    def encode_session(self, session: OperatorSession) -> str:
        return self._encode(self._session_key, session.to_payload())

    def decode_session(self, token: str | None, *, now: int) -> OperatorSession:
        """Verify, then bound. Expiry is checked only after the signature."""

        session = OperatorSession.from_payload(self._decode(self._session_key, token))
        if session.expires_at <= now:
            raise SessionDecodeError("session has expired")
        if session.issued_at > now + 60:
            # A session stamped in the future is either a clock problem or a
            # forgery attempt against a leaked secret; refuse either way.
            raise SessionDecodeError("session was issued in the future")
        return session

    # --- CSRF ---------------------------------------------------------------

    def csrf_token(self, session_id: str) -> str:
        """The CSRF token bound to one session.

        Derived rather than stored, so it needs no second cookie and no server
        state, and it changes when the session identifier changes. It is not
        secret from the operator — it is secret from *other origins*, which is
        exactly what the same-origin policy guarantees for a page an attacker
        cannot read.
        """

        return _b64encode(
            hmac.new(self._csrf_key, session_id.encode("utf-8"), hashlib.sha256).digest()
        )

    def csrf_token_matches(self, session_id: str, presented: str | None) -> bool:
        if not presented or len(presented) > MAX_TOKEN_CHARS:
            return False
        return constant_time_equal(self.csrf_token(session_id), presented)

    # --- sign-in transaction ------------------------------------------------

    def encode_login_transaction(self, payload: dict[str, Any]) -> str:
        return self._encode(self._login_key, payload)

    def decode_login_transaction(self, token: str | None, *, now: int) -> dict[str, Any]:
        payload = self._decode(self._login_key, token)
        expires_at = payload.get("exp")
        if not isinstance(expires_at, int) or isinstance(expires_at, bool):
            raise SessionDecodeError("login transaction has no bounded lifetime")
        if expires_at <= now:
            raise SessionDecodeError("login transaction has expired")
        return payload

    # --- Gmail mailbox authorization transaction (#267) ----------------------

    def encode_gmail_transaction(self, payload: dict[str, Any]) -> str:
        """Sign one Gmail authorization transaction with its own key.

        Separate from the sign-in transaction so that neither can ever be
        presented as the other. The two carry different authority — one is
        minted before there is a session and decides who signs in, the other
        only ever exists after and decides which mailbox is bound to an
        already-approved operator — and a token that verifies for both is one
        substitution away from being a confused deputy.
        """

        return self._encode(self._gmail_key, payload)

    def decode_gmail_transaction(self, token: str | None, *, now: int) -> dict[str, Any]:
        payload = self._decode(self._gmail_key, token)
        expires_at = payload.get("exp")
        if not isinstance(expires_at, int) or isinstance(expires_at, bool):
            raise SessionDecodeError("gmail transaction has no bounded lifetime")
        if expires_at <= now:
            raise SessionDecodeError("gmail transaction has expired")
        return payload
