"""The extension capture credential: the one non-cookie way into this API.

Scope, stated first, because the value of this module is what it refuses
-------------------------------------------------------------------------
This is **not** a general bearer-token mode for the application. A presented
credential authorises exactly the paths and methods enumerated in
``EXTENSION_CAPTURE_CONTRACT`` and nothing else. Every other route — the whole
web UI, every admin surface, every other API — continues to require an approved
operator session cookie, and a valid capture credential presented against one of
them is worth precisely as much as no credential at all.

Why a separate credential at all
--------------------------------
The Chrome extension is not a browser session. It has no sign-in surface, it
cannot complete an OAuth redirect, and it must not be handed the operator's
session cookie: a cookie is ambient, carries the operator's identity, and unlocks
the entire application. The three credentials this deployment holds are therefore
kept apart on purpose, and none of them substitutes for another:

* the **operator session cookie** — a signed browser session for a human;
* the **Google identity client** — used once, at sign-in, to learn who that
  human is;
* the **extension capture credential** — this module: a bearer secret held by
  one extension install, good only for submitting captures.

A future Gmail OAuth grant will be a fourth, and it is not this.

Shape and storage
-----------------
A credential is presented as ``Authorization: Bearer vmrx1.<key_id>.<secret>``.

* ``key_id`` is a short, **non-secret** label. It is what makes a fleet of one
  credential per install possible, it is the only part of a credential that may
  ever appear in a log line, and it is what revocation names.
* ``secret`` is 32+ characters of ``secrets.token_urlsafe``.

The server never holds the secret. Configuration carries
``<key_id>:<sha256-hex-of-secret>``, and verification hashes what was presented
and compares digests in constant time. A plain SHA-256 is the right primitive
here and a password KDF would not be: the input is full-entropy random, not a
memorable phrase, so there is no dictionary to slow down — and the property that
actually matters is that a reader of ``/etc/vmr/vmr.env`` (or a leaked backup, or
a settings dump) learns nothing they can replay.

Revocation
----------
Two paths, both fail-closed, both effective on the next restart:

1. Remove the entry from ``EXTENSION_AUTH__CREDENTIALS``.
2. Name its ``key_id`` in ``EXTENSION_AUTH__REVOKED_KEY_IDS``.

The second exists because it is the safer operation under pressure. Deleting the
right line out of a list is a chance to delete the wrong one, and a revoked id
that is still listed as a credential — a stale copy, a bad merge, a restored
backup — must stay dead. Revocation is therefore checked *before* the digest, so
a revoked key id can never be resurrected by a credential entry that outlived it.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, Field, field_validator
from starlette.types import Scope

# --- The credential format ---------------------------------------------------

# Versioned so a later format can be introduced without a presented token from
# one scheme ever being read under the rules of another.
CREDENTIAL_SCHEME = "vmrx1"
CREDENTIAL_PARTS = 3

# A key id is a label, not a secret: lowercase, bounded, and safe to put in a log
# line or an operator's notes without leaking anything.
_KEY_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")

# 32 characters of `secrets.token_urlsafe(32)` is ~43 characters, so this floor
# refuses a hand-typed short string while accepting the documented recipe.
MIN_SECRET_CHARS = 32

# There is no reason to hash a megabyte of attacker-supplied header before
# rejecting it.
MAX_CREDENTIAL_CHARS = 1024

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")

# Chrome extension ids are exactly 32 characters drawn from `a`-`p`. Matching the
# real shape rather than "anything after chrome-extension://" is what stops a
# typo or a pasted fragment from silently becoming an allow-list entry that no
# real install can match — and stops a longer, attacker-chosen origin string from
# being accepted as one.
_EXTENSION_ORIGIN = re.compile(r"^chrome-extension://[a-p]{32}$")


# --- The enumerated contract -------------------------------------------------

# Exactly the requests the Sales Navigator / LinkedIn capture extension makes to
# save a contact, and nothing beyond them. Each entry is a normalised path mapped
# to the methods a credential may use on it.
#
# What is deliberately absent, and why:
#
# * The legacy campaign-era intakes (`/api/intake/sales-navigator/stage`,
#   `/api/intake/linkedin-profile/stage`). The extension has not produced either
#   contract since 2.0; the routes remain for stored evidence, not for new
#   submissions.
# * The company-page intake (`/api/intake/linkedin-company/stage`). Company
#   evidence is a separate surface from contact capture and is not part of the
#   hosted capture the Beta needs. It stays local-only until it is asked for.
# * Every write other than the capture itself. A credential cannot create a
#   campaign, promote a capture, label an existing contact, or reach any admin
#   route — those are operator decisions and require an operator session.
EXTENSION_CAPTURE_CONTRACT: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        # The capture itself: one reviewed contact-first submission.
        "/api/intake/contact-captures": frozenset({"POST"}),
        # The three reads the panel makes before the operator commits.
        "/api/contact-labels": frozenset({"GET"}),
        "/api/contacts/lookup": frozenset({"GET"}),
        "/api/campaigns": frozenset({"GET"}),
    }
)

# The request headers the extension actually sends. `Authorization` is here
# because the credential travels in it; the other two are the existing capture
# contract. Nothing is added speculatively: an unlisted header makes the
# preflight fail, which is a visible, diagnosable failure rather than a silently
# widened surface.
EXTENSION_REQUEST_HEADERS: tuple[str, ...] = (
    "Authorization",
    "Content-Type",
    "Idempotency-Key",
)

# Ten minutes. Long enough that a capture session does not re-preflight every
# request, short enough that tightening the contract takes effect promptly.
PREFLIGHT_MAX_AGE_SECONDS = 600

# Where the middleware records its finding, and the key the routes read back.
EXTENSION_KEY_ID_STATE_KEY = "extension_capture_key_id"
EXTENSION_CREDENTIAL_LABEL = "extension_capture"


def normalize_contract_path(raw: str) -> str:
    """The single path spelling the contract is written against.

    Deliberately the same collapse ``app.core.auth.policy`` applies, and for the
    same reason: ``//api/intake/contact-captures`` and
    ``/api/intake/./contact-captures`` must not be able to reach a route by a
    spelling this table does not recognise. Normalisation here can only ever make
    a path match *less*, never more — an unrecognised spelling falls through to
    the ordinary anonymous refusal.
    """

    segments: list[str] = []
    for segment in raw.split("/"):
        if not segment or segment == ".":
            continue
        if segment == "..":
            if segments:
                segments.pop()
            continue
        segments.append(segment)
    return "/" + "/".join(segments)


def contract_methods(path: str) -> frozenset[str]:
    """The methods a capture credential may use on ``path`` (empty when none)."""

    return EXTENSION_CAPTURE_CONTRACT.get(normalize_contract_path(path), frozenset())


def is_contract_request(path: str, method: str) -> bool:
    """Whether ``method path`` is inside the enumerated extension contract."""

    return method.upper() in contract_methods(path)


def credential_digest(secret: str) -> str:
    """The stored form of one credential secret."""

    return hashlib.sha256(secret.encode("utf-8", "surrogatepass")).hexdigest()


def parse_presented_credential(raw: str | None) -> tuple[str, str] | None:
    """Split a presented ``Authorization`` value into ``(key_id, secret)``.

    Returns ``None`` — never raises — for every shape that was not minted by this
    scheme. A security boundary that raises on malformed input is a boundary that
    answers 500 instead of doing its job, and the presented value here is
    entirely attacker-controlled text.
    """

    if not raw or len(raw) > MAX_CREDENTIAL_CHARS or not raw.isascii():
        return None
    scheme, separator, token = raw.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return None
    token = token.strip()
    parts = token.split(".")
    if len(parts) != CREDENTIAL_PARTS:
        return None
    version, key_id, secret = parts
    if version != CREDENTIAL_SCHEME:
        return None
    if not _KEY_ID_PATTERN.match(key_id) or len(secret) < MIN_SECRET_CHARS:
        return None
    return key_id, secret


class ExtensionAuthSettings(BaseModel):
    """Extension capture credentials (env prefix ``EXTENSION_AUTH__``).

    Defaults are "off, with nobody configured", so an environment that says
    nothing about extension capture has no extension capture. The startup
    contract in ``app/core/auth/startup.py`` refuses the half-configured states.

    One scope note, because it is otherwise a silent surprise. This boundary is
    read by ``OperatorAuthenticationMiddleware``, which returns early when
    ``AUTH__ENABLED`` is false — so in local development, where hosted
    authentication is off by default, extension credentials are **not enforced**
    and the intake keeps its unchanged local rule (``APP_ENV=local`` plus the
    loopback/extension origin check). That is deliberate: local development has
    no authenticated intake and this slice does not give it one. In staging the
    startup contract requires ``AUTH__ENABLED`` alongside this, so the inert
    combination cannot reach a hosted deployment.
    """

    model_config = {"frozen": True}

    enabled: bool = Field(
        default=False,
        description="Accept extension capture credentials on the enumerated intake contract.",
    )

    # Digests, not secrets — but still excluded from dumps and reprs. A digest is
    # not replayable, and it is also not something that belongs in a log line, a
    # diagnostics screen or a settings dump.
    credentials: tuple[str, ...] = Field(
        default=(),
        repr=False,
        exclude=True,
        description='Issued credentials as "<key_id>:<sha256-hex-of-secret>".',
    )

    revoked_key_ids: tuple[str, ...] = Field(
        default=(),
        description="Key ids refused outright, even if still listed in credentials.",
    )

    allowed_origins: tuple[str, ...] = Field(
        default=(),
        description="Exact chrome-extension:// origins permitted to present a credential.",
    )

    @field_validator("credentials")
    @classmethod
    def _normalize_credentials(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Refuse unusable entries at load time; never echo the offending value.

        Refusing rather than dropping matters for the same reason it does on the
        operator allow-list: an entry that silently does not parse produces a
        deployment that looks healthy and refuses every capture, with nothing to
        distinguish "the credential is wrong" from "the credential was never
        loaded".
        """

        seen: dict[str, str] = {}
        for entry in value:
            key_id, separator, digest = entry.strip().partition(":")
            key_id = key_id.strip().lower()
            digest = digest.strip().lower()
            if not separator or not _KEY_ID_PATTERN.match(key_id) or not _SHA256_HEX.match(digest):
                raise ValueError(
                    'EXTENSION_AUTH__CREDENTIALS entries must be "<key_id>:<sha256-hex-of-secret>"'
                )
            if key_id in seen and seen[key_id] != digest:
                # Two different secrets under one name is an ambiguity nobody
                # should get to resolve by list order.
                raise ValueError(
                    "EXTENSION_AUTH__CREDENTIALS lists one key id twice with different digests"
                )
            seen[key_id] = digest
        return tuple(f"{key_id}:{digest}" for key_id, digest in seen.items())

    @field_validator("revoked_key_ids")
    @classmethod
    def _normalize_revoked(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for entry in value:
            candidate = entry.strip().lower()
            if not _KEY_ID_PATTERN.match(candidate):
                raise ValueError("EXTENSION_AUTH__REVOKED_KEY_IDS must contain only key ids")
            if candidate not in normalized:
                normalized.append(candidate)
        return tuple(normalized)

    @field_validator("allowed_origins")
    @classmethod
    def _normalize_origins(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for entry in value:
            candidate = entry.strip().rstrip("/")
            if not candidate.isascii() or not _EXTENSION_ORIGIN.match(candidate):
                raise ValueError(
                    "EXTENSION_AUTH__ALLOWED_ORIGINS must contain exact "
                    "chrome-extension://<32-character-id> origins"
                )
            if candidate not in normalized:
                normalized.append(candidate)
        return tuple(normalized)

    # --- decisions ----------------------------------------------------------

    def is_configured(self) -> bool:
        """True when this boundary could actually admit a request."""

        return bool(self.enabled and self.credentials and self.allowed_origins)

    def is_allowed_origin(self, origin: str | None) -> bool:
        """Whether ``origin`` is one of the approved extension installs.

        Exact match against the configured list. No prefix rule, no scheme-only
        rule, and no "any chrome-extension origin": the point of the list is that
        one specific install is approved, and every other extension in the
        browser — including one an operator installs tomorrow — is not.
        """

        if not origin:
            return False
        candidate = origin.strip().rstrip("/")
        # Header values reach ASGI latin-1-decoded, so a single non-ASCII byte
        # would make `compare_digest` raise on a `str` argument — turning a
        # designed refusal into a 500 on the security path. Every configured
        # origin is ASCII by construction, so a non-ASCII candidate is simply a
        # mismatch.
        if not candidate.isascii():
            return False
        return any(hmac.compare_digest(candidate, approved) for approved in self.allowed_origins)

    def authenticate(self, presented: str | None) -> str | None:
        """The key id a valid credential names, or ``None``.

        One outcome for every failure. A caller must not be able to tell an
        unknown key id from a wrong secret from a revoked credential from a
        malformed header, and no call site needs to.
        """

        if not self.enabled:
            return None
        parsed = parse_presented_credential(presented)
        if parsed is None:
            return None
        key_id, secret = parsed
        # Revocation first, unconditionally. See the module docstring: a revoked
        # id must stay dead even if a stale credential entry still carries it.
        if key_id in self.revoked_key_ids:
            return None
        digest = credential_digest(secret)
        matched = False
        for entry in self.credentials:
            stored_key_id, _, stored_digest = entry.partition(":")
            if stored_key_id != key_id:
                continue
            # Constant time on the digest comparison. The key id is not secret,
            # so looking it up directly leaks nothing worth having.
            if hmac.compare_digest(stored_digest, digest):
                matched = True
            break
        return key_id if matched else None


# --- Request-level helpers ---------------------------------------------------


def _scope_headers(scope: Scope, name: bytes) -> list[str]:
    return [
        raw_value.decode("latin-1")
        for raw_name, raw_value in scope.get("headers", [])
        if raw_name.lower() == name
    ]


def _single_header(scope: Scope, name: bytes) -> str | None:
    """The one unambiguous value of a header, or ``None``.

    Duplicated headers are ambiguity and ambiguity refuses, exactly as on the
    cookie and origin reads in ``app/core/auth/middleware.py``. A proxy or client
    that sends ``Authorization`` twice must not get to decide which one this
    boundary reads.
    """

    values = _scope_headers(scope, name)
    return values[0] if len(values) == 1 else None


def authenticate_capture_request(
    scope: Scope, settings: ExtensionAuthSettings, *, path: str, method: str
) -> str | None:
    """The key id authorising this request as an extension capture, or ``None``.

    Every condition must hold; there is no partial credit:

    1. The boundary is enabled.
    2. ``method path`` is inside the enumerated contract.
    3. Exactly one ``Authorization`` header, carrying a valid, unrevoked
       credential.
    4. The origin is one of the approved extension installs.

    On the origin rule, condition 4 is stated precisely because the two method
    classes genuinely differ:

    * **Unsafe methods (the capture POST) require an approved ``Origin``.** The
      Fetch standard appends ``Origin`` to every non-GET/HEAD request regardless
      of mode or tainting, so a real capture always carries one. Requiring it is
      therefore free, and it is what makes a stolen credential replayed from
      ``https://evil.example`` fail even though the credential itself verifies.
    * **Safe methods accept an absent ``Origin``, but never a wrong one.** A
      Chrome extension holding a host permission may have its cross-origin GET
      treated as same-origin, and the standard then omits the header. Refusing
      those would break the panel's three reads for a property they cannot
      provide. A *present* origin is still checked, so the arbitrary-web-origin
      case is refused on every method — and these three reads are bounded,
      read-only, and useless without the credential anyway.
    """

    if not settings.enabled:
        return None
    if not is_contract_request(path, method):
        return None

    key_id = settings.authenticate(_single_header(scope, b"authorization"))
    if key_id is None:
        return None

    origins = _scope_headers(scope, b"origin")
    if len(origins) > 1:
        return None
    origin = origins[0].strip() if origins else None
    if origin is None:
        return None if method.upper() not in {"GET", "HEAD", "OPTIONS"} else key_id
    return key_id if settings.is_allowed_origin(origin) else None


def capture_preflight_headers(
    scope: Scope, settings: ExtensionAuthSettings, *, path: str
) -> dict[str, str] | None:
    """CORS headers for one approved preflight, or ``None`` to refuse it.

    This is the *whole* preflight exemption the application grants, and it is
    exactly the narrow, enumerated one ``app/core/auth/policy.py`` describes: a
    fixed list of intake paths, answered with CORS headers, no body, and no
    authentication implication whatsoever. Answering ``OPTIONS`` here says only
    "a request of this shape from this origin would be considered" — the request
    that follows still has to present a credential.

    ``Access-Control-Allow-Credentials`` is deliberately never emitted. The
    extension authenticates with a header it sets itself, so it needs no ambient
    cookie, and a credentialed CORS grant would be a way to reach this API with
    the operator's session instead.
    """

    if not settings.enabled:
        return None
    methods = contract_methods(path)
    if not methods:
        return None

    origins = _scope_headers(scope, b"origin")
    if len(origins) != 1 or not settings.is_allowed_origin(origins[0].strip()):
        return None

    requested = _single_header(scope, b"access-control-request-method")
    if requested is None or requested.strip().upper() not in methods:
        return None

    return {
        "Access-Control-Allow-Origin": origins[0].strip(),
        "Access-Control-Allow-Methods": ", ".join(sorted(methods)),
        "Access-Control-Allow-Headers": ", ".join(EXTENSION_REQUEST_HEADERS),
        "Access-Control-Max-Age": str(PREFLIGHT_MAX_AGE_SECONDS),
        "Vary": "Origin, Access-Control-Request-Method",
    }


def extension_key_id(request: Any) -> str | None:
    """The key id recorded by the middleware for this request, if any."""

    scope = getattr(request, "scope", request)
    state = scope.get("state") or {}
    value = state.get(EXTENSION_KEY_ID_STATE_KEY)
    return value if isinstance(value, str) and value else None


def is_extension_capture_request(request: Any) -> bool:
    """Whether this request was authorised by an extension capture credential.

    The routes call this instead of re-reading the header, so there is exactly
    one place that decides, and so a request carrying only a session cookie can
    never be mistaken for an extension request: the middleware records a key id
    only when a credential actually verified.
    """

    return extension_key_id(request) is not None
