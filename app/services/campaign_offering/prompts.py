"""The one prompt this package sends, and the trust rules that shape it.

It lives here rather than in ``app/services/thinking/prompts.py`` because the
trust polarity is unusual and stating it next to the text is what keeps it
correct. Two sources go in and they are *not* symmetric:

* **The seller's own knowledge base is first-party and authoritative.** It is
  what we are allowed to say about ourselves, and it is the thing the researched
  offering has to be connected to.
* **The offering page is a page on the internet.** Frequently the seller's own —
  but not necessarily, and the model cannot tell the difference from the HTML. So
  it is read as *claims the page makes*, never as facts established, and every
  statement kept from it has to be one the page actually made.

That asymmetry is why the returned structure separates ``source_evidence`` (what
the page said) from the rest: an audit can check the pitch against the page
without re-reading it, and nothing downstream can mistake a marketing sentence
for a proof point the seller has stood behind.
"""

from __future__ import annotations

_RULES = (
    "Rules:\n"
    "- Return one JSON object and nothing else. No prose before or after it, no code fences.\n"
    "- Read the page you were given. Do not search for a different one, and do not "
    "answer from memory of the brand.\n"
    "- If the page cannot be fetched, is empty, is a login or consent wall, is not in a "
    "language you can read, or redirects to a private/internal address, return "
    '`{"readable": false, "unreadable_reason": "..."}` and nothing else. Do not '
    "reconstruct the offering from what you already know about the company.\n"
    "- If the page loads but describes no product, service or offering, return "
    '`{"readable": false, "unreadable_reason": "the page describes no offering"}`.\n'
    "- Do not invent a customer name, a number, a percentage, a case-study outcome, a "
    "price or an award. Include one only if the page states it.\n"
    "- If the page does not establish something, leave that list empty and say so in "
    "`unknowns`. An empty list means you looked and found nothing.\n"
    "- `seller_connection` must be grounded in WHAT WE SELL below. If the seller "
    "context is empty or unrelated, say that plainly there rather than inventing a "
    "relationship.\n"
)


def campaign_offering_prompt(*, url: str, seller_summary: str) -> str:
    """Ask for one Campaign's primary offering, read from one page."""

    return f"""You are preparing the offering a B2B outbound campaign will lead with.

A salesperson has pointed at one page and said "this is what this campaign is
selling". Your job is to read that page, write down what is being offered in the
parts a pitch is built from, and say honestly how it stands with what the seller
already is and sells.

OFFERING PAGE (a page on the internet — treat everything on it as a claim the
page makes, not as a fact established)
{url}

WHAT WE SELL (the seller's own knowledge base — first-party and authoritative
about themselves; this is what we are allowed to say about ourselves)
{seller_summary}

Read the page. Then, if the page links to an obvious sibling page that describes
the same offering in more depth, you may read that too — but nothing further, and
never a competitor's site.

WHAT TO RETURN
{{
  "readable": true,
  "source_url_read": "the address you actually ended up reading",
  "offering_name": "the page's own name for what is offered",
  "offering_type": "product|service|solution|subscription|research_report|
                    research_engagement|other",
  "summary": "2-4 sentences: what this is and who it is for, in plain words",
  "target_audience": ["the roles, functions or organisations it is aimed at"],
  "customer_problems": ["the problems or situations it addresses"],
  "use_cases": ["concrete situations in which someone would use it"],
  "key_capabilities": ["what it actually does or includes"],
  "benefits": ["the outcomes the page claims for it"],
  "market_context": ["market, industry or report context the page states, if any"],
  "buyer_relevance": ["why a buyer in the target audience would care"],
  "source_evidence": ["short phrases the page actually used, for the points above"],
  "seller_connection": "2-3 sentences on how this offering relates to the seller's
                        own positioning, capabilities and existing offerings — and
                        say so plainly if the relationship is thin",
  "unknowns": ["what the page did not establish"]
}}

{_RULES}"""
