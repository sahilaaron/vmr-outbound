"""Typed, immutable Personalization policy snapshots.

The configuration is stored as JSONB so Agent Studio can evolve without a
migration for every wording change.  It is not free-shaped JSON: this module is
the single typed boundary that validates, serializes and turns each setting into
deterministic instructions.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.personalization_policy import (
    PersonalizationPolicyActivation,
    PersonalizationPolicyVersion,
)
from app.services.audit import record_audit_event

POLICY_SCHEMA_VERSION: Final = "personalization-policy/v1"
MAX_EXAMPLES: Final = 40
MAX_EXAMPLE_CHARS: Final = 1_200
MAX_CHANGE_NOTE_CHARS: Final = 2_000


class PolicyError(ValueError):
    """A policy snapshot cannot be safely stored or activated."""


class EnforcementStrength(enum.StrEnum):
    ADVISORY = "advisory"
    PREFERRED = "preferred"
    REQUIRED = "required"


class StandardState(enum.StrEnum):
    ENABLED = "enabled"
    UNAVAILABLE = "unavailable"


class Scale(enum.IntEnum):
    MINIMUM = 0
    LOW = 1
    BALANCED = 2
    HIGH = 3
    MAXIMUM = 4


class ExampleCategory(enum.StrEnum):
    STRONG = "strong_example"
    ACCEPTABLE = "acceptable"
    TOO_PERFORMATIVE = "too_performative"
    TOO_GENERIC = "too_generic"
    TOO_ASSUMPTIVE = "too_assumptive"
    TOO_DESCRIPTIVE = "too_descriptive"
    TOO_SALESY = "too_salesy"
    TOO_LONG = "too_long"
    WEAK_RELEVANCE = "weak_relevance"
    GOOD_QUESTION = "good_question"
    GOOD_FALLBACK = "good_fallback"


@dataclass(frozen=True)
class WritingStandard:
    identifier: str
    description: str
    wording: str
    strength: EnforcementStrength
    state: StandardState


@dataclass(frozen=True)
class Temperament:
    company_context_usage: Scale
    question_first_preference: Scale
    commercial_directness: Scale
    personalization_depth: Scale
    evidence_confidence_tolerance: Scale
    role_led_emphasis: Scale
    seller_introduction_timing: Scale
    assertive_tone: Scale


@dataclass(frozen=True)
class WritingStrategy:
    identifier: str
    name: str
    enabled: bool
    eligible_when: str
    evidence_required: tuple[str, ...]
    opening_shape: str
    introduction_placement: str
    cta_shape: str
    prohibited_behavior: tuple[str, ...]
    fallback_destination: str | None


@dataclass(frozen=True)
class PreferenceExample:
    category: ExampleCategory
    content: str
    note: str | None = None


@dataclass(frozen=True)
class EvidencePolicy:
    maximum_age_days: int


@dataclass(frozen=True)
class PolicyConfig:
    standards: tuple[WritingStandard, ...]
    temperament: Temperament
    strategies: tuple[WritingStrategy, ...]
    fallback_ladder: tuple[str, ...]
    examples: tuple[PreferenceExample, ...]
    evidence: EvidencePolicy

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": POLICY_SCHEMA_VERSION,
            "standards": [
                {
                    "id": standard.identifier,
                    "description": standard.description,
                    "wording": standard.wording,
                    "strength": standard.strength.value,
                    "state": standard.state.value,
                }
                for standard in self.standards
            ],
            "temperament": {
                key: int(getattr(self.temperament, key)) for key in _TEMPERAMENT_FIELDS
            },
            "strategies": [
                {
                    "id": strategy.identifier,
                    "name": strategy.name,
                    "enabled": strategy.enabled,
                    "eligible_when": strategy.eligible_when,
                    "evidence_required": list(strategy.evidence_required),
                    "opening_shape": strategy.opening_shape,
                    "introduction_placement": strategy.introduction_placement,
                    "cta_shape": strategy.cta_shape,
                    "prohibited_behavior": list(strategy.prohibited_behavior),
                    "fallback_destination": strategy.fallback_destination,
                }
                for strategy in self.strategies
            ],
            "fallback_ladder": list(self.fallback_ladder),
            "examples": [
                {
                    "category": example.category.value,
                    "content": example.content,
                    "note": example.note,
                }
                for example in self.examples
            ],
            "evidence": {"maximum_age_days": self.evidence.maximum_age_days},
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PolicyConfig:
        if not isinstance(raw, dict):
            raise PolicyError("Policy configuration must be an object.")
        if raw.get("schema_version") != POLICY_SCHEMA_VERSION:
            raise PolicyError(f"Policy schema must be {POLICY_SCHEMA_VERSION!r}.")

        standards_raw = _list(raw.get("standards"), "standards")
        standards: list[WritingStandard] = []
        seen_standard_ids: set[str] = set()
        for item in standards_raw:
            if not isinstance(item, dict):
                raise PolicyError("Every standard must be an object.")
            identifier = _identifier(item.get("id"), "standard id")
            if identifier in seen_standard_ids:
                raise PolicyError(f"Standard id {identifier!r} appears more than once.")
            seen_standard_ids.add(identifier)
            standards.append(
                WritingStandard(
                    identifier=identifier,
                    description=_text(item.get("description"), "standard description", 600),
                    wording=_text(item.get("wording"), "standard wording", 800),
                    strength=_enum(EnforcementStrength, item.get("strength"), "standard strength"),
                    state=_enum(StandardState, item.get("state"), "standard state"),
                )
            )
        missing = set(_REQUIRED_STANDARD_IDS) - seen_standard_ids
        if missing:
            raise PolicyError(f"Required standards are missing: {', '.join(sorted(missing))}.")
        disabled_core = {
            standard.identifier
            for standard in standards
            if standard.identifier in _REQUIRED_STANDARD_IDS
            and standard.state is not StandardState.ENABLED
        }
        if disabled_core:
            raise PolicyError(
                "Core outreach standards cannot be unavailable: "
                f"{', '.join(sorted(disabled_core))}."
            )

        temperament_raw = raw.get("temperament")
        if not isinstance(temperament_raw, dict):
            raise PolicyError("temperament must be an object.")
        scales = {field: _scale(temperament_raw.get(field), field) for field in _TEMPERAMENT_FIELDS}
        temperament = Temperament(**scales)

        strategies_raw = _list(raw.get("strategies"), "strategies")
        strategies: list[WritingStrategy] = []
        seen_strategy_ids: set[str] = set()
        for item in strategies_raw:
            if not isinstance(item, dict):
                raise PolicyError("Every strategy must be an object.")
            identifier = _identifier(item.get("id"), "strategy id")
            if identifier in seen_strategy_ids:
                raise PolicyError(f"Strategy id {identifier!r} appears more than once.")
            seen_strategy_ids.add(identifier)
            strategies.append(
                WritingStrategy(
                    identifier=identifier,
                    name=_text(item.get("name"), "strategy name", 120),
                    enabled=_boolean(item.get("enabled"), "strategy enabled"),
                    eligible_when=_text(item.get("eligible_when"), "strategy eligibility", 500),
                    evidence_required=tuple(
                        _text(value, "strategy evidence requirement", 80)
                        for value in _list(item.get("evidence_required"), "evidence_required")
                    ),
                    opening_shape=_text(item.get("opening_shape"), "opening shape", 500),
                    introduction_placement=_text(
                        item.get("introduction_placement"), "introduction placement", 500
                    ),
                    cta_shape=_text(item.get("cta_shape"), "CTA shape", 500),
                    prohibited_behavior=tuple(
                        _text(value, "prohibited behavior", 300)
                        for value in _list(item.get("prohibited_behavior"), "prohibited_behavior")
                    ),
                    fallback_destination=(
                        _identifier(item.get("fallback_destination"), "fallback destination")
                        if item.get("fallback_destination") is not None
                        else None
                    ),
                )
            )
        missing_strategies = set(_REQUIRED_STRATEGY_IDS) - seen_strategy_ids
        if missing_strategies:
            raise PolicyError(
                f"Required strategies are missing: {', '.join(sorted(missing_strategies))}."
            )
        if not any(strategy.enabled for strategy in strategies):
            raise PolicyError("At least one writing strategy must be enabled.")
        if not next(
            (
                strategy.enabled
                for strategy in strategies
                if strategy.identifier == "earnest_offering_led"
            ),
            False,
        ):
            raise PolicyError("The earnest offering-led fallback strategy must remain enabled.")
        allowed_evidence = {"contact", "company", "combined", "sector"}
        for strategy in strategies:
            unsupported = set(strategy.evidence_required) - allowed_evidence
            if unsupported:
                raise PolicyError(
                    f"Strategy {strategy.identifier!r} has unsupported evidence categories."
                )
        strategy_ids = {strategy.identifier for strategy in strategies}
        for strategy in strategies:
            if strategy.fallback_destination and strategy.fallback_destination not in strategy_ids:
                raise PolicyError(
                    f"Strategy {strategy.identifier!r} falls back to an unknown strategy."
                )

        ladder = tuple(
            _identifier(value, "fallback level")
            for value in _list(raw.get("fallback_ladder"), "fallback_ladder")
        )
        if ladder != _FALLBACK_LADDER:
            raise PolicyError("The evidence fallback ladder is fixed and must remain ordered.")

        examples_raw = _list(raw.get("examples"), "examples")
        if len(examples_raw) > MAX_EXAMPLES:
            raise PolicyError(f"A policy may contain at most {MAX_EXAMPLES} examples.")
        examples: list[PreferenceExample] = []
        for item in examples_raw:
            if not isinstance(item, dict):
                raise PolicyError("Every preference example must be an object.")
            examples.append(
                PreferenceExample(
                    category=_enum(ExampleCategory, item.get("category"), "example category"),
                    content=_text(item.get("content"), "example content", MAX_EXAMPLE_CHARS),
                    note=(
                        _text(item.get("note"), "example note", 500)
                        if item.get("note") is not None
                        else None
                    ),
                )
            )

        evidence_raw = raw.get("evidence")
        if not isinstance(evidence_raw, dict):
            raise PolicyError("evidence must be an object.")
        age = evidence_raw.get("maximum_age_days")
        if not isinstance(age, int) or isinstance(age, bool) or not 30 <= age <= 730:
            raise PolicyError("maximum_age_days must be an integer from 30 to 730.")

        return cls(
            standards=tuple(standards),
            temperament=temperament,
            strategies=tuple(strategies),
            fallback_ladder=ladder,
            examples=tuple(examples),
            evidence=EvidencePolicy(maximum_age_days=age),
        )


_TEMPERAMENT_FIELDS: Final = (
    "company_context_usage",
    "question_first_preference",
    "commercial_directness",
    "personalization_depth",
    "evidence_confidence_tolerance",
    "role_led_emphasis",
    "seller_introduction_timing",
    "assertive_tone",
)

_REQUIRED_STANDARD_IDS: Final = (
    "do_not_explain_company",
    "context_must_improve_relevance",
    "prefer_curiosity",
    "no_intelligence_display",
    "admit_weak_evidence",
    "explain_seller_offering",
    "match_strategy_to_evidence",
    "minimum_personalization",
)
CORE_STANDARD_IDS: Final = frozenset(_REQUIRED_STANDARD_IDS)

_FALLBACK_LADDER: Final = (
    "contact_and_company",
    "company_only",
    "contact_role_only",
    "sector_only",
    "offering_led",
)

_REQUIRED_STRATEGY_IDS: Final = (
    "relevant_question_first",
    "relevant_statement_then_question",
    "role_led_relevance",
    "company_context_relevance",
    "earnest_offering_led",
)


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise PolicyError(f"{label} must be a list.")
    return value


def _text(value: Any, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PolicyError(f"{label} must not be blank.")
    clean = value.strip()
    if len(clean) > limit:
        raise PolicyError(f"{label} must not exceed {limit} characters.")
    return clean


def _identifier(value: Any, label: str) -> str:
    clean = _text(value, label, 64)
    if not clean.replace("_", "").isalnum() or clean.lower() != clean:
        raise PolicyError(f"{label} must use lower-case letters, numbers and underscores.")
    return clean


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise PolicyError(f"{label} must be true or false.")
    return value


def _enum(enum_type: type[enum.Enum], value: Any, label: str) -> Any:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise PolicyError(f"{label} has an unsupported value.") from exc


def _scale(value: Any, label: str) -> Scale:
    if not isinstance(value, int) or isinstance(value, bool):
        raise PolicyError(f"{label} must be an integer from 0 to 4.")
    try:
        return Scale(value)
    except ValueError as exc:
        raise PolicyError(f"{label} must be an integer from 0 to 4.") from exc


def default_policy() -> PolicyConfig:
    standards = (
        WritingStandard(
            "do_not_explain_company",
            "Do not tell a prospect obvious facts about their own organisation.",
            "Never explain the recipient's own company back to them.",
            EnforcementStrength.REQUIRED,
            StandardState.ENABLED,
        ),
        WritingStandard(
            "context_must_improve_relevance",
            "Context earns a place only by making the seller's offer more relevant.",
            "Use context only when it creates a clear, useful connection to the offering.",
            EnforcementStrength.REQUIRED,
            StandardState.ENABLED,
        ),
        WritingStandard(
            "prefer_curiosity",
            "Ask honestly instead of pretending to know an internal priority.",
            "Prefer a relevant question over an unsupported statement about priorities.",
            EnforcementStrength.REQUIRED,
            StandardState.ENABLED,
        ),
        WritingStandard(
            "no_intelligence_display",
            "Research is not included merely to prove that research happened.",
            "Do not display intelligence for its own sake or summarize gathered facts.",
            EnforcementStrength.REQUIRED,
            StandardState.ENABLED,
        ),
        WritingStandard(
            "admit_weak_evidence",
            "Weak evidence should lead to less personalization, not invention.",
            "When evidence is weak, step down the fallback ladder without apology or pretence.",
            EnforcementStrength.REQUIRED,
            StandardState.ENABLED,
        ),
        WritingStandard(
            "explain_seller_offering",
            "The recipient must understand what the seller actually offers.",
            "State the seller's offering plainly enough that the recipient can evaluate relevance.",
            EnforcementStrength.REQUIRED,
            StandardState.ENABLED,
        ),
        WritingStandard(
            "match_strategy_to_evidence",
            "The opening form must follow the evidence that is actually available.",
            "Use only a writing strategy whose evidence requirements were deterministically met.",
            EnforcementStrength.REQUIRED,
            StandardState.ENABLED,
        ),
        WritingStandard(
            "minimum_personalization",
            "More personalization is not automatically better.",
            "Use the least personalization required to earn attention.",
            EnforcementStrength.REQUIRED,
            StandardState.ENABLED,
        ),
    )
    strategies = (
        WritingStrategy(
            "relevant_question_first",
            "Relevant question first",
            True,
            "A supported Company fact, credible Contact role, or meaningful combination exists.",
            ("contact", "company", "combined", "sector"),
            "Open with one honest question grounded in the selected context.",
            "Explain the seller only after the question establishes relevance.",
            "Ask whether the subject is worth exploring; do not demand a meeting slot.",
            ("leading question", "assumed priority", "research recital"),
            "earnest_offering_led",
        ),
        WritingStrategy(
            "relevant_statement_then_question",
            "Relevant statement followed by a question",
            True,
            "A current, supported Company fact has explicit offering relevance.",
            ("company", "combined"),
            "State one sourced fact briefly, then ask a curiosity-led question.",
            "Introduce the seller after the question, never inside the factual statement.",
            "Invite a reply to the question rather than asking for calendar time.",
            ("company summary", "praise", "unsupported implication"),
            "relevant_question_first",
        ),
        WritingStrategy(
            "role_led_relevance",
            "Role-led relevance",
            True,
            (
                "The Contact's recorded role creates a credible connection without "
                "guessing priorities."
            ),
            ("contact", "combined"),
            "Open on the responsibility area implied by the recorded role, phrased as a question.",
            "Introduce the offering immediately after the role connection.",
            "Ask whether the responsibility area includes the offered problem space.",
            ("claiming role ownership", "claiming a target or challenge"),
            "earnest_offering_led",
        ),
        WritingStrategy(
            "company_context_relevance",
            "Company-context relevance",
            True,
            "A supported, current Company fact materially changes the offering's relevance.",
            ("company", "combined"),
            "Use one short Company-context reference; never describe the organisation.",
            "Introduce the offering after the relevance link is clear.",
            "Ask a narrow question about whether the connection matters.",
            ("fact stacking", "company explanation", "intelligence display"),
            "relevant_question_first",
        ),
        WritingStrategy(
            "earnest_offering_led",
            "Earnest offering-led introduction",
            True,
            "No meaningful prospect context clears the evidence threshold.",
            (),
            "Open plainly with what the seller helps with and why it may be worth considering.",
            "Introduce the seller in the opening; lack of context is not hidden.",
            "Ask whether the offering is relevant or who owns it; do not manufacture specificity.",
            ("fake personalization", "generic compliment", "invented familiarity"),
            None,
        ),
    )
    return PolicyConfig(
        standards=standards,
        temperament=Temperament(
            company_context_usage=Scale.BALANCED,
            question_first_preference=Scale.HIGH,
            commercial_directness=Scale.BALANCED,
            personalization_depth=Scale.LOW,
            evidence_confidence_tolerance=Scale.LOW,
            role_led_emphasis=Scale.BALANCED,
            seller_introduction_timing=Scale.BALANCED,
            assertive_tone=Scale.LOW,
        ),
        strategies=strategies,
        fallback_ladder=_FALLBACK_LADDER,
        examples=(),
        evidence=EvidencePolicy(maximum_age_days=365),
    )


_TEMPERAMENT_INSTRUCTIONS: Final[dict[str, tuple[str, ...]]] = {
    "company_context_usage": (
        "Do not use Company context.",
        "Use Company context only when it is the clearest available relevance link.",
        "Use one Company fact when it materially improves relevance.",
        "Prefer a useful Company connection when it clears every evidence gate.",
        "Lead from Company context whenever a supported, relevant fact is available.",
    ),
    "question_first_preference": (
        "Prefer a relevant statement before any question.",
        "Usually establish relevance before asking a question.",
        "Choose question-first or statement-first from the selected strategy.",
        "Prefer an honest question as the opening.",
        "Always open with an honest, non-leading question when context permits.",
    ),
    "commercial_directness": (
        "Keep the commercial purpose understated.",
        "State the offering softly but clearly.",
        "State the offering plainly without hype.",
        "Make the commercial purpose clear early.",
        "Lead with a direct commercial proposition while preserving honesty.",
    ),
    "personalization_depth": (
        "Use at most one Company insight, combining it with a relevant role when useful.",
        "Use at most two connected Company insights, and omit either unless it adds relevance.",
        "Use at most two connected Company insights, combining role context only when useful.",
        "Use up to three connected Company insights only when each adds distinct relevance.",
        "Use up to three connected Company insights deeply, never to display research volume.",
    ),
    "evidence_confidence_tolerance": (
        "Require at least one complete supporting source with confidence of 0.90 or higher.",
        "Require at least one complete supporting source with confidence of 0.80 or higher.",
        "Require at least one complete supporting source with confidence of 0.70 or higher.",
        "Require at least one complete supporting source with confidence of 0.60 or higher.",
        "Require at least one complete supporting source with confidence of 0.50 or higher.",
    ),
    "role_led_emphasis": (
        "Do not lead from the Contact's role.",
        "Use role context only when Company context is unavailable.",
        "Balance Contact role and Company context by relevance.",
        "Prefer a credible role-led connection over Company description.",
        "Lead from the recorded role whenever it creates a credible connection.",
    ),
    "seller_introduction_timing": (
        "Introduce the seller only after relevance is established.",
        "Delay the seller introduction until after the opening question or connection.",
        "Introduce the seller after one short relevance line.",
        "Introduce the seller in the first two sentences.",
        "Introduce the seller and offering in the opening sentence.",
    ),
    "assertive_tone": (
        "Use a reserved, exploratory tone.",
        "Use a calm tone and avoid certainty about the recipient.",
        "Use a balanced, commercially confident tone.",
        "Use an assertive offer while keeping prospect claims qualified.",
        "Use a strongly assertive offer, never assertive guesses about the prospect.",
    ),
}


def temperament_instructions(config: PolicyConfig) -> tuple[str, ...]:
    return tuple(
        _TEMPERAMENT_INSTRUCTIONS[field][int(getattr(config.temperament, field))]
        for field in _TEMPERAMENT_FIELDS
    )


def minimum_confidence(config: PolicyConfig) -> float:
    return (0.90, 0.80, 0.70, 0.60, 0.50)[int(config.temperament.evidence_confidence_tolerance)]


def supporting_confidence(confidences: Iterable[float | None]) -> float:
    """Confidence of the strongest complete citation supporting one eligible claim.

    Insight eligibility separately requires every observation to be traceable and
    complete.  Once that evidence-integrity boundary has passed, a weaker secondary
    citation does not erase stronger support for the same non-conflicting claim.
    """

    return max((float(value or 0.0) for value in confidences), default=0.0)


def company_context_limit(config: PolicyConfig) -> int:
    """Deterministic Company-insight cap owned by personalization depth policy."""

    return (1, 2, 2, 3, 3)[int(config.temperament.personalization_depth)]


def create_policy_version(
    session: Session,
    *,
    configuration: PolicyConfig | dict[str, Any],
    name: str,
    actor: str,
    based_on_version_id: uuid.UUID | None = None,
    change_note: str | None = None,
) -> PersonalizationPolicyVersion:
    config = (
        configuration
        if isinstance(configuration, PolicyConfig)
        else PolicyConfig.from_dict(configuration)
    )
    clean_name = _text(name, "policy name", 160)
    clean_actor = _text(actor, "actor", 128)
    clean_note = change_note.strip() if change_note else None
    if clean_note and len(clean_note) > MAX_CHANGE_NOTE_CHARS:
        raise PolicyError(f"change note must not exceed {MAX_CHANGE_NOTE_CHARS} characters.")
    if (
        based_on_version_id is not None
        and session.get(PersonalizationPolicyVersion, based_on_version_id) is None
    ):
        raise PolicyError("The policy version selected as the base does not exist.")
    number = (
        int(
            session.scalar(
                select(func.coalesce(func.max(PersonalizationPolicyVersion.version_number), 0))
            )
            or 0
        )
        + 1
    )
    version = PersonalizationPolicyVersion(
        version_number=number,
        schema_version=POLICY_SCHEMA_VERSION,
        name=clean_name,
        configuration=config.to_dict(),
        validation_summary={"valid": True, "errors": [], "schema_version": POLICY_SCHEMA_VERSION},
        based_on_version_id=based_on_version_id,
        change_note=clean_note,
        created_by=clean_actor,
    )
    session.add(version)
    session.flush()
    record_audit_event(
        session,
        actor=clean_actor,
        action="personalization_policy.version_created",
        entity_type="personalization_policy_version",
        entity_id=str(version.id),
        new_state=f"v{number}",
        reason=clean_note or "immutable Personalization policy version created",
        context={
            "schema_version": POLICY_SCHEMA_VERSION,
            "based_on_version_id": str(based_on_version_id) if based_on_version_id else None,
        },
    )
    return version


def list_policy_versions(session: Session) -> tuple[PersonalizationPolicyVersion, ...]:
    return tuple(
        session.scalars(
            select(PersonalizationPolicyVersion).order_by(
                PersonalizationPolicyVersion.version_number.desc()
            )
        ).all()
    )


def active_policy(session: Session) -> PersonalizationPolicyVersion | None:
    activation = session.scalars(
        select(PersonalizationPolicyActivation).order_by(
            PersonalizationPolicyActivation.activated_at.desc(),
            PersonalizationPolicyActivation.id.desc(),
        )
    ).first()
    return (
        session.get(PersonalizationPolicyVersion, activation.policy_version_id)
        if activation
        else None
    )


def activation_history(session: Session) -> tuple[PersonalizationPolicyActivation, ...]:
    return tuple(
        session.scalars(
            select(PersonalizationPolicyActivation).order_by(
                PersonalizationPolicyActivation.activated_at.desc(),
                PersonalizationPolicyActivation.id.desc(),
            )
        ).all()
    )


def activate_policy(
    session: Session,
    *,
    policy_version_id: uuid.UUID,
    actor: str,
    reason: str | None = None,
) -> PersonalizationPolicyActivation:
    version = session.get(PersonalizationPolicyVersion, policy_version_id)
    if version is None:
        raise PolicyError("That Personalization policy version does not exist.")
    PolicyConfig.from_dict(dict(version.configuration))
    previous = active_policy(session)
    activation = PersonalizationPolicyActivation(
        policy_version_id=version.id,
        previous_policy_version_id=previous.id if previous else None,
        reason=reason.strip() if reason else None,
        activated_by=_text(actor, "actor", 128),
    )
    session.add(activation)
    session.flush()
    record_audit_event(
        session,
        actor=activation.activated_by,
        action="personalization_policy.activated",
        entity_type="personalization_policy_version",
        entity_id=str(version.id),
        previous_state=f"v{previous.version_number}" if previous else None,
        new_state=f"v{version.version_number}",
        reason=activation.reason or "Personalization policy activated",
        context={
            "activation_id": str(activation.id),
            "rollback": bool(previous and version.version_number < previous.version_number),
        },
    )
    return activation


def ensure_initial_policy(
    session: Session, *, actor: str = "system:agent-studio"
) -> PersonalizationPolicyVersion:
    current = active_policy(session)
    if current is not None:
        return current
    existing = session.scalars(
        select(PersonalizationPolicyVersion).order_by(PersonalizationPolicyVersion.version_number)
    ).first()
    version = existing or create_policy_version(
        session,
        configuration=default_policy(),
        name="Initial outreach standard",
        actor=actor,
        change_note="Initial versioned Personalization policy supplied by Agent Studio.",
    )
    activate_policy(
        session,
        policy_version_id=version.id,
        actor=actor,
        reason="Initial Personalization policy activation.",
    )
    return version
