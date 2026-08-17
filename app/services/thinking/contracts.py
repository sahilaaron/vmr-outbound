"""The seam between an Agent and a language model.

Three Phase 2 Agents — Research, Insights and Personalization — need language
understanding rather than a deterministic rule. This module is the only place
that decides *what that means*, so the Agents themselves stay ordinary adapters
that ask a question and receive a validated JSON object.

The seam exists for three reasons, in order of importance:

1. **Nothing fabricated may reach a draft.** A model answer is an
   interpretation, never evidence. Every caller validates the returned payload
   against its own contract and refuses what does not parse, rather than
   storing a plausible-looking shape and discovering the problem in an operator's
   outbox.
2. **The automated suite must never shell out.** Tests inject a scripted
   :class:`Thinker`; production injects :class:`~app.services.thinking.claude_cli.ClaudeCliThinker`.
   Neither knows about the other.
3. **A failure has to be classifiable.** The Agent worker distinguishes
   "try again in a minute" from "this will never work"; a raw
   ``subprocess.CalledProcessError`` cannot express that, so the errors below do.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class ThinkingError(Exception):
    """A language-model call that did not produce a usable answer.

    ``retryable`` is the honest question "would running this again plausibly
    succeed?" — not "was this bad?". A timeout is retryable; a missing
    executable is not.
    """

    retryable = False
    code = "thinking_failed"

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


class ThinkingUnavailable(ThinkingError):
    """The model could not be reached at all — no executable, no permission."""

    retryable = False
    code = "thinking_unavailable"


class ThinkingTimeout(ThinkingError):
    """The call exceeded its wall-clock budget and was terminated."""

    retryable = True
    code = "thinking_timeout"


class ThinkingRefused(ThinkingError):
    """The model ran and declined, or the tool failed permanently.

    Reserved for causes a retry cannot change: an unauthenticated session, a
    rejected model or flag, a permission denial, an explicit policy refusal.
    A tool failure that is merely *unexplained* is :class:`ThinkingTransient`,
    not this — see the classification note there.
    """

    retryable = False
    code = "thinking_refused"


class ThinkingTransient(ThinkingError):
    """The call failed for a reason that repeating it could plausibly survive.

    Provider capacity, a rate or usage limit, a network fault, a 5xx from the
    service behind the CLI, or a non-zero exit with no recognisably permanent
    cause. Retryable on purpose, and the default for an unclassifiable tool
    failure, because the two errors are not symmetric: a wrongly-retryable
    failure costs at most ``max_attempts`` bounded calls and then fails
    terminally anyway, while a wrongly-terminal one costs the Contact and has
    to be re-queued by hand. A usage limit reached part-way through a batch is
    exactly this shape, and it must pause the batch rather than consume it.
    """

    retryable = True
    code = "thinking_transient"


class ThinkingMalformed(ThinkingError):
    """The model answered, but not with the JSON object that was asked for.

    Retryable on purpose: a single malformed answer is usually a one-off, and
    the alternative — failing the Contact terminally — costs an operator more
    than one repeated call does.
    """

    retryable = True
    code = "thinking_malformed"


@dataclass(frozen=True)
class ThinkingRequest:
    """One question, with the budget and tool permission it may use."""

    prompt: str
    # A short label recorded alongside the answer so a stored dossier or draft
    # can say which question produced it.
    purpose: str
    timeout_seconds: float = 240.0
    # Tool names the model may use for this call. Empty means "answer from the
    # prompt alone" — the right setting for drafting, where the only admissible
    # inputs are the evidence already attached.
    allowed_tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class ThinkingResult:
    """A parsed JSON answer plus the provenance needed to store it honestly."""

    payload: dict[str, Any]
    producer: str
    producer_version: str
    duration_seconds: float
    # Kept for the operator-facing failure path only. Never stored verbatim in a
    # dossier: the parsed payload is the record, the raw text is the receipt.
    raw: str = ""
    warnings: tuple[str, ...] = field(default_factory=tuple)


class Thinker(Protocol):
    """Anything that can answer a :class:`ThinkingRequest` with JSON."""

    name: str
    version: str

    def think(self, request: ThinkingRequest) -> ThinkingResult: ...
