"""RS256 verification of a Google ID token against the published JWKS.

Why this exists rather than an unverified decode
------------------------------------------------
Even in the authorization-code flow, where the ID token arrives over a direct
TLS channel, trusting a decoded-but-unverified JWT makes the security of the
sign-in depend on a property that is invisible in the code and easy to lose in a
later refactor — the moment anyone accepts an assertion from anywhere else, the
whole boundary silently becomes forgeable. Verifying the signature makes the
guarantee local, explicit and testable.

The attacks this module is written against
------------------------------------------
* **Algorithm confusion.** ``alg`` is compared against exactly one accepted
  value. There is no algorithm table, no HMAC branch, and no ``none`` branch, so
  an attacker cannot select a verification path in which the public key is used
  as a shared secret.
* **Key confusion.** The key is chosen by an exact ``kid`` match against keys
  fetched from Google over TLS. A JWK embedded in the token header is ignored —
  it is never read at all.
* **Unknown-key flooding.** An unknown ``kid`` triggers at most one refresh, and
  refreshes are rate-limited, so a stream of forged tokens cannot turn into a
  stream of outbound requests.
* **Resource exhaustion.** The JWKS response, its key count and the token itself
  are all size-bounded before anything is parsed.

Verification proves *who signed the token*. The separate claim rules in
``identity.validate_identity_claims`` prove *that the token is for us, for this
sign-in, and still fresh*. Both are required; neither is sufficient.
"""

from __future__ import annotations

import base64
import binascii
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.core.auth.identity import IdentityAssertionError

# Google's published signing keys for ID tokens.
GOOGLE_JWKS_URI = "https://www.googleapis.com/oauth2/v3/certs"

# The one accepted signature algorithm. Google signs ID tokens with RS256.
ACCEPTED_ALGORITHM = "RS256"

MAX_JWKS_BYTES = 128 * 1024
MAX_JWKS_KEYS = 16
MAX_JWT_CHARS = 8192

# Bounds on how long a fetched key set is reused, independent of what the
# provider's cache headers claim.
MIN_CACHE_SECONDS = 300
MAX_CACHE_SECONDS = 24 * 60 * 60
# Floor between two refreshes triggered by an unknown `kid`.
MIN_REFRESH_INTERVAL_SECONDS = 30


def _b64url_decode(segment: str) -> bytes:
    padding_chars = "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(segment + padding_chars)
    except (binascii.Error, ValueError) as exc:
        raise IdentityAssertionError("identity assertion is not valid base64url") from exc


def _b64url_uint(value: str) -> int:
    raw = _b64url_decode(value)
    if not raw:
        raise IdentityAssertionError("identity provider key is malformed")
    return int.from_bytes(raw, "big")


def public_key_from_jwk(jwk: dict[str, Any]) -> rsa.RSAPublicKey:
    """Build an RSA public key from one JWKS entry, refusing anything else."""

    if jwk.get("kty") != "RSA":
        raise IdentityAssertionError("identity provider key is not an RSA key")
    if jwk.get("use") not in (None, "sig"):
        raise IdentityAssertionError("identity provider key is not a signing key")
    if jwk.get("alg") not in (None, ACCEPTED_ALGORITHM):
        raise IdentityAssertionError("identity provider key is for a different algorithm")
    modulus = jwk.get("n")
    exponent = jwk.get("e")
    if not isinstance(modulus, str) or not isinstance(exponent, str):
        raise IdentityAssertionError("identity provider key is malformed")
    numbers = rsa.RSAPublicNumbers(e=_b64url_uint(exponent), n=_b64url_uint(modulus))
    try:
        key = numbers.public_key()
    except ValueError as exc:
        raise IdentityAssertionError("identity provider key is malformed") from exc
    if key.key_size < 2048:
        # A short modulus is either a broken provider or a downgrade attempt.
        raise IdentityAssertionError("identity provider key is too short to trust")
    return key


def parse_jwks(document: Any) -> dict[str, rsa.RSAPublicKey]:
    """Turn a JWKS document into a ``kid`` -> key map, bounded and strict."""

    if not isinstance(document, dict):
        raise IdentityAssertionError("identity provider key set is malformed")
    keys = document.get("keys")
    if not isinstance(keys, list) or not keys:
        raise IdentityAssertionError("identity provider key set is empty")
    if len(keys) > MAX_JWKS_KEYS:
        raise IdentityAssertionError("identity provider key set is implausibly large")
    parsed: dict[str, rsa.RSAPublicKey] = {}
    for entry in keys:
        if not isinstance(entry, dict):
            continue
        kid = entry.get("kid")
        if not isinstance(kid, str) or not kid or kid in parsed:
            continue
        if entry.get("kty") != "RSA":
            # Google publishes only RSA signing keys here; skipping anything
            # else is forward-compatible without widening what is accepted.
            continue
        parsed[kid] = public_key_from_jwk(entry)
    if not parsed:
        raise IdentityAssertionError("identity provider key set has no usable RSA key")
    return parsed


@dataclass
class _CachedKeys:
    keys: dict[str, rsa.RSAPublicKey]
    expires_at: float
    fetched_at: float


