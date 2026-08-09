"""Deterministic validation of a generated seven-message sequence.

Two levels, and the distinction matters. **Per-message** validation asks
whether one message is safe to show a human at all. **Sequence-level**
validation asks whether the seven together are a sequence rather than seven
attempts at the same email. A message can be individually flawless and still
fail the second question.

Three outcomes, never two:

*Hard failure* means nothing is persisted and nobody is offered the sequence.
It is reserved for things that are wrong rather than merely weak -- a claim
that the recipient read the last email, an invented deadline, leaked prompt
text, a subject that is empty.

*Warning* means a human should read something before approving. Warnings are
persisted with the message and shown in review; they never silently block.

*Acceptable fallback* means the sequence is thin because the evidence is thin,
which is the correct outcome rather than a defect. A four-sentence follow-up
that talks about the offering because no prospect context cleared policy is
not a failure, and validation must not push the generator toward padding it.

Everything here is deterministic string and structure checking. Nothing calls a
model, and nothing reads the database. That is what makes a validation result
reproducible from the stored text alone, which is what an operator diagnosing a
refusal six weeks later actually needs.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.models.email_sequence import SEQUENCE_LENGTH
from app.models.enums import SequenceMessageType

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.services.personalization.cadence import SequenceCadence
    from app.services.personalization.generation import ContextDecision
    from app.services.personalization.sequence import GeneratedMessage

#: Part of the input digest: changing what validation refuses changes what the
#: same inputs produce.
VALIDATION_POLICY_VERSION = "sequence-validation/v1"

SEVERITY_FAILURE = "failure"
SEVERITY_WARNING = "warning"

MIN_SUBJECT_CHARS = 3
MAX_SUBJECT_CHARS = 160
#: A subject far past this is a sentence, not a subject line.
SUBJECT_WARNING_CHARS = 78
MIN_BODY_WORDS = 12

#: How many messages may share one opening shape, one CTA shape or one subject
#: structure before the sequence stops being a sequence.
MAX_SHARED_OPENING = 2
MAX_SHARED_SUBJECT_SHAPE = 2
MAX_SHARED_CTA = 3
MAX_SHARED_SENTENCE = 1
#: A phrase this long repeated across messages is reuse, not coincidence.
REPEATED_PHRASE_WORDS = 8
MAX_SHARED_PHRASE = 1

_WORD = re.compile(r"[a-z0-9']+")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

#: Claims about recipient behaviour. There is no tracking of any kind behind
#: these messages, so every one of these is a statement about something nobody
#: knows.
_ENGAGEMENT_CLAIMS: tuple[str, ...] = (
    "you opened",
    "you have opened",
    "you've opened",
    "since you opened",
    "you read my",
    "you read our",
    "you have read",
    "you've read",
    "you saw my",
    "you saw our",
    "you clicked",
    "you have clicked",
    "you've clicked",
    "you downloaded",
    "you have downloaded",
    "you've downloaded",
    "you visited",
    "you have visited",
    "you've visited",
    "you viewed",
    "you checked out",
    "i saw that you",
    "i noticed you opened",
    "i noticed you read",
    "i see you opened",
    "i see you read",
    "i can see you",
    "our system shows",
    "our records show you",
    "you ignored",
    "you have ignored",
    "you've ignored",
    "you did not reply",
    "you didn't reply",
    "you haven't replied",
    "you have not replied",
    "you never replied",
    "you passed on",
    "you turned down",
    "you rejected",
    "you engaged with",
)

#: Invented pressure. None of these can be true of a prospect nobody has spoken
#: to, and all of them are standard outbound reflexes.
_INVENTED_URGENCY: tuple[str, ...] = (
    "last chance",
    "final notice",
    "final warning",
    "act now",
    "before it's too late",
    "before its too late",
    "expires today",
    "expires tomorrow",
    "only a few spots",
    "only a few slots",
    "limited spots",
    "limited slots",
    "spots are filling",
    "closing this out today",
    "this offer ends",
    "the offer ends",
    "running out of time",
    "time is running out",
    "don't miss out",
    "dont miss out",
)

#: Asserted knowledge of the prospect's internal world.
_INVENTED_PRIORITY: tuple[str, ...] = (
    "your priority",
    "your priorities are",
    "you are focused on",
    "you're focused on",
    "you must be",
    "your budget for",
    "your roadmap",
    "your q1 goals",
    "your q2 goals",
    "your q3 goals",
    "your q4 goals",
    "i know you are under pressure",
    "i know you're under pressure",
    "your team is struggling",
    "your team is under pressure",
    "your procurement cycle",
    "your growth plans",
    "your expansion plans",
    "your hiring plans",
    "your upcoming initiative",
    "your strategic initiative",
)

#: Guilt and performative familiarity.
_PRESSURE_AND_PERFORMANCE: tuple[str, ...] = (
    "i noticed that your company",
    "i saw that your company",
    "i've been following your company",
    "i have been following your company",
    "we both know",
    "i'll take that as a no",
    "ill take that as a no",
    "should i close your file",
    "should i close the file",
    "am i talking to the wrong person",
    "did i do something wrong",
    "i must have upset you",
    "you're clearly busy but",
    "youre clearly busy but",
    "third and final",
    "breaking up with you",
)

#: Leaked machinery: prompt text, internal metadata, raw structure, secrets and
#: local paths. Each is a sign that something the model was given came back out.
_LEAKAGE_MARKERS: tuple[str, ...] = (
    "untrusted prospect context",
    "trusted seller context",
    "restricted seller claims",
    "non-negotiable writing standards",
    "deterministic temperament instructions",
    "operational drafting rules",
    "selected strategy --",
    "policy version\nv",
    "structured company intelligence",
    "curated policy examples",
    "the seven positions and what each is for",
    "sequence coherence and context distribution",
    "follow-up rules -- non-negotiable",
    "return exactly one json object",
    "evidence_insight_ids",
    "fallback_level",
    "campaign_contact_id",
    "personalization_policy_version",
    "as an ai language model",
    "as an ai assistant",
    "i cannot fulfill",
    "system prompt",
    "```json",
    "api_key",
    "api key:",
    "secret_key",
    "bearer ey",
    "postgresql://",
    "c:\\users\\",
    "/home/claude/",
    "/mnt/user-data/",
)

#: Ways a follow-up can point at a message that does not exist yet.
_FORWARD_REFERENCES: tuple[str, ...] = (
    "in my next email",
    "in my next message",
    "my next note will",
    "i'll send the details next week in my",
    "as i will explain in",
    "in the following email",
)


@dataclass(frozen=True)
class Finding:
    """One validation observation about one message, or about the sequence."""

    severity: str
    code: str
    detail: str
    #: ``None`` for a sequence-level finding.
    position: int | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "detail": self.detail,
            "position": self.position,
        }


@dataclass(frozen=True)
class ValidationFindings:
    """Everything validation concluded, per message and for the sequence."""

    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def failures(self) -> tuple[Finding, ...]:
        return tuple(item for item in self.findings if item.severity == SEVERITY_FAILURE)

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(item for item in self.findings if item.severity == SEVERITY_WARNING)

    @property
    def failed(self) -> bool:
        return bool(self.failures)

    @property
    def sequence_warnings(self) -> tuple[str, ...]:
        return tuple(
            f"{item.code}: {item.detail}" for item in self.warnings if item.position is None
        )

    def warnings_for(self, position: int) -> tuple[str, ...]:
        return tuple(
            f"{item.code}: {item.detail}" for item in self.warnings if item.position == position
        )

    def failures_for(self, position: int) -> tuple[str, ...]:
        return tuple(
            f"{item.code}: {item.detail}" for item in self.failures if item.position == position
        )

    def message_status(self, position: int) -> str:
        if self.failures_for(position):
            return SEVERITY_FAILURE
        return SEVERITY_WARNING if self.warnings_for(position) else "passed"

    def summary(self) -> dict[str, Any]:
        """A bounded record for Admin diagnosis.

        Bounded on purpose: an operator needs to know what was wrong, not to
        receive an unbounded dump that could carry back whatever the model
        produced.
        """

        return {
            "validation_policy_version": VALIDATION_POLICY_VERSION,
            "failed": self.failed,
            "failure_count": len(self.failures),
            "warning_count": len(self.warnings),
            "findings": [item.summary() for item in self.findings[:60]],
            "truncated": len(self.findings) > 60,
        }


class SequenceValidationError(ValueError):
    """A generated sequence must not be persisted or offered for review."""

    code = "sequence_validation_failed"

    def __init__(self, findings: ValidationFindings) -> None:
        failures = findings.failures
        headline = "; ".join(
            f"{'sequence' if item.position is None else f'position {item.position}'}: {item.detail}"
            for item in failures[:4]
        )
        if len(failures) > 4:
            headline += f" (and {len(failures) - 4} more)"
        super().__init__(f"The generated sequence failed validation. {headline}")
        self.findings = findings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _words(text: str) -> list[str]:
    return _WORD.findall(text.casefold())


def _sentences(text: str) -> list[str]:
    return [item.strip() for item in _SENTENCE_SPLIT.split(text.strip()) if item.strip()]


def _first_sentence(text: str) -> str:
    sentences = _sentences(text)
    return sentences[0].casefold() if sentences else ""


def _opening_shape(text: str) -> str:
    """The first five meaningful words, as a crude fingerprint of an opening."""

    return " ".join(_words(_first_sentence(text))[:5])


def _subject_shape(subject: str) -> str:
    """A subject's *structure*, with the specific nouns removed.

    "Quick question about Acme's export lanes" and "Quick question about
    Contoso's freight" are the same subject written twice. Comparing the
    leading function words catches that where comparing whole strings would not.
    """

    words = _words(subject)
    return " ".join(words[:3])


def _phrases(text: str, size: int) -> set[str]:
    words = _words(text)
    if len(words) < size:
        return set()
    return {" ".join(words[index : index + size]) for index in range(len(words) - size + 1)}


def _contains(text: str, needles: Sequence[str]) -> list[str]:
    lowered = text.casefold()
    return [needle for needle in needles if needle in lowered]


def _looks_like_html(text: str) -> bool:
    return bool(re.search(r"<\s*(a|p|div|span|br|table|img|html|body)\b[^>]*>", text, re.I))


def _cta_shape(body: str) -> str:
    """A fingerprint of the ask, taken from the last sentence that asks anything."""

    for sentence in reversed(_sentences(body)):
        lowered = sentence.casefold()
        if "?" in sentence or any(
            marker in lowered
            for marker in ("would you", "are you", "happy to", "worth", "let me know", "open to")
        ):
            return " ".join(_words(lowered)[:6])
    return ""


# ---------------------------------------------------------------------------
# Per-message validation
# ---------------------------------------------------------------------------


def _validate_message(
    message: GeneratedMessage,
    *,
    max_words: int,
    decision: ContextDecision,
) -> list[Finding]:
    findings: list[Finding] = []
    position = message.position
    subject, body = message.subject, message.body

    def fail(code: str, detail: str) -> None:
        findings.append(Finding(SEVERITY_FAILURE, code, detail, position))

    def warn(code: str, detail: str) -> None:
        findings.append(Finding(SEVERITY_WARNING, code, detail, position))

    if len(subject.strip()) < MIN_SUBJECT_CHARS:
        fail("subject_too_short", "The subject line is empty or too short to be one.")
    if len(subject) > MAX_SUBJECT_CHARS:
        fail(
            "subject_too_long",
            f"The subject is {len(subject)} characters, beyond the {MAX_SUBJECT_CHARS} bound.",
        )
    elif len(subject) > SUBJECT_WARNING_CHARS:
        warn(
            "subject_long",
            f"The subject is {len(subject)} characters and will be truncated in most inboxes.",
        )
    if "\n" in subject or "\r" in subject:
        fail("subject_multiline", "The subject line contains a line break.")

    body_words = _words(body)
    if len(body_words) < MIN_BODY_WORDS:
        fail(
            "body_too_short",
            f"The body is {len(body_words)} words, which is too short to be a message.",
        )
    if len(body.split()) > max_words:
        fail(
            "body_too_long",
            f"The body is {len(body.split())} words, beyond the {max_words}-word ceiling "
            f"for position {position}.",
        )

    if _looks_like_html(body) or _looks_like_html(subject):
        fail("html_in_plain_text", "The message contains HTML markup where plain text is expected.")
    if "\ufffd" in body or "\ufffd" in subject:
        fail("malformed_unicode", "The message contains replacement characters.")
    if body.lstrip().startswith("{") and '"body"' in body:
        fail("raw_json_leaked", "The body appears to contain the raw JSON envelope.")

    for code, needles, detail in (
        ("prohibited_engagement_claim", _ENGAGEMENT_CLAIMS, "claims the recipient engaged"),
        ("invented_urgency", _INVENTED_URGENCY, "manufactures urgency or scarcity"),
        ("invented_priority", _INVENTED_PRIORITY, "asserts an unsupported prospect priority"),
        ("pressure_or_performance", _PRESSURE_AND_PERFORMANCE, "uses guilt or performed research"),
        ("leaked_internal_content", _LEAKAGE_MARKERS, "leaks prompt or internal metadata"),
        ("forward_reference", _FORWARD_REFERENCES, "refers to a message not yet written"),
    ):
        hits = _contains(f"{subject}\n{body}", needles)
        if hits:
            fail(code, f"The copy {detail} ({', '.join(sorted(hits)[:3])}).")

    # A follow-up that cites evidence the initial message never had would be
    # inventing proof. The allow-list already refuses unsupplied ids, so this is
    # the weaker, honest check: a message that cites nothing when nothing was
    # eligible is correct, not broken.
    if message.evidence_insight_ids and not any(
        item.evidence_id for item in decision.used
    ):  # pragma: no cover - unreachable while the allow-list holds
        fail(
            "unsupported_proof",
            "The message cites evidence although the decision supplied none.",
        )

    if message.message_type is SequenceMessageType.INITIAL and position != 1:
        fail("wrong_message_type", "Only position 1 may be the initial message.")
    if message.message_type is SequenceMessageType.FOLLOW_UP and position == 1:
        fail("wrong_message_type", "Position 1 cannot be a follow-up.")

    if "?" not in body and position <= 5:
        warn(
            "no_explicit_ask",
            "The message contains no question; check that it still carries a clear ask.",
        )
    return findings


# ---------------------------------------------------------------------------
# Sequence-level validation
# ---------------------------------------------------------------------------


def _validate_structure(messages: Sequence[GeneratedMessage]) -> list[Finding]:
    findings: list[Finding] = []

    def fail(code: str, detail: str) -> None:
        findings.append(Finding(SEVERITY_FAILURE, code, detail, None))

    positions = [message.position for message in messages]
    if len(messages) != SEQUENCE_LENGTH:
        fail(
            "wrong_message_count",
            f"The sequence has {len(messages)} messages rather than {SEQUENCE_LENGTH}.",
        )
    if len(set(positions)) != len(positions):
        fail("duplicate_position", "Two messages claim the same position.")
    if sorted(positions) != list(range(1, SEQUENCE_LENGTH + 1)):
        fail("positions_not_contiguous", f"Positions are {sorted(positions)} rather than 1-7.")

    purposes = [message.purpose for message in messages]
    if len(set(purposes)) != len(purposes):
        fail("duplicate_purpose", "Two positions claim the same purpose.")

    initial = [
        message for message in messages if message.message_type is SequenceMessageType.INITIAL
    ]
    if len(initial) != 1:
        fail("initial_message_count", f"The sequence has {len(initial)} initial messages, not 1.")
    return findings


def _validate_timing(
    messages: Sequence[GeneratedMessage], *, cadence: SequenceCadence
) -> list[Finding]:
    findings: list[Finding] = []
    previous_day = -1
    for message in sorted(messages, key=lambda item: item.position):
        expected_delay, expected_day = cadence.for_position(message.position)
        if message.recommended_delay_days < 0:
            findings.append(
                Finding(
                    SEVERITY_FAILURE,
                    "negative_delay",
                    "A message carries a negative delay.",
                    message.position,
                )
            )
        if message.recommended_elapsed_day <= previous_day and message.position > 1:
            findings.append(
                Finding(
                    SEVERITY_FAILURE,
                    "timing_not_increasing",
                    f"Day {message.recommended_elapsed_day} does not follow day {previous_day}.",
                    message.position,
                )
            )
        if (
            message.recommended_delay_days != expected_delay
            or message.recommended_elapsed_day != expected_day
        ):
            findings.append(
                Finding(
                    SEVERITY_FAILURE,
                    "timing_off_cadence",
                    "The recorded timing does not match the resolved cadence.",
                    message.position,
                )
            )
        previous_day = message.recommended_elapsed_day
    return findings


def _validate_repetition(messages: Sequence[GeneratedMessage]) -> list[Finding]:
    """Catch the sequence that is one email written seven times.

    Every check here is a *count* against a bound rather than a ban, because
    some repetition is correct: the seller's name recurs, the offering recurs,
    and a sequence that renamed the product every message would be worse.
    """

    findings: list[Finding] = []
    ordered = sorted(messages, key=lambda item: item.position)

    def bound(
        label: str,
        code: str,
        values: Mapping[int, str],
        limit: int,
        severity: str = SEVERITY_FAILURE,
    ) -> None:
        counts: dict[str, list[int]] = {}
        for position, value in values.items():
            if value:
                counts.setdefault(value, []).append(position)
        for value, seen in counts.items():
            if len(seen) > limit:
                findings.append(
                    Finding(
                        severity,
                        code,
                        f"{len(seen)} messages share the same {label} "
                        f"(positions {', '.join(str(item) for item in sorted(seen))}): {value!r}.",
                        None,
                    )
                )

    bound(
        "opening",
        "repeated_opening",
        {message.position: _opening_shape(message.body) for message in ordered},
        MAX_SHARED_OPENING,
    )
    bound(
        "subject structure",
        "repeated_subject_structure",
        {message.position: _subject_shape(message.subject) for message in ordered},
        MAX_SHARED_SUBJECT_SHAPE,
    )
    bound(
        "call to action",
        "repeated_cta",
        {message.position: _cta_shape(message.body) for message in ordered},
        MAX_SHARED_CTA,
        severity=SEVERITY_WARNING,
    )

    subjects = [message.subject.strip().casefold() for message in ordered]
    duplicate_subjects = {value for value in subjects if subjects.count(value) > 1}
    if duplicate_subjects:
        findings.append(
            Finding(
                SEVERITY_FAILURE,
                "duplicate_subject",
                f"{len(duplicate_subjects)} subject line(s) are repeated verbatim.",
                None,
            )
        )

    sentence_owners: dict[str, set[int]] = {}
    phrase_owners: dict[str, set[int]] = {}
    for message in ordered:
        for sentence in _sentences(message.body):
            key = " ".join(_words(sentence))
            if len(key.split()) >= 6:
                sentence_owners.setdefault(key, set()).add(message.position)
        for phrase in _phrases(message.body, REPEATED_PHRASE_WORDS):
            phrase_owners.setdefault(phrase, set()).add(message.position)

    repeated_sentences = [
        key for key, owners in sentence_owners.items() if len(owners) > MAX_SHARED_SENTENCE
    ]
    if repeated_sentences:
        findings.append(
            Finding(
                SEVERITY_FAILURE,
                "repeated_sentence",
                f"{len(repeated_sentences)} sentence(s) appear in more than one message.",
                None,
            )
        )
    repeated_phrases = [
        key for key, owners in phrase_owners.items() if len(owners) > MAX_SHARED_PHRASE
    ]
    if repeated_phrases:
        findings.append(
            Finding(
                SEVERITY_WARNING,
                "repeated_phrase",
                f"{len(repeated_phrases)} phrase(s) of {REPEATED_PHRASE_WORDS}+ words recur "
                "across messages.",
                None,
            )
        )
    return findings


def _validate_progression(messages: Sequence[GeneratedMessage]) -> list[Finding]:
    """A sequence should not get louder as it goes.

    Escalation is the characteristic failure of an outbound sequence: each
    message asks for slightly more than the last until the seventh is a demand.
    The length trend is a crude proxy and is a warning rather than a failure,
    because a genuinely useful longer message at position 3 is legitimate.
    """

    findings: list[Finding] = []
    ordered = sorted(messages, key=lambda item: item.position)
    initial_words = len(_words(ordered[0].body)) if ordered else 0
    for message in ordered[3:]:
        if initial_words and len(_words(message.body)) > initial_words:
            findings.append(
                Finding(
                    SEVERITY_WARNING,
                    "follow_up_longer_than_initial",
                    "A late follow-up is longer than the initial message; follow-ups should "
                    "generally get shorter.",
                    message.position,
                )
            )
    return findings


def validate_sequence(
    messages: Sequence[GeneratedMessage],
    *,
    decision: ContextDecision,
    cadence: SequenceCadence,
    max_words_by_position: Mapping[int, int],
) -> ValidationFindings:
    """Validate every message and then the sequence they form."""

    findings: list[Finding] = []
    for message in messages:
        findings.extend(
            _validate_message(
                message,
                max_words=max_words_by_position.get(message.position, 150),
                decision=decision,
            )
        )
    findings.extend(_validate_structure(messages))
    findings.extend(_validate_timing(messages, cadence=cadence))
    findings.extend(_validate_repetition(messages))
    findings.extend(_validate_progression(messages))
    return ValidationFindings(findings=tuple(findings))
