"""Make backend failure text safe to put on an operator screen.

Failure text arrives from adapters, providers, drivers and the database. Any of
those can quote the thing that failed, and the thing that failed can be a URL
with an API key in it or a connection string with a password. The Workbench is a
page an operator may screenshot into an issue, so text is sanitized on the way
out rather than trusted on the way in.

This is redaction, not suppression: the shape of the failure is preserved so it
stays diagnosable. ``api=sk_live_abc`` becomes ``api=[redacted]``, not an empty
string, and the surrounding message is untouched.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[redacted]"

#: Maximum characters of failure text rendered. Long provider payloads are
#: truncated with an explicit marker rather than silently cut.
MAX_MESSAGE_CHARS = 1_000

_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Query-string or key/value secrets: api=..., api_key: ..., token="...".
    re.compile(
        r"(?i)\b(api|api[_-]?key|apikey|key|token|secret|password|passwd|pwd|authorization|auth)"
        r"\s*[=:]\s*[\"']?([^\s\"'&;,)]+)"
    ),
    # Credentials embedded in a URL: scheme://user:password@host
    re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^\s/:@]+):([^\s/@]+)@"),
    # Bearer / Basic authorization values.
    re.compile(r"(?i)\b(bearer|basic)\s+([A-Za-z0-9._\-+/=]{8,})"),
    # Long opaque provider key shapes.
    re.compile(r"\b(sk|pk|rk)_(live|test)_[A-Za-z0-9]{6,}\b"),
)


def _redact_kv(match: re.Match[str]) -> str:
    return f"{match.group(1)}={REDACTED}"


def _redact_url(match: re.Match[str]) -> str:
    return f"{match.group(1)}{match.group(2)}:{REDACTED}@"


def _redact_scheme(match: re.Match[str]) -> str:
    return f"{match.group(1)} {REDACTED}"


def sanitize_text(value: str | None, *, limit: int = MAX_MESSAGE_CHARS) -> str | None:
    """Redact credential-shaped substrings and bound the length."""

    if value is None:
        return None
    # Order matters. The bearer/basic pattern runs first: the generic key/value
    # rule would otherwise consume "Authorization:" and leave the token itself
    # sitting in the message.
    cleaned = value
    cleaned = _PATTERNS[2].sub(_redact_scheme, cleaned)
    cleaned = _PATTERNS[1].sub(_redact_url, cleaned)
    cleaned = _PATTERNS[0].sub(_redact_kv, cleaned)
    cleaned = _PATTERNS[3].sub(REDACTED, cleaned)
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip() + " … (truncated)"
    return cleaned


#: Keys whose *values* are never rendered, whatever they contain. Matching is on
#: a normalized key name, so ``API_KEY``, ``api-key`` and ``apiKey`` all match.
_SENSITIVE_KEYS = frozenset(
    {
        "api",
        "apikey",
        "authorization",
        "auth",
        "credential",
        "credentials",
        "key",
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "accesstoken",
        "refreshtoken",
        "sessiontoken",
        "databaseurl",
        "dsn",
        "connectionstring",
    }
)


def _normalized_key(key: str) -> str:
    return key.replace("_", "").replace("-", "").lower()


def sanitize_payload(value: Any, *, depth: int = 0) -> Any:
    """Recursively redact a structured payload before it is rendered.

    Bounded in depth so a pathological nested payload cannot hang a page render.
    """

    if depth > 8:
        return "…"
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            if _normalized_key(key) in _SENSITIVE_KEYS:
                clean[key] = REDACTED
            else:
                clean[key] = sanitize_payload(raw_value, depth=depth + 1)
        return clean
    if isinstance(value, (list, tuple)):
        return [sanitize_payload(item, depth=depth + 1) for item in value]
    if isinstance(value, str):
        return sanitize_text(value, limit=400)
    return value


def sanitize_mapping(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    result = sanitize_payload(value)
    return result if isinstance(result, dict) else {}
