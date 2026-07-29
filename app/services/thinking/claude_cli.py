"""A :class:`~app.services.thinking.contracts.Thinker` backed by the local Claude CLI.

This runs the operator's own ``claude`` executable as a subprocess, under their
existing subscription. It adds no paid API dependency and holds no credential of
its own — which is precisely why it is the sanctioned path here.

Two decisions in this module are deliberate and worth keeping:

**The command is data, not code.** ``claude`` is a moving target: flag names and
the exact JSON envelope have changed between versions. Rather than pin this
build to one CLI release, the argument template lives in settings and the parser
accepts every envelope shape the CLI has plausibly produced. A flag change
should be an ``.env`` edit, not a patch.

**A non-zero exit is never interpreted.** If the CLI fails, the failure is
reported as a failure. There is no partial parse, no "best effort" salvage of
half an answer — a half-read company is worse than an unread one, because the
half that is missing is invisible downstream.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from typing import Any

from app.core.config import Settings, get_settings
from app.services.thinking.contracts import (
    ThinkingMalformed,
    ThinkingRefused,
    ThinkingRequest,
    ThinkingResult,
    ThinkingTimeout,
    ThinkingUnavailable,
)

PRODUCER = "claude-cli"


def extract_json_object(text: str) -> dict[str, Any]:
    """Pull the one JSON object out of whatever the CLI printed.

    Tried in order: the whole string; the CLI's ``--output-format json``
    envelope, whose ``result`` field holds the model's actual answer as a
    string; and finally the first balanced ``{...}`` span in the output, which
    is what survives when the model wraps its answer in a fenced code block or
    a sentence of preamble.

    Raises :class:`ThinkingMalformed` when none of those yields an object.
    """

    stripped = text.strip()
    if not stripped:
        raise ThinkingMalformed("The model returned no output.")

    direct = _loads_object(stripped)
    if direct is not None:
        # An envelope carries the answer in a nested field; a bare answer does
        # not. Distinguishing them by shape (rather than by CLI version) is what
        # keeps this tolerant of the CLI changing underneath us.
        for envelope_key in ("result", "output", "text", "content"):
            inner = direct.get(envelope_key)
            if isinstance(inner, str):
                nested = _loads_object(inner) or _first_balanced_object(inner)
                if nested is not None:
                    return nested
        if direct.get("is_error") is True:
            raise ThinkingRefused(
                "The Claude CLI reported an error result.",
                detail={"result": str(direct.get("result"))[:400]},
            )
        return direct

    found = _first_balanced_object(stripped)
    if found is not None:
        return found
    raise ThinkingMalformed(
        "The model did not return a JSON object.",
        detail={"output_head": stripped[:400]},
    )


def _loads_object(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _first_balanced_object(text: str) -> dict[str, Any] | None:
    """Scan for the first brace-balanced span, ignoring braces inside strings."""

    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidate = _loads_object(text[start : index + 1])
                    if candidate is not None:
                        return candidate
                    break
        start = text.find("{", start + 1)
    return None


class ClaudeCliThinker:
    """Answer a request by running the operator's local ``claude`` executable."""

    name = PRODUCER

    def __init__(self, *, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self.version = self._settings.claude_cli_version_label

    def think(self, request: ThinkingRequest) -> ThinkingResult:
        executable = self._settings.claude_cli_path
        resolved = shutil.which(executable)
        if resolved is None:
            raise ThinkingUnavailable(
                f"The Claude CLI executable {executable!r} was not found on PATH.",
                detail={"executable": executable},
            )

        argv = [resolved, *self._arguments(request)]
        timeout = min(request.timeout_seconds, self._settings.claude_cli_timeout_seconds)
        started = time.monotonic()
        try:
            completed = subprocess.run(  # noqa: S603 - argv is a list; never shell=True
                argv,
                input=request.prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                cwd=self._settings.claude_cli_working_directory or None,
            )
        except subprocess.TimeoutExpired as exc:
            raise ThinkingTimeout(
                f"The Claude CLI did not answer within {timeout:.0f}s.",
                detail={"purpose": request.purpose, "timeout_seconds": timeout},
            ) from exc
        except OSError as exc:
            raise ThinkingUnavailable(
                "The Claude CLI could not be executed.",
                detail={"error": str(exc)},
            ) from exc

        duration = time.monotonic() - started
        if completed.returncode != 0:
            raise ThinkingRefused(
                f"The Claude CLI exited with status {completed.returncode}.",
                detail={
                    "returncode": completed.returncode,
                    # Truncated deliberately: this is surfaced to an operator and
                    # goes through the workbench sanitizer on the way.
                    "stderr": (completed.stderr or "").strip()[:400],
                },
            )

        payload = extract_json_object(completed.stdout or "")
        return ThinkingResult(
            payload=payload,
            producer=self.name,
            producer_version=self.version,
            duration_seconds=duration,
            raw=(completed.stdout or "")[:4000],
        )

    def _arguments(self, request: ThinkingRequest) -> list[str]:
        """Build argv from the configured template.

        ``{allowed_tools}`` expands to nothing when the request permits no
        tools, so a drafting call cannot silently acquire web access from a
        template written for research.
        """

        arguments: list[str] = []
        for token in self._settings.claude_cli_arguments:
            if token == "{allowed_tools}":
                if request.allowed_tools:
                    arguments.extend(["--allowedTools", ",".join(request.allowed_tools)])
                continue
            arguments.append(token)
        return arguments
