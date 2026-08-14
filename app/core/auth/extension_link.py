"""Account-linked extension authorization: tokens, PKCE, and the directory seam.

What this module is, stated before what it does
-----------------------------------------------
It is the replacement for a human pasting ``vmrx1.<key_id>.<secret>`` into a
browser. Nothing else about the extension's authority changes: a linked
extension reaches exactly the four routes enumerated in
``EXTENSION_CAPTURE_CONTRACT`` (``app/core/auth/extension.py``) and no others,
and that table remains the single source of truth for the boundary. This module
answers a narrower question — *which VMR account is this capture for, and is that
account still allowed to make it* — which configuration could never answer.

The shape
---------
A first-party PKCE authorization-code flow against the hosted VMR app, with two
opaque, database-backed, rotating tokens. No JWT: a token here is an index into a
row plus a secret, so revocation is a column and not a denylist, and a leaked
token carries no claims because it carries nothing at all.

``vmre1.<session id, 32 hex>.<43-character secret>``
    The access token. Fifteen minutes. Presented in ``Authorization: Bearer``.

``vmrr1.<session id, 32 hex>.<43-character secret>``
    The refresh token. Thirty days, sliding, and **rotated on every use**.

The server stores ``sha256(secret)`` and never the secret, and compares digests
with :func:`hmac.compare_digest`. The middle segment is not a secret — it is what
lets one row be found without scanning — and it is the only part of a token that
may appear in a log line.

Three rules that are the whole security argument
------------------------------------------------
1. **Parsing never raises.** Every function that reads an attacker-controlled
   string returns ``None`` for every shape it did not mint, exactly like
   ``parse_presented_credential``. A boundary that raises on malformed input is a
   boundary that answers 500 instead of doing its job.
2. **Everything fails closed.** A missing directory, an unreachable database, an
   expired row, a revoked row, a digest mismatch, an inactive owner: refuse. The
   database outage is the interesting one, because it is the case where "unknown"
   is tempting to read as "probably fine".
3. **A refresh secret that does not match a live row revokes that row.** The only
   way to hold a superseded refresh secret for a live link is to have copied it,
   so the link — the whole family — dies and the operator has to reconnect.

The refusal vocabulary is deliberately tiny. Callers get ``invalid_grant``,
``invalid_request`` or ``unauthorized`` and never learn whether a code was
unknown, expired, already used, issued to a different install, or presented with
the wrong verifier. Nothing in this module ever returns a secret it was given.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.types import Scope

from app.core.auth.extension import (
    ExtensionAuthSettings,
    capture_origin_permitted,
    presented_authorization_header,
    single_request_origin,
)
from app.models.enums import UserState
from app.models.extension_session import (
    EXTENSION_SCOPE_CAPTURE,
    ExtensionAuthorizationCode,
    ExtensionSession,
)
from app.models.user import User

__all__ = [
    "ACCESS_TOKEN_SCHEME",
    "ACCESS_TOKEN_TTL_SECONDS",
    "AUTHORIZATION_CODE_TTL_SECONDS",
    "REFRESH_TOKEN_SCHEME",
    "REFRESH_TOKEN_TTL_SECONDS",
    "DatabaseExtensionLinkDirectory",
    "ExtensionLinkDirectory",
    "ExtensionLinkResolution",
    "ExtensionLinkUnavailable",
    "IssuedLink",
    "authorize_capture_request",
    "default_extension_link_directory",
    "exchange_authorization_code",
    "issue_authorization_code",
    "live_link_for",
    "parse_link_token",
    "redirect_uri_for",
    "revoke_link_for_session",
    "revoke_links_for_user",
    "rotate_refresh_token",
    "verify_code_challenge",
]

# --- Formats -----------------------------------------------------------------

#: Versioned, and deliberately not ``vmrx1``. A token minted under one scheme must
#: never be readable under another's rules, and the two schemes have entirely
#: different authorities: ``vmrx1`` names a configured key id, ``vmre1`` names a
#: row that names an account.
ACCESS_TOKEN_SCHEME = "vmre1"
REFRESH_TOKEN_SCHEME = "vmrr1"

_TOKEN_PARTS = 3

#: ``secrets.token_urlsafe(32)`` is 43 characters. The floor refuses a hand-typed
#: string; the ceiling refuses a megabyte of attacker-supplied header being
#: hashed before it is rejected.
_SECRET_BYTES = 32
MIN_TOKEN_SECRET_CHARS = 43
MAX_TOKEN_SECRET_CHARS = 64
MAX_TOKEN_CHARS = 256

_SESSION_ID_HEX = re.compile(r"^[0-9a-f]{32}$")
_URLSAFE_SECRET = re.compile(
    rf"^[A-Za-z0-9_-]{{{MIN_TOKEN_SECRET_CHARS},{MAX_TOKEN_SECRET_CHARS}}}$"
)

#: A PKCE ``S256`` challenge is the base64url-without-padding SHA-256 of the
#: verifier, which is always exactly 43 characters. Pinning the length rather
#: than accepting "some base64url" is what stops a client sending a short,
#: guessable challenge.
_CODE_CHALLENGE = re.compile(r"^[A-Za-z0-9_-]{43}$")
#: RFC 7636 bounds the verifier at 43-128 characters from the unreserved set.
_CODE_VERIFIER = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
CODE_CHALLENGE_METHOD = "S256"

#: The extension's own opaque per-install identifier. Bounded and charset-limited
#: because it is stored, compared and echoed into an audit trail; it is never a
#: secret and never authorises anything on its own.
_INSTALLATION_ID = re.compile(r"^[A-Za-z0-9._:-]{8,64}$")

#: Anti-CSRF value chosen by the extension and echoed back untouched. Constrained
#: to base64url so it can be placed in a redirect URL without any chance of the
#: value itself changing the URL's meaning.
_STATE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")

#: The only redirect destination a Chrome extension can complete a web auth flow
#: on. ``chrome.identity.getRedirectURL()`` returns exactly this string, and the
#: check below is equality against it rather than a pattern with a wildcard: an
#: open redirect next to an authorization endpoint is an account takeover.
_REDIRECT_HOST_SUFFIX = ".chromiumapp.org"

# --- Lifetimes ---------------------------------------------------------------

#: Fifteen minutes. A leaked access token is worthless within fifteen minutes,
#: which is what makes an opaque token with no denylist an honest design.
ACCESS_TOKEN_TTL_SECONDS = 900
#: Thirty days, slid forward on every rotation. Long enough that a browser
#: restart, a weekend and a holiday do not cost a sign-in; short enough that an
#: install nobody uses stops working on its own.
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60
#: Sixty seconds, single use. The code exists only for the moment between the
#: redirect landing and the extension redeeming it.
AUTHORIZATION_CODE_TTL_SECONDS = 60


class ExtensionLinkUnavailable(RuntimeError):
    """Raised when the link directory could not be consulted at all.

    Distinct from "no such link" for the same reason
    :class:`app.core.auth.accounts.AccountLookupUnavailable` is: this one means
    *unknown*, and the boundary answers it with a refusal rather than with an
    acceptance. It is never used to mean "refused".
    """


@dataclass(frozen=True)
class ExtensionLinkResolution:
    """One verified access token, resolved to the account behind it."""

    session_id: uuid.UUID
    user_id: uuid.UUID
    extension_id: str
    installation_id: str
    scope: str

    @property
    def key_id(self) -> str:
        """The non-secret label the middleware records for this request.

        Deliberately the session id and nothing else: it names one revocable
        link, it is already public in the token's middle segment, and it is safe
        in a log line. It is prefixed so that a reader can never mistake it for a
        configured ``vmrx1`` key id, which is a different kind of thing.
        """

        return f"link:{self.session_id.hex}"


@dataclass(frozen=True)
class IssuedLink:
    """The pair of tokens handed to the extension, plus what it shows a human."""

    session_id: uuid.UUID
    access_token: str
    refresh_token: str
    expires_in: int
    scope: str
    account_email: str


# --- Primitives --------------------------------------------------------------


def token_digest(secret: str) -> str:
    """The stored form of one token secret."""

    return hashlib.sha256(secret.encode("utf-8", "surrogatepass")).hexdigest()


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def new_token_secret() -> str:
    return secrets.token_urlsafe(_SECRET_BYTES)


def format_token(scheme: str, session_id: uuid.UUID, secret: str) -> str:
    return f"{scheme}.{session_id.hex}.{secret}"


@dataclass(frozen=True)
class ParsedLinkToken:
    """A token that had the right shape. It has not been verified against a row."""

    session_id: uuid.UUID
    secret: str


def parse_link_token(raw: str | None, *, scheme: str) -> ParsedLinkToken | None:
    """Split a presented token into ``(session_id, secret)``, or ``None``.

    Never raises, for every shape of hostile input: a bare ``.``, a token from
    the other scheme, a 5MB header, a UUID with hyphens, a non-ASCII byte. The
    discipline is ``parse_presented_credential``'s and the reason is the same —
    this value is entirely attacker-controlled text.
    """

    if not raw:
        return None
    candidate = raw.strip()
    if len(candidate) > MAX_TOKEN_CHARS or not candidate.isascii():
        return None
    parts = candidate.split(".")
    if len(parts) != _TOKEN_PARTS:
        return None
    presented_scheme, session_hex, secret = parts
    if presented_scheme != scheme:
        return None
    if not _SESSION_ID_HEX.match(session_hex) or not _URLSAFE_SECRET.match(secret):
        return None
    try:
        session_id = uuid.UUID(hex=session_hex)
    except ValueError:  # pragma: no cover - the pattern above already refuses these
        return None
    return ParsedLinkToken(session_id=session_id, secret=secret)


def parse_presented_access_token(raw: str | None) -> ParsedLinkToken | None:
    """The ``vmre1`` token inside an ``Authorization`` header value, or ``None``."""

    if not raw or len(raw) > MAX_TOKEN_CHARS or not raw.isascii():
        return None
    scheme, separator, token = raw.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return None
    return parse_link_token(token.strip(), scheme=ACCESS_TOKEN_SCHEME)


def verify_code_challenge(*, code_verifier: str, code_challenge: str) -> bool:
    """Whether ``base64url(sha256(verifier))`` is the recorded challenge.

    Constant-time on the comparison. The verifier is checked for shape first so
    that a malformed one is a refusal rather than an exception, and so that a
    caller cannot make this function hash an unbounded string.
    """

    if not code_verifier or not code_verifier.isascii():
        return False
    if not _CODE_VERIFIER.match(code_verifier):
        return False
    if not _CODE_CHALLENGE.match(code_challenge or ""):
        return False
    computed = _b64url(hashlib.sha256(code_verifier.encode("ascii")).digest())
    return hmac.compare_digest(computed, code_challenge)


def is_valid_code_challenge(value: str | None) -> bool:
    return bool(value and value.isascii() and _CODE_CHALLENGE.match(value))


def is_valid_installation_id(value: str | None) -> bool:
    return bool(value and value.isascii() and _INSTALLATION_ID.match(value))


def is_valid_state(value: str | None) -> bool:
    return bool(value and value.isascii() and _STATE.match(value))


def redirect_uri_for(extension_id: str) -> str:
    """The one destination an authorization for ``extension_id`` may return to."""

    return f"https://{extension_id}{_REDIRECT_HOST_SUFFIX}/"


def is_exact_redirect_uri(*, presented: str | None, extension_id: str) -> bool:
    """Exact match, never a prefix rule and never a pattern.

    ``https://<id>.chromiumapp.org/`` is what ``chrome.identity.getRedirectURL()``
    produces, so a real extension always sends exactly this. Anything else — an
    extra path segment, a query string, a different id, a lookalike host — is
    refused, because the value decides where an authorization code is delivered.
    """

    if not presented or not presented.isascii() or len(presented) > 255:
        return False
    return hmac.compare_digest(presented, redirect_uri_for(extension_id))


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(moment: datetime) -> datetime:
    """Postgres returns aware datetimes; a test seeding a naive one still compares."""

    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


# --- The middleware seam -----------------------------------------------------


class ExtensionLinkDirectory(Protocol):
    """The one question the authentication boundary asks about a link.

    A protocol for the same reason :class:`app.core.auth.accounts.AccountDirectory`
    is one: the boundary must be testable without a database, and the live
    implementation must never be constructed at import time.
    """

    def resolve_access_token(self, presented: str | None) -> ExtensionLinkResolution | None:
        """The link a presented ``Authorization`` value names, or ``None``.

        Implementations must raise :class:`ExtensionLinkUnavailable` when the
        answer is unknown, and must never return ``None`` to mean "could not
        tell": the two are handled differently, and conflating them would turn a
        database blip into a silent acceptance or a signed-out extension.
        """


class DatabaseExtensionLinkDirectory:
    """The live directory, reading one row per presented access token."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def resolve_access_token(self, presented: str | None) -> ExtensionLinkResolution | None:
        parsed = parse_presented_access_token(presented)
        if parsed is None:
            # Not a token this scheme minted. No database work, so a malformed
            # header never becomes a query.
            return None
        try:
            with self._session_factory() as session:
                resolution = _resolve_parsed_access_token(session, parsed)
                session.commit()
                return resolution
        except Exception as exc:  # noqa: BLE001 - every driver error is one outcome
            # Deliberately broad, exactly like the account directory: a dropped
            # socket, an authentication failure and a missing table all mean the
            # same thing here, which is that this request cannot be decided.
            raise ExtensionLinkUnavailable("the extension link directory is unavailable") from exc


