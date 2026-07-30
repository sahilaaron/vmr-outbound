"""The language-model seam: what it accepts, and what it refuses to guess at.

The parser is the interesting part. The Claude CLI's output envelope has changed
between releases and a model sometimes wraps its answer in prose or a code fence,
so the seam accepts several shapes on purpose. What it must never do is *invent*
an answer: unparseable output is a failure, never a partial read, because half a
company record is worse than none — the missing half is invisible downstream.
"""

from __future__ import annotations

import subprocess
from unittest import mock

import pytest
from app.core.config import Settings
from app.services.thinking.claude_cli import ClaudeCliThinker, extract_json_object
from app.services.thinking.contracts import (
    ThinkingMalformed,
    ThinkingRefused,
    ThinkingRequest,
    ThinkingUnavailable,
)


def test_a_bare_json_object_is_read_as_the_answer() -> None:
    assert extract_json_object('{"subject": "hello"}') == {"subject": "hello"}


def test_the_cli_json_envelope_is_unwrapped() -> None:
    """The CLI's own --output-format json wraps the model's answer in `result`."""

    envelope = '{"type":"result","is_error":false,"result":"{\\"claims\\": [1]}"}'
    assert extract_json_object(envelope) == {"claims": [1]}


def test_an_answer_inside_a_code_fence_is_recovered() -> None:
    """A fenced answer is a formatting habit, not a failure worth escalating."""

    text = 'Here is the result:\n```json\n{"summary": "a company"}\n```\nHope that helps.'
    assert extract_json_object(text) == {"summary": "a company"}


def test_braces_inside_strings_do_not_break_the_scan() -> None:
    text = 'preamble {"body": "we use {placeholders} sometimes", "n": 1} trailing'
    assert extract_json_object(text) == {"body": "we use {placeholders} sometimes", "n": 1}


def test_an_error_envelope_is_a_refusal_not_a_parse_failure() -> None:
    """The CLI told us it failed. Reporting that is more useful than 'malformed'."""

    with pytest.raises(ThinkingRefused):
        extract_json_object('{"type":"result","is_error":true,"result":"rate limited"}')


@pytest.mark.parametrize("text", ["", "   ", "I could not do that.", "[1, 2, 3]"])
def test_output_without_an_object_is_a_failure_rather_than_a_guess(text: str) -> None:
    with pytest.raises(ThinkingMalformed):
        extract_json_object(text)


def test_a_missing_executable_is_reported_as_unavailable_not_retried() -> None:
    """No amount of retrying installs the CLI, so this must not be retryable."""

    settings = Settings(claude_cli_path="definitely-not-a-real-executable-xyz")
    thinker = ClaudeCliThinker(settings=settings)
    with pytest.raises(ThinkingUnavailable) as caught:
        thinker.think(ThinkingRequest(prompt="hello", purpose="test"))
    assert caught.value.retryable is False


def test_a_non_zero_exit_never_yields_a_partial_answer() -> None:
    """`false` exits 1 having printed nothing; that is a refusal, not an empty answer."""

    settings = Settings(claude_cli_path="false", claude_cli_arguments=())
    thinker = ClaudeCliThinker(settings=settings)
    with pytest.raises(ThinkingRefused):
        thinker.think(ThinkingRequest(prompt="hello", purpose="test"))


def test_the_prompt_reaches_the_executable_on_stdin() -> None:
    """`cat` echoes stdin, which proves the prompt is delivered and parsed back."""

    settings = Settings(claude_cli_path="cat", claude_cli_arguments=())
    thinker = ClaudeCliThinker(settings=settings)
    result = thinker.think(ThinkingRequest(prompt='{"claims": ["from stdin"]}', purpose="test"))
    assert result.payload == {"claims": ["from stdin"]}
    assert result.producer == "claude-cli"


def test_a_tool_permission_is_only_passed_when_the_call_allows_one() -> None:
    """Drafting must not inherit web access from a template written for research."""

    settings = Settings(claude_cli_path="cat", claude_cli_arguments=("{allowed_tools}",))
    thinker = ClaudeCliThinker(settings=settings)
    assert thinker._arguments(ThinkingRequest(prompt="x", purpose="p")) == []
    assert thinker._arguments(
        ThinkingRequest(prompt="x", purpose="p", allowed_tools=("WebSearch",))
    ) == ["--allowedTools", "WebSearch"]


def test_non_ascii_survives_the_round_trip_in_both_directions() -> None:
    """A prospect named Sørensen must not break the seam.

    `cat` echoes stdin, so this exercises the encode of the prompt and the decode of
    the output in one pass. Every character here is one that a real campaign carries
    and that a Windows code page cannot represent: Nordic and Arabic names, an em
    dash, the curly quotes the CLI's own output uses, a CJK company name.
    """

    payload = (
        '{"claims": ["Ana Sørensen — Sanko Pharma \\u2018quality\\u2019 roadmap", '
        '"شركة الخليج", "京セラ"]}'
    )
    settings = Settings(claude_cli_path="cat", claude_cli_arguments=())
    thinker = ClaudeCliThinker(settings=settings)
    result = thinker.think(ThinkingRequest(prompt=payload, purpose="test"))
    assert result.payload["claims"][0] == "Ana Sørensen — Sanko Pharma ‘quality’ roadmap"
    assert result.payload["claims"][1] == "شركة الخليج"
    assert result.payload["claims"][2] == "京セラ"


def test_the_subprocess_encoding_is_stated_rather_than_inherited() -> None:
    """The locale must never decide how the seam talks to the CLI.

    `text=True` would take the encoding from the host locale — a code page on
    Windows, where most of the above is unrepresentable. The failure that produced
    was a UnicodeEncodeError raised inside `subprocess.run`: not an OSError and not a
    ThinkingError, so it escaped the seam's whole error vocabulary and surfaced as
    the worker's opaque "unexpected operational error", retrying forever.

    Asserted on the call rather than on behaviour because behaviour cannot show it:
    on a UTF-8 host the broken version passes.
    """

    captured: dict[str, object] = {}
    real_run = subprocess.run

    def _spy(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return real_run(*args, **kwargs)  # type: ignore[arg-type]

    settings = Settings(claude_cli_path="cat", claude_cli_arguments=())
    thinker = ClaudeCliThinker(settings=settings)
    with mock.patch.object(subprocess, "run", _spy):
        thinker.think(ThinkingRequest(prompt='{"ok": true}', purpose="test"))

    assert captured.get("encoding") == "utf-8"
    assert captured.get("errors") == "strict", (
        "replace would substitute U+FFFD and hand back a quietly altered answer, "
        "which is the partial read this seam refuses to do"
    )
    assert "text" not in captured, "text=True is what defers to the locale"


def test_output_that_is_not_utf8_is_malformed_not_an_unexplained_crash() -> None:
    """A decode failure has to arrive in the seam's own vocabulary."""

    settings = Settings(claude_cli_path="cat", claude_cli_arguments=())
    thinker = ClaudeCliThinker(settings=settings)

    def _bad_bytes(*_args: object, **_kwargs: object) -> object:
        raise UnicodeDecodeError("utf-8", b"\xff\xfe", 0, 1, "invalid start byte")

    with mock.patch.object(subprocess, "run", _bad_bytes):
        with pytest.raises(ThinkingMalformed) as caught:
            thinker.think(ThinkingRequest(prompt="x", purpose="test"))
    assert "not valid UTF-8" in str(caught.value)
