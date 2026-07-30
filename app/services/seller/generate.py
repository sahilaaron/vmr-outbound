"""Fill the seller knowledge base from the company's own website(s).

Typing a knowledge base in by hand is the step everyone skips, and skipping it is
expensive in a way that is easy to miss: the Personalization Agent writes from
seller context and stored evidence and nothing else, so an empty knowledge base
does not produce an error — it produces vague copy, and blames the model.

So this reads the seller's own sites and proposes the entries. Three properties
make that safe enough to write directly rather than staging a review queue:

* **Everything goes through the ordinary services.** ``save_profile``,
  ``create_offering``, ``create_proof_point`` and ``create_persona`` validate,
  audit and enforce their own uniqueness rules exactly as they do for a form
  submission. Nothing here writes a row itself.
* **Nothing existing is destroyed.** An offering or persona whose name already
  exists is skipped, not overwritten — an operator's own wording outranks a
  generated one. The profile is the single exception and is only written when
  there is no profile yet.
* **A refusal is reported, not swallowed.** Whatever could not be stored comes
  back with its reason, so a thin website produces a visibly thin result instead
  of a silent one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from app.models.enums import SellerOfferingType
from app.services.seller import records
from app.services.seller.common import SellerKnowledgeError
from app.services.seller.profile import get_profile, save_profile
from app.services.thinking import prompts
from app.services.thinking.contracts import Thinker, ThinkingError, ThinkingRequest

MAX_WEBSITES = 5
MAX_OFFERINGS = 8
MAX_PROOF_POINTS = 12
MAX_PERSONAS = 6


class KnowledgeBaseGenerationError(Exception):
    """The request could not be formed, or the model could not answer it."""


@dataclass
class GenerationResult:
    """What was created, what was skipped, and why."""

    profile_written: bool = False
    offerings: list[str] = field(default_factory=list)
    proof_points: int = 0
    personas: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)

    @property
    def created_anything(self) -> bool:
        return bool(self.profile_written or self.offerings or self.proof_points or self.personas)

    @property
    def summary(self) -> str:
        parts: list[str] = []
        if self.profile_written:
            parts.append("company profile")
        if self.offerings:
            parts.append(f"{len(self.offerings)} offering(s)")
        if self.proof_points:
            parts.append(f"{self.proof_points} proof point(s)")
        if self.personas:
            parts.append(f"{len(self.personas)} persona(s)")
        if not parts:
            return "Nothing could be established from those sites."
        text = "Created " + ", ".join(parts) + "."
        if self.skipped:
            text += f" {len(self.skipped)} entry(ies) skipped."
        return text


def parse_websites(raw: str) -> tuple[str, ...]:
    """Read one URL per line, tolerantly, and refuse anything that is not one.

    A bare domain is accepted and given a scheme: an operator typing their own
    company's site should not have to remember ``https://``.
    """

    seen: list[str] = []
    for line in (raw or "").splitlines():
        candidate = line.strip().strip(",")
        if not candidate:
            continue
        if "://" not in candidate:
            candidate = f"https://{candidate}"
        parts = urlsplit(candidate)
        host = parts.netloc
        # urlsplit is permissive enough to accept "https://not a url at all" with
        # that whole phrase as the host, so the host is checked rather than merely
        # being non-empty: no whitespace, and a dot, since a bare word is far more
        # likely to be a typo than an intranet name on this page.
        if (
            parts.scheme not in {"http", "https"}
            or not host
            or any(character.isspace() for character in host)
            or "." not in host
        ):
            raise KnowledgeBaseGenerationError(f"{line.strip()!r} is not a website address")
        if candidate not in seen:
            seen.append(candidate)
    if not seen:
        raise KnowledgeBaseGenerationError("enter at least one website address")
    if len(seen) > MAX_WEBSITES:
        raise KnowledgeBaseGenerationError(
            f"at most {MAX_WEBSITES} websites at a time — more than that makes one "
            "blended profile out of several different companies"
        )
    return tuple(seen)


def _text(value: Any, *, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned[:limit] if cleaned else None


def _list(value: Any, *, limit: int = 20) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value[:limit]:
        text = _text(item, limit=500)
        if text:
            out.append(text)
    return out


def _offering_type(value: Any) -> SellerOfferingType:
    try:
        return SellerOfferingType(str(value).strip().lower())
    except ValueError:
        return SellerOfferingType.OTHER


def generate_from_websites(
    session: Session,
    *,
    websites: tuple[str, ...],
    thinker: Thinker,
    timeout_seconds: float = 420.0,
    actor: str = "kb-generator",
) -> GenerationResult:
    """Read the sites and create the knowledge-base entries they support."""

    request = ThinkingRequest(
        prompt=prompts.knowledge_base_prompt(websites=websites),
        purpose="seller_knowledge_base",
        timeout_seconds=timeout_seconds,
        # The one place a language-model call here is allowed to browse: the
        # material is the seller's own public site.
        allowed_tools=("WebSearch",),
    )
    try:
        answer = thinker.think(request)
    except ThinkingError as exc:
        raise KnowledgeBaseGenerationError(exc.message) from exc

    payload = answer.payload
    result = GenerationResult(unknowns=_list(payload.get("unknowns"), limit=10))

    profile_payload = payload.get("profile")
    if isinstance(profile_payload, dict) and get_profile(session) is None:
        name = _text(profile_payload.get("name"), limit=255)
        if name:
            try:
                save_profile(
                    session,
                    name=name,
                    short_description=_text(profile_payload.get("short_description"), limit=2000),
                    description=_text(profile_payload.get("description"), limit=8000),
                    positioning=_text(profile_payload.get("positioning"), limit=4000),
                    communication_guidance=_text(
                        profile_payload.get("communication_guidance"), limit=4000
                    ),
                    industries_served=_list(profile_payload.get("industries_served")),
                    geographies_served=_list(profile_payload.get("geographies_served")),
                    capabilities=_list(profile_payload.get("capabilities")),
                    differentiators=_list(profile_payload.get("differentiators")),
                    updated_by=actor,
                )
                result.profile_written = True
            except SellerKnowledgeError as exc:
                result.skipped.append(f"company profile: {exc}")
    elif isinstance(profile_payload, dict):
        # An operator's own profile is not replaced by a generated one.
        result.skipped.append("company profile: one already exists and was left alone")

    for item in (payload.get("offerings") or [])[:MAX_OFFERINGS]:
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name"), limit=255)
        if not name:
            continue
        try:
            records.create_offering(
                session,
                name=name,
                offering_type=_offering_type(item.get("offering_type")),
                short_description=_text(item.get("short_description"), limit=2000),
                description=_text(item.get("description"), limit=8000),
                problems_addressed=_list(item.get("problems_addressed")),
                use_cases=_list(item.get("use_cases")),
                differentiators=_list(item.get("differentiators")),
                created_by=actor,
            )
            result.offerings.append(name)
        except SellerKnowledgeError as exc:
            result.skipped.append(f"offering {name!r}: {exc}")

    for item in (payload.get("proof_points") or [])[:MAX_PROOF_POINTS]:
        if not isinstance(item, dict):
            continue
        statement = _text(item.get("statement"), limit=4000)
        if not statement:
            continue
        try:
            records.create_proof_point(
                session,
                statement=statement,
                supporting_detail=_text(item.get("supporting_detail"), limit=4000),
                source_reference=_text(item.get("source_reference"), limit=1024),
                created_by=actor,
            )
            result.proof_points += 1
        except SellerKnowledgeError as exc:
            result.skipped.append(f"proof point: {exc}")

    for item in (payload.get("personas") or [])[:MAX_PERSONAS]:
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name"), limit=255)
        if not name:
            continue
        try:
            records.create_persona(
                session,
                name=name,
                role_function=_text(item.get("role_function"), limit=255),
                seniority=_text(item.get("seniority"), limit=120),
                responsibilities=_list(item.get("responsibilities")),
                challenges=_list(item.get("challenges")),
                use_cases=_list(item.get("use_cases")),
                created_by=actor,
            )
            result.personas.append(name)
        except SellerKnowledgeError as exc:
            result.skipped.append(f"persona {name!r}: {exc}")

    return result