def _resolve_parsed_access_token(
    session: Session, parsed: ParsedLinkToken
) -> ExtensionLinkResolution | None:
    """Every condition, in the order that leaks the least.

    Revocation and expiry are checked before the digest so that a revoked link
    cannot be resurrected by a valid secret, and the owning account is re-read on
    every request so that disabling somebody ends their extension's authority on
    its next call rather than at the next expiry.
    """

    row = session.get(ExtensionSession, parsed.session_id)
    if row is None or row.revoked_at is not None:
        return None
    now = _now()
    if _aware(row.access_token_expires_at) <= now:
        return None
    if not hmac.compare_digest(row.access_token_hash, token_digest(parsed.secret)):
        return None
    owner = session.get(User, row.user_id)
    if owner is None or owner.state != UserState.ACTIVE:
        return None
    row.last_used_at = now
    return ExtensionLinkResolution(
        session_id=row.id,
        user_id=row.user_id,
        extension_id=row.extension_id,
        installation_id=row.installation_id,
        scope=row.scope,
    )


def default_extension_link_directory() -> DatabaseExtensionLinkDirectory:
    """The directory bound to the application's own session factory.

    Imported lazily by the caller so that importing the auth package never opens
    a database connection as a side effect.
    """

    from app.db.session import SessionLocal

    return DatabaseExtensionLinkDirectory(SessionLocal)