class JwksClient:
    """Fetches and caches the provider's signing keys.

    The cache is per-process and in memory. Losing it costs one HTTPS request,
    so there is no persistence, no shared store and nothing to invalidate on
    deploy.
    """

    def __init__(
        self,
        *,
        jwks_uri: str = GOOGLE_JWKS_URI,
        timeout_seconds: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._jwks_uri = jwks_uri
        self._timeout_seconds = timeout_seconds
        self._client = client
        self._cache: _CachedKeys | None = None

    async def key_for(self, kid: str, *, now: float | None = None) -> rsa.RSAPublicKey:
        """The signing key for ``kid``, refreshing once if it is not yet known."""

        moment = time.monotonic() if now is None else now
        cache = self._cache
        if cache is not None and moment < cache.expires_at:
            key = cache.keys.get(kid)
            if key is not None:
                return key
            # Unknown `kid` on a live cache means either a legitimate key
            # rotation or a forged token. One rate-limited refresh distinguishes
            # them without letting forgeries drive outbound traffic.
            if moment - cache.fetched_at < MIN_REFRESH_INTERVAL_SECONDS:
                raise IdentityAssertionError("identity assertion names an unknown signing key")

        keys = await self._fetch(moment)
        key = keys.get(kid)
        if key is None:
            raise IdentityAssertionError("identity assertion names an unknown signing key")
        return key

    async def _fetch(self, moment: float) -> dict[str, rsa.RSAPublicKey]:
        try:
            if self._client is not None:
                response = await self._client.get(self._jwks_uri, timeout=self._timeout_seconds)
            else:
                async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                    response = await client.get(self._jwks_uri)
        except httpx.HTTPError as exc:
            raise IdentityAssertionError(
                "the identity provider key set could not be retrieved"
            ) from exc

        if response.status_code != 200:
            raise IdentityAssertionError("the identity provider key set is unavailable")
        if len(response.content) > MAX_JWKS_BYTES:
            raise IdentityAssertionError("the identity provider key set is implausibly large")
        try:
            document = json.loads(response.content)
        except ValueError as exc:
            raise IdentityAssertionError("the identity provider key set is malformed") from exc

        keys = parse_jwks(document)
        ttl = _cache_seconds(response.headers.get("cache-control"))
        self._cache = _CachedKeys(keys=keys, expires_at=moment + ttl, fetched_at=moment)
        return keys


def _cache_seconds(cache_control: str | None) -> float:
    """How long to reuse a key set, clamped to a sane band."""

    if not cache_control:
        return MIN_CACHE_SECONDS
    for directive in cache_control.split(","):
        name, separator, value = directive.strip().partition("=")
        if separator and name.strip().lower() == "max-age":
            try:
                seconds = int(value.strip())
            except ValueError:
                break
            return float(min(max(seconds, MIN_CACHE_SECONDS), MAX_CACHE_SECONDS))
    return MIN_CACHE_SECONDS


def split_jwt(token: str) -> tuple[str, str, str]:
    """Split a compact-serialisation JWT, refusing anything that is not one."""

    if not token or len(token) > MAX_JWT_CHARS:
        raise IdentityAssertionError("identity assertion is absent or oversized")
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        raise IdentityAssertionError("identity assertion is not a compact JWT")
    return parts[0], parts[1], parts[2]


def read_header(encoded_header: str) -> dict[str, Any]:
    """Parse and vet the JOSE header before any key work happens."""

    try:
        header = json.loads(_b64url_decode(encoded_header))
    except (ValueError, UnicodeDecodeError) as exc:
        raise IdentityAssertionError("identity assertion header is not valid JSON") from exc
    if not isinstance(header, dict):
        raise IdentityAssertionError("identity assertion header is not an object")

    algorithm = header.get("alg")
    if algorithm != ACCEPTED_ALGORITHM:
        # Includes `none`, every HMAC variant, and every other asymmetric
        # family. One accepted value, compared by equality.
        raise IdentityAssertionError("identity assertion uses an unaccepted signature algorithm")
    if header.get("typ") not in (None, "JWT", "jwt"):
        raise IdentityAssertionError("identity assertion is not a JWT")
    if any(key in header for key in ("jwk", "jku", "x5u", "x5c")):
        # A token must not be allowed to nominate its own verification key or a
        # URL to fetch one from.
        raise IdentityAssertionError("identity assertion may not supply its own signing key")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise IdentityAssertionError("identity assertion does not name a signing key")
    header["kid"] = kid
    return header


def verify_signature(*, signing_input: str, signature: str, key: rsa.RSAPublicKey) -> None:
    """Verify RS256 (RSASSA-PKCS1-v1_5 with SHA-256) or raise."""

    try:
        key.verify(
            _b64url_decode(signature),
            signing_input.encode("ascii"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except InvalidSignature as exc:
        raise IdentityAssertionError("identity assertion signature does not verify") from exc


async def verify_id_token(token: str, *, jwks: JwksClient) -> dict[str, Any]:
    """Return the verified claim set of a Google ID token.

    Order matters: the header is vetted, then the key is selected by ``kid``,
    then the signature is verified, and only then is the payload parsed. Nothing
    downstream ever sees the claims of a token whose signature did not verify.
    """

    encoded_header, encoded_payload, signature = split_jwt(token)
    header = read_header(encoded_header)
    key = await jwks.key_for(header["kid"])
    verify_signature(
        signing_input=f"{encoded_header}.{encoded_payload}", signature=signature, key=key
    )
    try:
        payload = json.loads(_b64url_decode(encoded_payload))
    except (ValueError, UnicodeDecodeError) as exc:
        raise IdentityAssertionError("identity assertion payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise IdentityAssertionError("identity assertion payload is not an object")
    return payload
