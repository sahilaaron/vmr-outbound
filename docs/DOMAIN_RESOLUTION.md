# Automatic company-domain resolution (DAT-017)

What it does, what it refuses to do, and how to tell whether it is working.

Code: `app/services/enrichment/domain_policy.py` (the pure, versioned decision)
and `app/services/captures/domain_resolution.py` (evidence, storage, promotion).
Feature flag: `FEATURES__AUTOMATIC_DOMAIN_RESOLUTION`, **off by default**.

---

## The problem it solves

DAT-014 required an operator to confirm a domain for every captured company,
because a logo.dev candidate is a name match and rank is not confidence. That
was correct and it did not scale: capturing thirty people from one conference
meant thirty confirmations, most of them obvious.

The unlock was not trusting the provider more. It was noticing that DAT-012G had
been storing, and never consulting, a second and better source: the **website
domain an operator captured from LinkedIn's own company page**, keyed by the
same LinkedIn company identifier the person capture recorded. When that agrees
with a provider candidate, two independent sources have named the same domain,
and asking a human to retype it is friction rather than judgement.

## Evidence axes

An *axis* is a source that could be wrong on its own but is unlikely to be wrong
in the same direction as another. Corroboration is counted **across** axes,
never within one.

| Axis | Source | Identity-grade when |
| --- | --- | --- |
| `prior_mapping` | A domain an operator already confirmed for this company | always (keyed on normalized name + LinkedIn id) |
| `company_page` | `linkedin_company_snapshots.website_domain` | joined on LinkedIn company id or normalized company URL |
| `canonical_company` | An existing `companies` row carrying the domain | the stored name normalizes to the captured name |
| `provider_candidate` | A logo.dev candidate | never |

**Name agreement is not an axis.** A candidate whose brand name matches the
captured company name, or whose domain label spells it, is recorded as a *note*
on the evidence and never counted as corroboration. Both signals derive from the
same string that produced the provider query, so treating them as independent
would be circular — it would let a company called "Apex" auto-confirm
`apex.com` purely because the name matches itself.

## The decision table

Applied in order. First match wins.

| # | Condition | Decision |
| --- | --- | --- |
| 1 | Two authoritative axes name **different** domains | `conflict` |
| 2 | Exactly one prior mapping, unopposed | `prior_mapping_reused` |
| 3 | Two independent axes agree on one domain, **or** an identity-matched company page names it | `auto_confirmed` |
| 4 | Provider answered, nothing usable, no other evidence | `no_credible_candidate` |
| 5 | Provider unreachable, no other evidence | `provider_unavailable` |
| 6 | Anything else | `review_required` (+ a recommendation) |

Conflict is checked **first** on purpose. A conflict discovered after a selection
is a silently wrong answer, and preferring one source by rule would resolve the
case quietly and wrongly about half the time.

### What this means in practice

| Situation | Result |
| --- | --- |
| Company page (same LinkedIn id) + provider agree | auto-confirmed |
| Company page (same LinkedIn id), no provider result | auto-confirmed, **no provider call** |
| Prior confirmation exists | reused, **no provider call** |
| One provider candidate, nothing else | review |
| One provider candidate whose name and domain both echo the company name | review |
| Three candidates, one matching a captured company page | auto-confirmed |
| Three candidates, nothing to check them against | review |
| Prior mapping says A, company page says B | conflict |
| Two prior confirmations disagree | conflict |
| Provider unreachable | `provider_unavailable`, no domain invented |

## What is stored

On `salesnav_company_enrichments`, for every run including the ones that resolve
nothing:

`resolution_policy_version` · `resolution_decision` · `resolution_reasons`
(ordered stable codes) · `resolution_evidence` (every domain considered, its
axis, whether the join was identity-grade, and the record it came from) ·
`resolution_recommendation` · `resolved_at`.

Plus, when an operator later replaces an automatic domain with a different one:
`resolution_corrected_at` and `resolution_corrected_from`.

The **applied** domain still lives in `confirmed_domain` / `confirmation_source`
exactly as before. An automatic decision and an operator decision are the same
kind of fact, so every existing reader keeps working unchanged.

## What it will not do

* Never invents a domain. An unreachable provider yields `provider_unavailable`,
  not a guess.
* Never auto-confirms on provider rank, or on a candidate being the only one.
* Never overwrites an operator's decision. The policy acts only on `unconfirmed`
  records; `manual`, `candidate` and `unresolved` are left alone.
* Never relaxes a promotion gate. Suppression, identity ambiguity and a missing
  surname block an automatically resolved capture exactly as they block a
  manually resolved one — promotion still runs through the unchanged DAT-014
  `promote()`.
* Never calls the provider when the answer is already known, and never calls it
  twice for the same company without an explicit refresh.

## Provider-call behaviour

The provider is consulted **last**, and only when it could still change the
answer. A prior mapping or an identity-matched company page settles the question
first. That is the difference between an enrichment bill that scales with new
companies and one that scales with captures.

## The review boundary (#172 / APP-008)

Unresolved companies are exposed by
`domain_resolution.pending_reviews()` as `ReviewItem`s carrying subject type and
id, the blocked action, reason codes, the evidence, a recommendation, and a
reusability flag.

This is deliberately a **projection over the existing enrichment record**, not a
new table. That record already is the company-review queue: it has the subject,
the evidence, the decision history and an idempotent one-row-per-capture shape.
A second queue would mean two places to look for the same unresolved company and
two places for them to disagree. APP-008 can consume `ReviewItem` without
DAT-017 being reworked.

## Metrics

`domain_resolution.metrics(session, since=None)` returns:

automatic-resolution rate · review rate · **correction rate** · provider calls ·
records that cost at least one call · a per-decision breakdown.

Correction rate is measured against *automatic decisions*, not all decisions, so
review volume cannot dilute it. It is the number that keeps the automatic rate
honest: a high automatic rate is only good news while corrections stay near
zero. Re-affirming the same domain is agreement and is deliberately not counted
as a correction.

## Known limitations

* **Company-page evidence only exists where an operator captured that page.**
  Most companies have none yet, so early automatic rates will be dominated by
  prior-mapping reuse. This improves as the company-page corpus grows.
* **Country, industry and brand-consistency signals are not used.** The issue
  lists them as possible evidence; the repository captures country and industry
  only on company-page snapshots, where the domain is already present and
  decisive, so they would add nothing today. Adding them would be inventing
  evidence.
* **The company-page scan is a full table read**, filtered in Python rather than
  by an indexed join, because there is no foreign key between a person capture's
  employer hints and a company snapshot. Fine at current volume; the natural fix
  is a `companies.linkedin_company_id` column, which belongs to APP-003.
* **`review_required` has no operator UI yet.** The records are durable and
  queryable; the surface is APP-008.