def authorize_capture_request(
    scope: Scope,
    settings: ExtensionAuthSettings,
    directory: ExtensionLinkDirectory | None,
    *,
    method: str,
) -> ExtensionLinkResolution | None:
    """The link authorising this capture request, or ``None``.

    The contract check is the caller's — this function is only ever reached for a
    request already inside ``EXTENSION_CAPTURE_CONTRACT``. What it adds is the
    three conditions a linked token has that a configured credential does not:

    1. Account linking is switched on for this deployment.
    2. The request's ``Origin`` satisfies the same rule a ``vmrx1`` capture must
       satisfy — approved exactly, absent only on a safe method.
    3. The token resolves to a live, unexpired, unrevoked row whose owner is
       still active **and whose extension id is the one the origin names**. That
       last clause is what stops a token minted for one approved install being
       replayed by another approved install.
    """

    if not settings.link_enabled:
        return None
    if directory is None:
        # A seam that was never bound cannot decide anything, and "cannot decide"
        # is a refusal here rather than a pass.
        return None
    if not capture_origin_permitted(scope, settings, method=method):
        return None

    resolution = directory.resolve_access_token(presented_authorization_header(scope))
    if resolution is None:
        return None
    if resolution.scope != EXTENSION_SCOPE_CAPTURE:
        return None

    origin = single_request_origin(scope)
    if origin is not None:
        # `is_allowed_origin` tolerates one trailing slash, so the same
        # normalisation is applied here rather than letting a trailing slash
        # silently fail a comparison the origin check already passed.
        if not hmac.compare_digest(
            origin.rstrip("/"), f"chrome-extension://{resolution.extension_id}"
        ):
            return None
    return resolution


