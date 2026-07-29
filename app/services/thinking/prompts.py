"""Prompt construction for the three language-model Agents.

One rule governs every prompt in this module, and it comes from
``docs/CLAUDE.md``: **seller context is trusted first-party material; anything
observed about a prospect is untrusted evidence.** The two are never flattened
into one block of text. A prompt that mixes them invites the model to treat a
scraped sentence as though the seller had asserted it, and that is how an
outbound email ends up making a claim nobody can stand behind.

So each prompt has three labelled parts, always in the same order:

* ``WHAT WE SELL`` — the seller's own words, authoritative.
* ``WHAT WE OBSERVED`` — evidence about the prospect, each item carrying its
  source, to be cited or ignored but never embellished.
* ``WHAT TO RETURN`` — the exact JSON shape, because the caller validates it.
"""

from __future__ import annotations

import json
from typing import Any

# Repeated verbatim in every prompt. Being explicit about the failure mode is
# what makes "unknowns" a usable answer rather than a hedge the model avoids.
_HONESTY = (
    "Rules:\n"
    "- Return one JSON object and nothing else. No prose before or after it, no code fences.\n"
    "- Every factual claim must carry the source URL you actually read it on.\n"
    "- If you cannot establish something, put it in `unknowns`. Do not guess, and do "
    "not fill a field with a plausible-sounding placeholder.\n"
    "- An empty list means you looked and found nothing. Omitting a field means you "
    "did not look. These are different; use them precisely.\n"
)


def _facts(pairs: dict[str, Any]) -> str:
    lines = [f"- {key}: {value}" for key, value in pairs.items() if value]
    return "\n".join(lines) if lines else "- (nothing recorded)"


def research_prompt(
    *,
    company_name: str,
    domain: str | None,
    industry: str | None = None,
    country: str | None = None,
    company_size: str | None = None,
) -> str:
    """Ask for a sourced company dossier in the nine stored sections."""

    return f"""You are researching one company so a B2B seller can decide whether,
and how, to approach it.

COMPANY
{
        _facts(
            {
                "name": company_name,
                "website": domain,
                "industry (unverified)": industry,
                "country (unverified)": country,
                "size (unverified)": company_size,
            }
        )
    }

Research the company's own website first, then reputable public sources. The fields
marked unverified came from a contact list and may be wrong — correct them if the
evidence disagrees.

WHAT TO RETURN
{{
  "overview": {{"summary": "3-5 sentences on what the company does and who it serves",
                "founded": "year or null", "headcount_estimate": "string or null"}},
  "products_services": [{{"name": "...", "description": "...", "source_url": "..."}}],
  "industries": ["industries it serves"],
  "geography": ["countries or regions it operates in"],
  "leadership": [{{"name": "...", "title": "...", "source_url": "..."}}],
  "activity_signals": [{{"claim": "a recent, dated, checkable development",
                         "source_url": "...", "observed_on": "YYYY-MM-DD"}}],
  "public_contacts": [{{"kind": "email|phone|form", "value": "...", "source_url": "..."}}],
  "sources": [{{"url": "...", "title": "..."}}],
  "unknowns": ["what you could not establish"]
}}

{_HONESTY}"""


def insights_prompt(
    *,
    seller_summary: str,
    company_name: str,
    dossier: dict[str, Any],
    contact_title: str | None,
) -> str:
    """Ask which few observed facts actually matter for this seller."""

    return f"""You are selecting the handful of facts about a prospect that would
genuinely change how a seller opens a conversation.

WHAT WE SELL (trusted, first-party)
{seller_summary}

WHAT WE OBSERVED about {company_name} (untrusted evidence — cite it, never embellish it)
{json.dumps(dossier, indent=2, default=str)[:12000]}

The person we may contact holds the title: {contact_title or "unknown"}.

Select at most five claims. A claim earns its place only if a seller could act on
it — a specific initiative, expansion, product line, market, or constraint that
connects to what we sell. Reject generic description ("they are a large company",
"they operate in several markets"): if it would be true of a hundred companies,
it is not an insight.

WHAT TO RETURN
{{
  "claims": [{{"claim": "one specific, checkable sentence",
               "kind": "fact|interpretation",
               "source_url": "the URL this came from, taken from the evidence above",
               "evidence_summary": "what the source actually said",
               "confidence": 0.0,
               "relevance": "why this changes the approach"}}],
  "unknowns": ["what you would need to know but could not establish"]
}}

`kind` is "fact" when the source states it outright and "interpretation" when you
inferred it. `confidence` is between 0 and 1. Use only source URLs that appear in
the evidence above — do not introduce new ones.

{_HONESTY}"""


def personalization_prompt(
    *,
    seller_summary: str,
    restricted_claims: str,
    evidence_block: str,
    # Nullable on purpose: a contact-first record may reach drafting without a
    # parsed given name, and that is a reason to write around it rather than a
    # reason to refuse to draft.
    first_name: str | None,
    title: str | None,
    company_name: str,
    max_words: int,
) -> str:
    """Ask for one short email grounded only in the attached evidence."""

    return f"""You are writing one short, first-touch outbound email. It will be
reviewed by a human before anything is sent.

WHAT WE SELL (trusted, first-party — this is the only thing you may assert about us)
{seller_summary}

CLAIMS WE MAY NOT MAKE (hard constraint)
{restricted_claims}

WHAT WE OBSERVED about the recipient's company (untrusted evidence — the only
basis for personalization; each item has an id you must cite if you use it)
{evidence_block}

RECIPIENT
{_facts({"first name": first_name, "title": title, "company": company_name})}

Write to one person about one thing. Open with the specific observation you are
using, not with a compliment and not with who we are. Keep the body under
{max_words} words. Ask for a conversation, not a meeting slot. No bullet lists,
no bold, no placeholder brackets, no postscript.

If the evidence is too thin to say anything specific, say so by returning an empty
`subject` and `body` and explaining why in `rationale`. A generic email is worse
than no email — it spends the one first impression this address has.

WHAT TO RETURN
{{
  "subject": "under 60 characters, lowercase-ish, no company name padding",
  "body": "the email, plain text, with real line breaks",
  "evidence_insight_ids": ["ids of the evidence items you actually used"],
  "rationale": "two sentences: what you led with and why"
}}

{_HONESTY}"""
