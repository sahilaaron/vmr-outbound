"""The encrypted-at-rest envelope for Gmail OAuth tokens.

The mechanism is the one this repository already uses for Admin-managed
verification-provider credentials (``app/services/verification/studio.py``):
Fernet -- AES-128-CBC with an HMAC-SHA256 authentication tag -- with the key
supplied from the environment and never from the database. Reusing the
established primitive rather than inventing one is deliberate; so is using a
*separate key*, so that rotating or losing one credential domain's key has no
effect on the other.

There is no fallback key and no "encode it for now" path. Without
``GMAIL__TOKEN_ENCRYPTION_KEY`` the feature reports itself unavailable, which is
the honest outcome: a token store that quietly degrades to base64 is worse than
one that refuses, because the first looks like it is working.

Every exception raised here is :class:`GmailTokenStorageError` with a fixed
message. Ciphertext, plaintext and the underlying ``cryptography`` exception are
never included -- ``InvalidToken`` carries no secret today, but an error string
is exactly the value that ends up in a log aggregator, and the rule is easier to
keep than to audit.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from app.core.gmail_config import GmailSettings


class GmailTokenStorageError(RuntimeError):
    """Gmail token encryption is unavailable, or a stored token is unreadable."""


def _fernet(settings: GmailSettings) -> Fernet:
    key = settings.token_encryption_key
    if not key or not key.strip():
        raise GmailTokenStorageError(
            "Gmail token storage is unavailable until an explicit encryption key is configured."
        )
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise GmailTokenStorageError("The Gmail token encryption key is invalid.") from exc


def encrypt_token(token: str, *, settings: GmailSettings) -> str:
    """Return Fernet ciphertext for one OAuth token.

    Refuses an empty token rather than storing an encrypted empty string, which
    would decrypt cleanly and then fail as an authorization error somewhere far
    away from the cause.
    """

    if not token or not token.strip():
        raise GmailTokenStorageError("An empty value is not a token and will not be stored.")
    return _fernet(settings).encrypt(token.encode()).decode()


def decrypt_token(ciphertext: str | None, *, settings: GmailSettings) -> str:
    """Return the plaintext token, or raise.

    A missing column and an undecryptable one are the same outcome to every
    caller -- the grant cannot be used and the operator must reconnect -- so
    they raise the same exception rather than making each call site decide.
    """

    if not ciphertext:
        raise GmailTokenStorageError("This mailbox grant holds no stored token.")
    try:
        return _fernet(settings).decrypt(ciphertext.encode()).decode()
    except (InvalidToken, ValueError, TypeError) as exc:
        raise GmailTokenStorageError("A stored Gmail token could not be decrypted.") from exc


def looks_like_ciphertext(value: str | None) -> bool:
    """Whether ``value`` is a well-formed Fernet token.

    Used by the tests that assert tokens are encrypted at rest. It checks the
    structure Fernet guarantees -- version byte ``0x80`` after urlsafe-base64
    decoding, and a length that can hold the timestamp, IV, ciphertext and tag
    -- without needing the key, so the assertion works on a row read straight
    out of the database.
    """

    if not value:
        return False
    import base64

    try:
        raw = base64.urlsafe_b64decode(value.encode())
    except (ValueError, TypeError):
        return False
    return len(raw) >= 57 and raw[0] == 0x80