# --- Issuance ----------------------------------------------------------------


def live_link_for(
    session: Session, *, user_id: uuid.UUID, extension_id: str, installation_id: str
) -> ExtensionSession | None:
    """The one live link for this account and install, if there is one."""

    return session.scalar(
        select(ExtensionSession).where(
            ExtensionSession.user_id == user_id,
            ExtensionSession.extension_id == extension_id,
            ExtensionSession.installation_id == installation_id,
            ExtensionSession.revoked_at.is_(None),
        )
    )


def issue_authorization_code(
    session: Session,
    *,
    user_id: uuid.UUID,
    extension_id: str,
    installation_id: str,
    code_challenge: str,
    redirect_uri: str,
) -> str:
    """Mint one sixty-second, single-use code and return the raw value.

    The raw code is returned to exactly one caller and stored only as a digest,
    so the row this creates cannot be turned back into a usable code by anybody
    reading the database.
    """

    code = new_token_secret()
    session.add(
        ExtensionAuthorizationCode(
            user_id=user_id,
            code_hash=token_digest(code),
            extension_id=extension_id,
            installation_id=installation_id,
            code_challenge=code_challenge,
            redirect_uri=redirect_uri,
            scope=EXTENSION_SCOPE_CAPTURE,
            expires_at=_now() + timedelta(seconds=AUTHORIZATION_CODE_TTL_SECONDS),
        )
    )
    session.flush()
    return code


