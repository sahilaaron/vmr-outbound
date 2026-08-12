"""Password hashing and the password policy.

Two decisions live here, and both are the kind that are cheap to make correctly
once and expensive to change later.

The primitive
-------------
Argon2id, through ``argon2-cffi`` — the reference binding, maintained by the
Python Cryptographic Authority, wheels for every platform this deployment uses,
and no C toolchain at install time. Argon2id is the OWASP first choice for new
applications and is memory-hard, which is the property that matters: an attacker
with a stolen ``users`` table is limited by RAM per guess rather than by how many
GPU cores they can rent.

The parameters below are the OWASP minimum configuration for Argon2id (19 MiB of
memory, two iterations, one degree of parallelism), which is deliberately the
*floor* rather than an invented number. It costs roughly 40ms per verification on
the staging VPS — imperceptible on a login form, and 40ms multiplied by the size
of a password list is the whole point.

Rejected, and why, so nobody has to re-derive it:

* **bcrypt.** Would be acceptable, but it silently truncates at 72 bytes, which
  collides with this policy's requirement to accept 64+ character
  password-manager output and its refusal to forbid any character. A primitive
  whose failure mode is "two different long passwords are the same password" is
  the wrong default when a better one installs just as easily.
* **A bare hash — SHA-256, SHA-512, MD5.** Not password functions. A single fast
  hash of a human-chosen password is a rainbow-table lookup.
* **PBKDF2 via ``hashlib``.** Stdlib-only and therefore tempting, but it is not
  memory-hard, so it is the weakest of the acceptable options and would have been
  chosen only to avoid one well-maintained dependency.

The policy
----------
Length first, composition never. NIST SP 800-63B is explicit that composition
rules ("one uppercase, one digit, one symbol") push people toward predictable
substitutions and *reduce* real entropy, and that periodic expiry without
evidence of compromise does the same. So: a 15-character minimum for accounts
that authenticate by password, at least 64 characters accepted, every printable
character permitted including spaces, paste and autofill supported by the form,
no expiry, and a bounded blocklist of the passwords an online attacker actually
tries first.

The blocklist is deliberately small and local. A full compromised-password corpus
means either a large data file in the repository or a network call to a range API
on the login path, and neither earns its place in a Beta whose real defence
against guessing is the 15-character minimum plus rate limiting.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2 import exceptions as argon2_exceptions

#: Shortest password accepted for an account that signs in with a password.
#: Fifteen rather than eight: this is a single-factor path with no second factor
#: behind it, and length is the only control that scales against offline
#: cracking.
MIN_PASSWORD_CHARS = 15

#: Longest password accepted. Well above the 64 the policy requires, and bounded
#: only so that an unbounded body cannot be turned into a CPU-exhaustion vector
#: by hashing a megabyte of input per request.
MAX_PASSWORD_CHARS = 256

# OWASP's minimum Argon2id configuration. Named constants rather than literals so
# that a future increase is one edit and is visible in a diff.
ARGON2_MEMORY_KIB = 19456  # 19 MiB
ARGON2_TIME_COST = 2
ARGON2_PARALLELISM = 1
ARGON2_HASH_BYTES = 32
ARGON2_SALT_BYTES = 16

_HASHER = PasswordHasher(
    time_cost=ARGON2_TIME_COST,
    memory_cost=ARGON2_MEMORY_KIB,
    parallelism=ARGON2_PARALLELISM,
    hash_len=ARGON2_HASH_BYTES,
    salt_len=ARGON2_SALT_BYTES,
)

#: Passwords an online attacker tries first, plus the ones this project's own
#: documentation and fixtures could plausibly leak into a real deployment. Every
#: entry is compared after the same normalisation the stored password gets, so
#: casing and Unicode form do not create a bypass.
#:
#: Entries shorter than `MIN_PASSWORD_CHARS` are still listed: the length rule
#: already refuses them, and listing them keeps the blocklist meaningful if the
#: minimum is ever lowered.
_COMMON_PASSWORDS: frozenset[str] = frozenset(
    {
        "password",
        "password1",
        "password123",
        "password1234",
        "password12345",
        "passwordpassword",
        "p@ssw0rd",
        "p@ssword123",
        "123456",
        "1234567890",
        "12345678901234567890",
        "111111111111111",
        "qwertyuiopasdfg",
        "qwertyuiop123456",
        "administrator",
        "administrator123",
        "letmeinletmein",
        "iloveyouiloveyou",
        "welcome",
        "welcome123",
        "welcometovmr123",
        "changeme",
        "changeme123",
        "changemepassword",
        "temporarypassword",
        "temppassword123",
        "verifiedmarketresearch",
        "verifiedmarket123",
        "vmroutbound",
        "vmroutbound123",
        "vmrpassword123",
        "abcdefghijklmnop",
        "aaaaaaaaaaaaaaa",
        "correcthorsebatterystaple",
        "thisisapassword",
        "secretsecret123",
    }
)


class PasswordPolicyError(ValueError):
    """Raised when a proposed password does not satisfy the policy.

    The message is written to be shown to the person choosing the password: it
    says what is wrong and what would be acceptable, and it never echoes the
    value back.
    """


@dataclass(frozen=True)
class PasswordRules:
    """The policy as data, so the setup page can state it without duplicating it."""

    minimum_characters: int = MIN_PASSWORD_CHARS
    maximum_characters: int = MAX_PASSWORD_CHARS


PASSWORD_RULES = PasswordRules()


def normalize_password(raw: str) -> str:
    """The one form a password is measured, compared and hashed in.

    NFKC, and *only* for the password. The same transform is refused for email
    addresses two modules over, and the difference is worth stating because the
    inconsistency looks like an oversight otherwise: normalising an address is a
    *widening* step that can make an unapproved address match an approved one,
    whereas normalising a password only decides whether the same keystrokes on
    two different keyboards produce the same secret. NIST recommends NFKC here
    for exactly that reason.

    Surrounding whitespace is preserved, not stripped. A password manager may
    legitimately generate a value with a leading or trailing space, and silently
    trimming it would make a stored password impossible to reproduce.
    """

    return unicodedata.normalize("NFKC", raw)


def validate_password(raw: str, *, email: str | None = None) -> str:
    """Return the normalised password, or raise :class:`PasswordPolicyError`.

    ``email`` is used only to refuse a password that *is* the account's own
    address or its local part — the single context-specific case common enough to
    be worth one comparison.
    """

    candidate = normalize_password(raw)

    if len(candidate) < MIN_PASSWORD_CHARS:
        raise PasswordPolicyError(
            f"Use at least {MIN_PASSWORD_CHARS} characters. "
            "Length is the whole rule here: there is no requirement to include "
            "capitals, digits or symbols, and a passphrase of ordinary words is "
            "a good choice."
        )
    if len(candidate) > MAX_PASSWORD_CHARS:
        raise PasswordPolicyError(f"Use at most {MAX_PASSWORD_CHARS} characters.")
    if candidate.strip() == "":
        raise PasswordPolicyError("A password cannot be only spaces.")

    folded = candidate.casefold()
    if folded in _COMMON_PASSWORDS:
        raise PasswordPolicyError(
            "That password appears on a list of commonly used passwords. "
            "Choose something else — a password manager's generated value or "
            "an unusual phrase both work."
        )

    if email:
        address = email.casefold()
        local_part = address.partition("@")[0]
        if folded == address or (local_part and folded == local_part):
            raise PasswordPolicyError("A password cannot be your own email address.")

    return candidate


def hash_password(raw: str) -> str:
    """Hash an already-validated password into an Argon2id PHC string.

    The returned value embeds the algorithm, the parameters and a per-password
    random salt, so raising the cost later does not invalidate existing hashes —
    :func:`needs_rehash` reports which ones are worth upgrading on next use.
    """

    return _HASHER.hash(normalize_password(raw))


def verify_password(stored_hash: str | None, presented: str) -> bool:
    """Whether ``presented`` matches ``stored_hash``. Never raises.

    A missing hash — an account that has not completed password setup — is a
    plain ``False``. So is a stored value that is not a parseable Argon2 string,
    which is a corruption rather than a match and must not become a 500 on the
    login path.

    This function does **not** decide whether the account may sign in. Account
    state, role and revocation are the caller's business; this answers one
    question.
    """

    if not stored_hash:
        return False
    try:
        return _HASHER.verify(stored_hash, normalize_password(presented))
    except (
        argon2_exceptions.VerifyMismatchError,
        argon2_exceptions.VerificationError,
        argon2_exceptions.InvalidHashError,
    ):
        return False


def needs_rehash(stored_hash: str) -> bool:
    """Whether ``stored_hash`` was produced with weaker parameters than current."""

    try:
        return _HASHER.check_needs_rehash(stored_hash)
    except argon2_exceptions.InvalidHashError:  # pragma: no cover - defensive
        return False


def dummy_verify() -> None:
    """Spend the same work as a real verification, then discard the result.

    Called on the login path when no account matches the presented address. Without
    it, "unknown email" returns in microseconds while "wrong password" takes 40ms,
    and the difference is a free account-enumeration oracle on an endpoint that is
    otherwise careful to say the same thing in both cases.
    """

    _HASHER.verify(_DUMMY_HASH, "vmr-dummy-verification-input")


# Computed once at import so the timing-equalising path costs the same as a real
# verification without hashing a throwaway password on every failed login.
_DUMMY_HASH = _HASHER.hash("vmr-dummy-verification-input")
