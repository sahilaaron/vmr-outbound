"""Deterministic Google-identity fixtures for the hosted-auth tests.

Real RSA keys and real RS256 signatures, produced locally. That matters: a stub
that returns pre-baked claims would exercise the routes but prove nothing about
the verification code, and the verification code is the part an attacker attacks.
Here the tests mint tokens with a key the suite controls, serve a JWKS through an
``httpx.MockTransport``, and can therefore forge exactly the tokens a real
attacker would try — wrong key, wrong algorithm, tampered payload, unknown
``kid`` — and watch each one be refused.

No network is used and no Google endpoint is contacted.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from app.core.auth.identity import IdentityAssertionError, IdentityClaims
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

TEST_CLIENT_ID = "vmr-test-client.apps.googleusercontent.com"
TEST_ISSUER = "https://accounts.google.com"
TEST_JWKS_URI = "https://jwks.test.invalid/certs"
TEST_TOKEN_URI = "https://token.test.invalid/token"


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64url_json(payload: dict[str, Any]) -> str:
    return b64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))


@dataclass
class SigningKey:
    """One RSA key pair plus the JWKS entry that publishes it."""

    kid: str
    private_key: rsa.RSAPrivateKey = field(
        default_factory=lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048)
    )

    def jwk(self) -> dict[str, Any]:
        numbers = self.private_key.public_key().public_numbers()
        return {
            "kty": "RSA",
            "use": "sig",
            "alg": "RS256",
            "kid": self.kid,
            "n": b64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
            "e": b64url(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
        }

    def sign(self, signing_input: str) -> str:
        signature = self.private_key.sign(
            signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256()
        )
        return b64url(signature)

    def public_pem(self) -> bytes:
        return self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )


def id_token(
    key: SigningKey,
    *,
    claims: dict[str, Any],
    algorithm: str = "RS256",
    kid: str | None = None,
    header_extra: dict[str, Any] | None = None,
    tamper_payload: bool = False,
    signing_key: SigningKey | None = None,
) -> str:
    """Mint a compact JWT, optionally in one of the shapes an attacker would try."""

    header: dict[str, Any] = {"alg": algorithm, "typ": "JWT", "kid": kid or key.kid}
    header.update(header_extra or {})
    encoded_header = b64url_json(header)
    encoded_payload = b64url_json(claims)
    signature = (signing_key or key).sign(f"{encoded_header}.{encoded_payload}")
    if tamper_payload:
        # Re-encode a changed payload *after* signing: the classic forgery.
        forged = dict(claims)
        forged["email"] = "attacker@example.com"
        encoded_payload = b64url_json(forged)
    return f"{encoded_header}.{encoded_payload}.{signature}"


def google_claims(
    *,
    email: str = "operator@vmr.example",
    nonce: str,
    audience: str = TEST_CLIENT_ID,
    issuer: str = TEST_ISSUER,
    email_verified: bool = True,
    subject: str = "1234567890",
    name: str = "VMR Operator",
    now: int | None = None,
    expires_in: int = 3600,
    issued_offset: int = 0,
) -> dict[str, Any]:
    moment = int(time.time()) if now is None else now
    return {
        "iss": issuer,
        "aud": audience,
        "sub": subject,
        "email": email,
        "email_verified": email_verified,
        "name": name,
        "nonce": nonce,
        "iat": moment + issued_offset,
        "exp": moment + expires_in,
    }


def jwks_transport(*keys: SigningKey, status_code: int = 200) -> httpx.MockTransport:
    """An ``httpx`` transport that serves exactly these keys as a JWKS."""

    document = {"keys": [key.jwk() for key in keys]}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            json=document,
            headers={"cache-control": "public, max-age=3600"},
        )

    return httpx.MockTransport(handler)


def token_and_jwks_transport(
    *keys: SigningKey,
    id_token_value: str,
    token_status: int = 200,
    token_body: dict[str, Any] | None = None,
) -> httpx.MockTransport:
    """One transport serving both the token exchange and the JWKS."""

    document = {"keys": [key.jwk() for key in keys]}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TEST_JWKS_URI:
            return httpx.Response(
                200, json=document, headers={"cache-control": "public, max-age=3600"}
            )
        if token_body is not None:
            return httpx.Response(token_status, json=token_body)
        return httpx.Response(token_status, json={"id_token": id_token_value})

    return httpx.MockTransport(handler)


class RecordingIdentityProvider:
    """A provider seam that returns whatever the test tells it to.

    Used for the route-level tests, where the point is the surrounding flow —
    state, nonce, allow-list, session minting — rather than the cryptography,
    which the JWKS tests cover directly against real signatures.
    """

    def __init__(
        self,
        *,
        claims: IdentityClaims | None = None,
        error: Exception | None = None,
    ) -> None:
        self.claims = claims
        self.error = error
        self.authorization_calls: list[dict[str, str]] = []
        self.exchange_calls: list[dict[str, str]] = []

    def authorization_url(
        self, *, redirect_uri: str, state: str, nonce: str, code_challenge: str
    ) -> str:
        self.authorization_calls.append(
            {
                "redirect_uri": redirect_uri,
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge,
            }
        )
        return f"https://accounts.google.test/authorize?state={state}"

    async def exchange_code(
        self, *, code: str, redirect_uri: str, code_verifier: str
    ) -> IdentityClaims:
        self.exchange_calls.append(
            {"code": code, "redirect_uri": redirect_uri, "code_verifier": code_verifier}
        )
        if self.error is not None:
            raise self.error
        if self.claims is None:  # pragma: no cover - test wiring error
            raise IdentityAssertionError("no claims configured")
        return self.claims


def operator_claims(
    *,
    nonce: str,
    email: str = "operator@vmr.example",
    email_verified: bool = True,
    audience: str = TEST_CLIENT_ID,
    issuer: str = TEST_ISSUER,
    now: int | None = None,
    expires_in: int = 3600,
) -> IdentityClaims:
    moment = int(time.time()) if now is None else now
    return IdentityClaims(
        subject="1234567890",
        email=email,
        email_verified=email_verified,
        display_name="VMR Operator",
        issuer=issuer,
        audience=audience,
        expires_at=moment + expires_in,
        issued_at=moment,
        nonce=nonce,
    )