def _mint_link(
    session: Session,
    *,
    user: User,
    extension_id: str,
    installation_id: str,
    label: str | None,
) -> IssuedLink:
    """Replace whatever live link this install had, and issue a new pair.

    Revoking first is what keeps the partial unique index satisfiable, and it is
    also the honest behaviour: re-authorizing an install means the previous
    tokens for it stop working, which is what an operator pressing "Reconnect"
    expects and what an attacker replaying an older pair must not survive.
    """

    now = _now()
    existing = live_link_for(
        session, user_id=user.id, extension_id=extension_id, installation_id=installation_id
    )
    if existing is not None:
        existing.revoked_at = now
        existing.revoked_reason = "replaced_by_new_authorization"
        session.flush()

    access_secret = new_token_secret()
    refresh_secret = new_token_secret()
    row = ExtensionSession(
        user_id=user.id,
        extension_id=extension_id,
        installation_id=installation_id,
        scope=EXTENSION_SCOPE_CAPTURE,
        access_token_hash=token_digest(access_secret),
        access_token_expires_at=now + timedelta(seconds=ACCESS_TOKEN_TTL_SECONDS),
        refresh_token_hash=token_digest(refresh_secret),
        refresh_token_expires_at=now + timedelta(seconds=REFRESH_TOKEN_TTL_SECONDS),
        label=label,
        last_used_at=now,
    )
    session.add(row)
    session.flush()
    return IssuedLink(
        session_id=row.id,
        access_token=format_token(ACCESS_TOKEN_SCHEME, row.id, access_secret),
        refresh_token=format_token(REFRESH_TOKEN_SCHEME, row.id, refresh_secret),
        expires_in=ACCESS_TOKEN_TTL_SECONDS,
        scope=EXTENSION_SCOPE_CAPTURE,
        account_email=user.email_normalized,
    )


def exchange_authorization_code(
    session: Session,
    *,
    code: str,
    code_verifier: str,
    extension_id: str,
    installation_id: str,
    label: str | None = None,
) -> IssuedLink | None:
    """Redeem one code for a link, or ``None`` for every failure.

    One outcome for unknown, expired, already-used, wrong-install, wrong-verifier
    and disabled-owner, because a caller must not be able to tell them apart. The
    code is consumed on *any* presentation that names a real row, so a stolen
    code cannot be used to grind at the verifier.
    """

    if not code or not code.isascii() or len(code) > MAX_TOKEN_SECRET_CHARS:
        return None
    row = session.scalar(
        select(ExtensionAuthorizationCode).where(
            ExtensionAuthorizationCode.code_hash == token_digest(code)
        )
    )
    if row is None:
        return None

    now = _now()
    already_used = row.consumed_at is not None
    # Single use, whatever happens next.
    row.consumed_at = row.consumed_at or now
    session.flush()

    if already_used or _aware(row.expires_at) <= now:
        return None
    if not hmac.compare_digest(row.extension_id, extension_id):
        return None
    if not hmac.compare_digest(row.installation_id, installation_id):
        return None
    if not verify_code_challenge(code_verifier=code_verifier, code_challenge=row.code_challenge):
        return None

    owner = session.get(User, row.user_id)
    if owner is None or owner.state != UserState.ACTIVE:
        return None
    return _mint_link(
        session,
        user=owner,
        extension_id=row.extension_id,
        installation_id=row.installation_id,
        label=label,
    )


