"""The one prompt Company Intelligence sends (CI-001).

Kept in this package rather than added to :mod:`app.services.thinking.prompts`
because it answers a different kind of question. The Phase 2 prompts ask a model
to *select* or to *write*; this one asks it to **classify material it is shown**,
and the difference shows up in the rules: there is no seller context here at all,
because what a company is does not depend on who is selling to it, and mixing the
two would invite the model to classify a prospect by what would be convenient.

Three parts, always in the same order:

* ``WHAT WE KNOW`` — the committed evidence, each fact carrying a short handle.
* ``WHAT TO CLASSIFY`` — the closed dimension list and the controlled vocabulary
  for the dimensions that have one.
* ``WHAT TO RETURN`` — the exact JSON shape, because the caller validates it and
  refuses what does not parse.

The evidence handles (``F1``, ``F2``…) exist because a citation has to be
checkable. A model asked to echo a UUID gets one wrong often enough to matter,
and a citation that does not resolve is indistinguishable from an invented one —
so the producer would have to treat both the same way, which means discarding
correct work. Short handles make the citation cheap to get right and trivial to
verify.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from app.models.enums import IntelligenceDimension

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.services.company_intelligence.inputs import IntelligenceInput

#: What each dimension means, in one line, so the model is not guessing at the
#: boundary between (say) a service and a capability.
DIMENSION_GUIDE: dict[IntelligenceDimension, str] = {
    IntelligenceDimension.INDUSTRY: (
        "the broad industry the company operates in; mark exactly one as primary"
    ),
    IntelligenceDimension.SUBINDUSTRY: (
        "the narrower category under an industry, when the evidence is specific enough"
    ),
    IntelligenceDimension.PRODUCT: "a named thing the company makes or sells",
    IntelligenceDimension.SERVICE: "a named service the company performs for customers",
    IntelligenceDimension.SPECIALTY: (
        "a distinguishing focus, accreditation or niche the company claims"
    ),
    IntelligenceDimension.CAPABILITY: (
        "something the company can do — a process, a technology, an in-house facility"
    ),
    IntelligenceDimension.GEOGRAPHY: (
        "a specific place the company has a presence in (country, state, city)"
    ),
    IntelligenceDimension.OPERATING_MARKET: "a broad region the company sells into",
    IntelligenceDimension.CUSTOMER_SEGMENT: "the type of customer the company sells to",
    IntelligenceDimension.BUSINESS_MODEL: "how the company makes money",
    IntelligenceDimension.COMPANY_TYPE: "what kind of organisation this is",
}

_RULES = (
    "Rules:\n"
    "- Return one JSON object and nothing else. No prose before or after it, no code fences.\n"
    "- Classify ONLY from the evidence above. You have no other sources and no tools.\n"
    "  Do not use anything you may recall about this company from elsewhere.\n"
    "- Every classification must cite at least one evidence handle. A value you cannot\n"
    "  cite does not belong in `classifications` — if you believe it anyway, leave it out.\n"
    "- Prefer a canonical value from the vocabulary when one fits. If none fits, write\n"
    "  your own wording; it will be recorded as unmapped and reviewed by a person.\n"
    "- If the evidence supports two answers that cannot both be true, do not choose.\n"
    "  Put both in `classifications` and describe the disagreement in `conflicts`.\n"
    "- If the evidence says nothing about a dimension, name it in `unknown_dimensions`.\n"
    "  That is a useful answer. A plausible guess is not.\n"
    "- `confidence` is your own certainty from 0 to 1. It is recorded as an opinion,\n"
    "  never as verification, so there is nothing to gain by inflating it.\n"
)


def _vocabulary_block(vocabularies: dict[str, list[str]]) -> str:
    if not vocabularies:
        return "(no controlled vocabulary is available; write your own wording)"
    lines: list[str] = []
    for dimension, values in vocabularies.items():
        lines.append(f"- {dimension}:")
        lines.append("  " + "; ".join(values))
    return "\n".join(lines)


def _evidence_block(source: IntelligenceInput) -> str:
    lines: list[str] = []
    for fact in source.facts:
        sources = ", ".join(item.source_url for item in fact.evidence) or "no source recorded"
        lines.append(f"[{fact.ref}] {fact.claim}  (source: {sources})")
    if not lines:
        lines.append("(no individual sourced facts were recorded)")
    return "\n".join(lines)


def classification_prompt(
    source: IntelligenceInput,
    *,
    vocabularies: dict[str, list[str]],
    dossier_char_limit: int = 12000,
) -> str:
    """Ask for a structured classification of one company's committed evidence."""

    dimensions = "\n".join(
        f"- {dimension.value}: {DIMENSION_GUIDE[dimension]}" for dimension in IntelligenceDimension
    )
    dossier = json.dumps(source.dossier_sections, indent=2, default=str)[:dossier_char_limit]

    return f"""You are classifying one company from evidence that has already been
gathered and recorded. You are not researching it and you cannot look anything up.

COMPANY
- name: {source.company_name}
- website: {source.company_domain or "not recorded"}

WHAT WE KNOW — sourced facts (untrusted evidence: cite it, never embellish it)
{_evidence_block(source)}

WHAT WE KNOW — research dossier sections
{dossier}

WHAT TO CLASSIFY
{dimensions}

CONTROLLED VOCABULARY (use these exact words where one fits)
{_vocabulary_block(vocabularies)}

WHAT TO RETURN
{{
  "classifications": [
    {{"dimension": "industry",
      "value": "one value, not a list",
      "is_primary": true,
      "evidence": ["F1", "F4"],
      "confidence": 0.0,
      "rationale": "one short sentence on what in the evidence supports this"}}
  ],
  "conflicts": [
    {{"dimension": "industry",
      "values": ["the first answer", "the incompatible second answer"],
      "statement": "what disagrees, factually",
      "evidence": ["F2", "F7"]}}
  ],
  "unknown_dimensions": ["dimensions the evidence says nothing about"]
}}

`dimension` must be one of the values listed under WHAT TO CLASSIFY. `evidence`
holds handles from the sourced facts above — nothing else, and never a handle
you did not see. Give one object per value: three products means three entries.

{_RULES}"""
