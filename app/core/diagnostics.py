"""Deterministic, bounded serialization for diagnostics and error metadata.

This is deliberately stricter than ordinary JSON serialization. Diagnostic
inputs are untrusted: they may contain provider payloads, exceptions, secrets,
HTML, or containers large enough to make an operator page unusable.
"""

from __future__ import annotations

import html
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any, TypeAlias

DiagnosticValue: TypeAlias = (
    None | bool | int | float | str | list["DiagnosticValue"] | dict[str, "DiagnosticValue"]
)

REDACTED = "[redacted]"
MAX_DEPTH_MARKER = "[maximum depth reached]"
_SECRET_KEY_PARTS = frozenset(
    {
        "api",
        "apikey",
        "auth",
        "authorization",
        "connectionstring",
        "cookie",
        "credential",
        "credentials",
        "databaseurl",
        "dsn",
        "key",
        "password",
        "passwd",
        "pwd",
        "secret",
        "session",
        "token",
    }
)
_INLINE_SECRET = re.compile(
    r"(?i)\b(api[_-]?key|authorization|cookie|password|secret|token)"
    r"\s*[=:]\s*[^\s,;&]+"
)


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _secret_key(value: str) -> bool:
    normalized = _normalized_key(value)
    return normalized in _SECRET_KEY_PARTS or any(
        normalized.endswith(part) for part in _SECRET_KEY_PARTS if len(part) >= 5
    )


def _bounded_text(value: str, *, max_string: int) -> str:
    cleaned = _INLINE_SECRET.sub(lambda match: f"{match.group(1)}={REDACTED}", value)
    # Diagnostic strings are often rendered into HTML later. Escaping here is a
    # defence-in-depth boundary; Jinja auto-escaping remains required as well.
    cleaned = html.escape(cleaned, quote=False)
    if len(cleaned) <= max_string:
        return cleaned
    omitted = len(cleaned) - max_string
    return f"{cleaned[:max_string]}…[truncated {omitted} chars]"


def serialize_diagnostic(
    value: Any,
    *,
    max_depth: int = 6,
    max_items: int = 25,
    max_string: int = 400,
    _depth: int = 0,
) -> DiagnosticValue:
    """Return a JSON-safe, redacted and explicitly truncated diagnostic value."""

    if _depth >= max_depth:
        return MAX_DEPTH_MARKER
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else _bounded_text(str(value), max_string=max_string)
    if isinstance(value, str):
        return _bounded_text(value, max_string=max_string)
    if isinstance(value, BaseException):
        # Exception messages routinely contain SQL, paths, DSNs, or provider
        # payloads. The type is useful; its arbitrary text is not safe output.
        return {"error_type": type(value).__name__, "message": "[exception detail withheld]"}
    if isinstance(value, Mapping):
        result: dict[str, DiagnosticValue] = {}
        entries = sorted(((str(key), item) for key, item in value.items()), key=lambda row: row[0])
        for key, item in entries[:max_items]:
            safe_key = _bounded_text(key, max_string=min(max_string, 120))
            result[safe_key] = (
                REDACTED
                if _secret_key(key)
                else serialize_diagnostic(
                    item,
                    max_depth=max_depth,
                    max_items=max_items,
                    max_string=max_string,
                    _depth=_depth + 1,
                )
            )
        if len(entries) > max_items:
            result["…"] = f"[truncated {len(entries) - max_items} items]"
        return result
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, memoryview)):
        items = list(value)
        result_list = [
            serialize_diagnostic(
                item,
                max_depth=max_depth,
                max_items=max_items,
                max_string=max_string,
                _depth=_depth + 1,
            )
            for item in items[:max_items]
        ]
        if len(items) > max_items:
            result_list.append(f"[truncated {len(items) - max_items} items]")
        return result_list
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"[{type(value).__name__} withheld: {len(value)} bytes]"
    return f"[unsupported {type(value).__name__}]"
