"""Prompt construction for the language-model Agents.

Two Agents use a model: **Insights**, which chooses among facts the Research Agent
already stored, and **Personalization**, which writes copy from them. Research does
not: it reads pages through the registered research workers and records what they
said, so every claim it stores is checkable against the page it came from.
``research_prompt`` below is therefore unused — see the note on it.

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
    """Ask for a sourced company dossier in the nine stored sections.

    **Nothing calls this, and nothing should.** It was written for a model-based
    Research adapter that has been removed: the Research Agent gathers through the
    registered research workers, which read real pages and attach a URL and a
    retrieval time to every fact. A model asked the same question returns prose that
    is equally plausible whether or not it read anything, and downstream the two are
    indistinguishable.

    Kept as the documented shape of a sourced dossier, not as a live path. Wiring it
    back in would reintroduce a second Research implementation — which is the defect
    this comment exists to prevent, so delete it rather than call it.
    """

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
    evidence_catalog: list[dict[str, object]],
    contact_title: str | None,
) -> str:
    """Ask which few observed facts actually matter for this seller."""

    catalog_json = json.dumps(evidence_catalog, indent=2, default=str)

    return f"""You are selecting the handful of facts about a prospect that would
genuinely change how a seller opens a conversation.

WHAT WE SELL (trusted, first-party)
{seller_summary}

WHAT WE OBSERVED about {company_name} (untrusted evidence — cite it, never embellish it)
{json.dumps(dossier, indent=2, default=str)[:12000]}

COMMITTED RESEARCH EVIDENCE HANDLES
{catalog_json}

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
               "evidence_handles": ["UUID from the committed evidence catalog"],
               "confidence": 0.0,
               "relevance": "why this changes the approach"}}],
  "employee_size": {{
    "candidates": [{{
      "source_wording": "verbatim numeric workforce wording from the cited evidence",
      "evidence_handles": ["UUID from the committed evidence catalog"],
      "observation_context": "one of the bounded context labels listed below",
      "exact_count": "integer or null",
      "range_wording": "verbatim range or null",
      "rationale": "brief public rationale, never private reasoning"
    }}]
  }},
  "unknowns": ["what you would need to know but could not establish"]
}}

`kind` is "fact" when the source states it outright and "interpretation" when you
inferred it. `confidence` is between 0 and 1. Every claim and Employee Size
candidate must cite committed evidence handles. Employee Size candidates must
describe this subject Company, not its customers, parents, subsidiaries,
portfolio, offices, contractors, hiring plans or layoffs. Do not calculate
Employee Size from revenue, funding, offices, traffic or marketing adjectives.
Deterministic application code validates handles and computes all counts, bands,
dates, conflicts, freshness and final status; your proposed numbers are not
authoritative.

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


def knowledge_base_prompt(*, websites: tuple[str, ...]) -> str:
    """Ask for the seller's own knowledge base, read off their own sites.

    The one prompt in this module about the *seller* rather than a prospect, which
    inverts the usual trust rule: here the websites ARE the first-party source, so
    what they say about the company can be taken at its word. What still may not
    be invented is anything they do not say — a customer name, a metric, an
    outcome. Those become proof points only if the site states them, because a
    proof point is something a salesperson will later be asked to stand behind.
    """

    listed = "\n".join(f"- {site}" for site in websites)
    return f"""You are reading a company's own public website(s) to write down what
they sell, in their own terms, so their sales team can use it consistently.

WEBSITES (these are the company's own — first-party, authoritative about themselves)
{listed}

Read the home page, then whatever pages describe the products, services,
industries served and customers. Prefer the company's own words over your
paraphrase where the wording is distinctive.

Two hard limits:
- Do not invent a customer name, a number, a percentage, a case-study outcome or
  an award. Include one only if the site states it, and put the page URL in
  `source_reference`. These become claims a salesperson will be asked to defend.
- If the site does not establish something, leave that field out or empty rather
  than filling it with something plausible.

WHAT TO RETURN
{{
  "profile": {{
    "name": "the company's own name for itself",
    "short_description": "one sentence",
    "description": "2-4 sentences on what they do and for whom",
    "positioning": "what they say makes them the right choice",
    "communication_guidance": "how they write — formal, plain, technical, etc.",
    "industries_served": ["..."],
    "geographies_served": ["..."],
    "capabilities": ["..."],
    "differentiators": ["..."]
  }},
  "offerings": [{{
    "name": "...",
    "offering_type": "product|service|solution|subscription|research_report|
                      research_engagement|other",
    "short_description": "one sentence",
    "description": "2-3 sentences",
    "problems_addressed": ["..."],
    "use_cases": ["..."],
    "differentiators": ["..."]
  }}],
  "proof_points": [{{
    "statement": "a specific, checkable claim the site actually makes",
    "supporting_detail": "what the site said around it",
    "source_reference": "the page URL it came from"
  }}],
  "personas": [{{
    "name": "a role, never a real person",
    "role_function": "...",
    "seniority": "...",
    "responsibilities": ["..."],
    "challenges": ["..."],
    "use_cases": ["..."]
  }}],
  "unknowns": ["what the site did not establish"]
}}

{_HONESTY}"""