def rotate_refresh_token(
    session: Session,
    *,
    refresh_token: str,
    extension_id: str,
    installation_id: str,
) -> IssuedLink | None:
    """Rotate both tokens, or refuse — and revoke the link on a detected reuse.

    Reuse detection is the point of rotating. A refresh secret that names a live
    row but does not match its current digest is a superseded secret, and the
    only way to hold one is to have copied it before it was rotated. The link is
    therefore revoked outright rather than merely refused, so the thief and the
    real install both lose it and the operator has to reconnect deliberately.
    """

    parsed = parse_link_token(refresh_token, scheme=REFRESH_TOKEN_SCHEME)
    if parsed is None:
        return None
    row = session.get(ExtensionSession, parsed.session_id)
    if row is None or row.revoked_at is not None:
        return None

    now = _now()
    if not hmac.compare_digest(row.refresh_token_hash, token_digest(parsed.secret)):
        row.revoked_at = now
        row.revoked_reason = "refresh_token_reuse"
        session.flush()
        return None

    if _aware(row.refresh_token_expires_at) <= now:
        return None
    if not hmac.compare_digest(row.extension_id, extension_id):
        return None
    if not hmac.compare_digest(row.installation_id, installation_id):
        return None

    owner = session.get(User, row.user_id)
    if owner is None or owner.state != UserState.ACTIVE:
        return None

    access_secret = new_token_secret()
    refresh_secret = new_token_secret()
    row.access_token_hash = token_digest(access_secret)
    row.access_token_expires_at = now + timedelta(seconds=ACCESS_TOKEN_TTL_SECONDS)
    row.refresh_token_hash = token_digest(refresh_secret)
    # Sliding: an install in daily use never has to sign in again, and one that
    # stops being used expires on its own.
    row.refresh_token_expires_at = now + timedelta(seconds=REFRESH_TOKEN_TTL_SECONDS)
    row.last_used_at = now
    session.flush()
    return IssuedLink(
        session_id=row.id,
        access_token=format_token(ACCESS_TOKEN_SCHEME, row.id, access_secret),
        refresh_token=format_token(REFRESH_TOKEN_SCHEME, row.id, refresh_secret),
        expires_in=ACCESS_TOKEN_TTL_SECONDS,
        scope=row.scope,
        account_email=owner.email_normalized,
    )


def revoke_link_for_session(
    session: Session, *, access_token: str, reason: str = "extension_disconnect"
) -> bool:
    """Revoke the link a presented access token names. ``False`` if it named none.

    The digest is still verified: holding a session id is not authority to end
    somebody else's link, and the id is public in every token's middle segment.
    """

    parsed = parse_link_token(access_token, scheme=ACCESS_TOKEN_SCHEME)
    if parsed is None:
        return False
    row = session.get(ExtensionSession, parsed.session_id)
    if row is None or row.revoked_at is not None:
        return False
    if not hmac.compare_digest(row.access_token_hash, token_digest(parsed.secret)):
        return False
    row.revoked_at = _now()
    row.revoked_reason = reason
    session.flush()
    return True


def revoke_links_for_user(
    session: Session,
    *,
    user_id: uuid.UUID,
    extension_id: str | None = None,
    installation_id: str | None = None,
    reason: str = "operator_disconnect",
) -> int:
    """Revoke an account's own links, optionally narrowed to one install."""

    conditions = [ExtensionSession.user_id == user_id, ExtensionSession.revoked_at.is_(None)]
    if extension_id:
        conditions.append(ExtensionSession.extension_id == extension_id)
    if installation_id:
        conditions.append(ExtensionSession.installation_id == installation_id)
    rows: Sequence[ExtensionSession] = session.scalars(
        select(ExtensionSession).where(*conditions)
    ).all()
    now = _now()
    for row in rows:
        row.revoked_at = now
        row.revoked_reason = reason
    session.flush()
    return len(rows)
