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
from itertools import islice
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
_URL_USERINFO = re.compile(
    r"(?i)\b(https?|postgres(?:ql)?(?:\+[a-z0-9]+)?|mysql(?:\+[a-z0-9]+)?|redis)"
    r"://[^\s/@]+(?::[^\s/@]*)?@"
)


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _secret_key(value: str) -> bool:
    normalized = _normalized_key(value)
    return normalized in _SECRET_KEY_PARTS or any(
        normalized.endswith(part) for part in _SECRET_KEY_PARTS if len(part) >= 5
    )


def _bounded_text(value: str, *, max_string: int) -> str:
    value = _URL_USERINFO.sub(lambda match: f"{match.group(1)}://{REDACTED}@", value)
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
        entries = list(islice(iter(value.items()), max_items + 1))
        bounded_entries: list[tuple[str, Any]] = []
        for index, (raw_key, item) in enumerate(entries[:max_items]):
            if isinstance(raw_key, str):
                key = raw_key
            elif type(raw_key) in {type(None), bool, int, float}:
                key = str(raw_key)
            else:
                key = f"[unsupported key {type(raw_key).__name__} #{index + 1}]"
            bounded_entries.append((key, item))
        for key, item in sorted(bounded_entries, key=lambda row: row[0]):
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
            try:
                omitted = max(1, len(value) - max_items)
                marker = f"[truncated {omitted} items]"
            except Exception:
                marker = "[truncated additional items]"
            result["…"] = marker
        return result
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, memoryview)):
        items = list(islice(iter(value), max_items + 1))
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
            try:
                omitted = max(1, len(value) - max_items)
                marker = f"[truncated {omitted} items]"
            except Exception:
                marker = "[truncated additional items]"
            result_list.append(marker)
        return result_list
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"[{type(value).__name__} withheld: {len(value)} bytes]"
    return f"[unsupported {type(value).__name__}]"
