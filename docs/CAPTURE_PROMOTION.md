# Capture promotion — company resolution and contact promotion (DAT-014)

How a person saved by the capture extension becomes a canonical Contact.

```
LinkedIn / Sales Navigator capture   (DAT-013 — permanent, immutable evidence)
        ↓ visible company name + LinkedIn company hints
logo.dev company-name → domain candidates            (DAT-010, unchanged)
        ↓ operator reviews, confirms, or rejects
canonical Company resolution                          (exact unique domain)
        ↓
canonical Contact promotion                           (exact identity only)
        ↓
later company research, qualification, email discovery
```

## Why the bridge exists

A `Contact` requires `company_domain`: it is the deduplication key, the
email-generation key, and it is `NOT NULL`. A LinkedIn page never shows a
domain, and deriving one from a company name is a guess. DAT-013 therefore
reports `created: 0` and stores the person as permanent capture evidence.
DAT-014 resolves the domain through the path the system already has, and only
then creates the contact.

**Never fabricate a domain** and **never merge on weak evidence** are the two
rules everything below serves.

## What is reused

| Concern | Where it already lived | Change |
| --- | --- | --- |
| Provider client | `app/services/enrichment/logodev.py` | none |
| Candidate lookup, confirmation, rejection | `app/services/enrichment/companies.py` | confirmation refactored to be record-based so both paths share it; rejection added |
| Candidate storage | `salesnav_company_enrichments` | generalized: a record is owned by a batch **or** a capture, enforced by a check constraint |
| Exact-URL person matching | `app/services/profiles/refresh.py` | none |
| Deterministic deduplication | `app/services/imports/dedup.py` | none |
| Suppression authority | `app/services/suppressions.py` | none |
| Field provenance and freshness | `app/services/provenance/` | none |
| Labels and append-only notes | `app/services/captures/labels.py` | none |

There is deliberately **no** second candidate store, identity resolver,
provenance ledger, or company matcher.

## Company-resolution policy

| Outcome | When |
| --- | --- |
| `pending_lookup` | No lookup has run yet |
| `existing_company_resolved` | A domain the operator **already confirmed** for the same normalized company is reused |
| `domain_candidate_confirmed` | The operator confirmed a candidate or typed a domain for this capture |
| `candidate_review_required` | One candidate is waiting for a decision |
| `multiple_candidates_review_required` | Several candidates are waiting |
| `no_candidate` | The provider found nothing, every candidate was rejected, or the page showed no company name |
| `company_identity_ambiguous` | Two earlier confirmations of this company name disagree |
| `lookup_unavailable` | Provider unreachable, rate-limited, or a malformed body — retryable |
| `left_unresolved` | The operator deliberately left it unresolved |

### Automatic confirmation

Allowed in **exactly one** case: a domain that an operator already confirmed for
the same normalized company name — and, when both records know it, the same
LinkedIn company identifier. That is a replay of the operator's own decision,
recorded with `confirmation_source = prior_mapping` so it is never mistaken for
a provider result.

Everything else stays a candidate:

* a provider's top-ranked result is a name match, not evidence of identity, and
  is never auto-confirmed no matter how few candidates come back;
* two disagreeing prior confirmations produce `company_identity_ambiguous`
  rather than a choice between them;
* a same-named company with a *different* LinkedIn company identifier is a
  different employer and its domain is not reused.

### Candidate evidence

Every lookup preserves, on the enrichment record:

original query (`lookup_query`) · normalized query (`normalized_query`) ·
captured LinkedIn company URL and identifier · captured location hint ·
provider (`logo.dev`) · lookup version · attempt count · retrieval timestamp ·
per-candidate name, domain, **rank**, and **confidence**.

`confidence` is always `null`. logo.dev's Search Brands API returns a brand name
and a domain and **no score**; recording the absence explicitly stops rank from
being read as confidence later. The workbench shows "not provided by this
provider" rather than an invented number.

A rejected candidate moves to `rejected_candidates` with its rejection reason,
the deciding operator and the decision time. A rejection is a decision worth
keeping, not a gap.

## Contact-promotion policy

Promotion requires a confirmed domain. Then, in order:

1. **Name.** The capture must show a first and a last name. A single-token name
   blocks promotion rather than inventing a surname.
2. **Suppression** (DAT-006), evaluated on the resolved domain and, when a
   contact matched, its email. A suppressed identity blocks promotion before
   anything is created; the suppression itself is never touched.
3. **Person identity**, strongest evidence first:
   * exactly one contact with this exact normalized LinkedIn URL →
     `contact_exact_match_linked`;
   * more than one → `contact_identity_ambiguous`, blocked;
   * otherwise the DAT-004 natural key (`first|last|domain`): one match links,
     several are ambiguous and block, none creates.
4. **Company row**, found or created by exact unique domain. An existing
   company's name, industry and other fields are never overwritten by a capture.
5. **Contact**, created with the captured name, company, title and profile URL.
   No email is invented.

| Outcome | Meaning |
| --- | --- |
| `contact_created` | A new canonical contact exists |
| `contact_exact_match_linked` | An existing contact was linked, not duplicated |
| `contact_identity_ambiguous` | Two or more candidates; nothing created or merged |
| `suppressed` | Blocked by the suppression ledger; nothing created |
| `already_promoted` | This capture already has a contact (idempotent retry) |
| `promotion_blocked` | Company unresolved, or the person cannot be named |

### What carries over

* **Labels** requested at capture time are resolved through the DAT-013 label
  registry and assigned to the promoted contact, additively and idempotently.
* **Notes** stay append-only. Their text, scope, author and creation time are
  untouched; only the contact link — null while the capture was unmatched — is
  filled in.
* **Provenance**: the capture's title, company name and LinkedIn URL are
  appended to the DAT-005 ledger dated when they were *observed*, and the
  versioned freshness policy decides what wins. Promotion does not get to
  override a manual override or newer evidence.

### What never happens

No Campaign choice invented by promotion · no email candidate or verification ·
no score or qualification · no draft or approval · no outreach readiness · no
change to the captured payload, profile fields, content hash, or experience
observations · no suppression weakened · no automatic merge. An optional
Campaign membership is created or reused only when the immutable capture
already carries an explicit filing request.

## Idempotency and failure

`contact_capture_promotions.capture_id` is unique, so a second promotion is
impossible at the database level, not merely discouraged in code. A retry
returns `already_promoted` with the original contact. A provider failure is
retryable and records the attempt count. A mid-promotion failure rolls back to
nothing — proven by a test that injects one.

## Durable execution reporting (CAP-002)

Admin Agent Studio reports the existing Capture workflow; it does not replace
it. `/admin/agents/studio/capture` and the exact-job Admin API share one typed,
frozen, query-only reader. Extension intake and each material promotion outcome
record a terminal Capture Agent Job with bounded `capture-agent-report/1`
lineage. Import rows and explicit manual/API Campaign enrollment use the same
report contract with source-specific sections.

The job result pins only safe decision facts and references:

- immutable snapshot/import/source record id and capture time;
- bounded captured person/employer fields, labels, note projection and field
  provenance when present;
- validation, exact duplicate candidates, suppression and rejection outcome;
- exact promotion outcome and Contact id, including created versus reused;
- filing request/status and exact Campaign Contact id when persisted;
- the next Identity Agent Job when one was actually created.

The extension payload and complete raw import mapping remain in their
authoritative source tables and are never copied or returned wholesale. The job
contains only the allowlisted bounded field projection described above. Safe
URLs discard credentials, queries and fragments, and notes are bounded. The
report does not rerun deduplication, query suppression providers, resolve
Identity or Company, promote, file, retry, enqueue or mutate anything.

Historical execution truth and current truth are deliberately independent. A
later Contact edit, label change, suppression, Campaign membership change or
merge may be shown as current state, but cannot rewrite the captured values,
historical Contact, filing result or Campaign Contact recorded by that
execution. If an older job never pinned a duplicate candidate or resulting
membership, that lineage is `partial` or `unavailable`; current data is not used
to fabricate it. No schema migration or guessed backfill was required.

## Operator workflow

`/contact-captures/pending` lists every capture without a contact. Opening one
shows the person, current title, captured company name, LinkedIn company hint,
capture source, labels, note, identity warnings, and the company-resolution
status — then offers: run or retry the lookup, confirm a candidate, enter a
domain by hand, reject a candidate with a reason, leave it unresolved, and
promote. A blocked promotion always shows why. Links to the resulting Contact
and Company appear on the same card.

The flow is deliberately small. It is not a company-management UI: renaming,
merging, or editing companies is not part of it.

Promotion is **not** operator-only. When the automatic-resolution switches below
are enabled, a capture may already have been resolved and promoted before an
operator ever opens this page — in the intake request itself or by the agent
worker. The pending list then simply does not contain it. This page remains the
authority for anything automation declined to decide.

## Boundaries

**The extension is not involved.** Its responsibility ends when DAT-013 accepts
the submission. It never calls logo.dev, never holds a provider key, and never
resolves a domain — the backend and the workbench own the provider lookup,
candidate storage, operator confirmation, company resolution, and promotion.

## Feature switches

All switches default to **off** (`app/core/features.py`), so none of the
automatic behaviour below happens unless it is deliberately enabled.

| Switch | Required for | Enforced at |
| --- | --- | --- |
| `FEATURES__CONTACT_CAPTURE_PROMOTION` | the promotion flow at all | `app/services/resolution/pending.py:138`, `app/services/captures/intake.py:1063` |
| `FEATURES__AUTOMATIC_COMPANY_DOMAIN_RESOLUTION` | resolving a domain *without* an operator — both in-request and in the worker | `pending.py:147`, `intake.py:1064` |
| `FEATURES__SALESNAV_DOMAIN_ENRICHMENT` | any logo.dev provider lookup | `pending.py:157`, `intake.py:1073` |
| `LOGO_DEV_API_KEY` | the same provider lookup (a key, not a switch) | `pending.py:164`, `intake.py:1073` |
| `FEATURES__WORKBENCH` | the operator pages under `/contact-captures/` | `app/core/auth/startup.py`, which permits it in local development and staging and refuses it outright in production |

Two additional facts that are easy to get wrong:

- **Automatic promotion needs all four together.** `contact_capture_promotion`
  alone enables only the operator-driven flow. Automatic resolution — in the
  intake request *or* in the agent worker — additionally requires
  `automatic_company_domain_resolution`, `salesnav_domain_enrichment` and a
  configured `LOGO_DEV_API_KEY`. With any one of them missing the capture simply
  stays pending; `intake.py` deliberately declines to record a decision it could
  not actually make, because a recorded non-decision would stop the capture ever
  resolving automatically later.
- **`FEATURES__MODEL_COMPANY_DOMAIN_LOOKUP` is an optional extra resolver, not a
  prerequisite** (`pending.py:73,178`). It is consulted only when the provider
  path is already available.

Without a provider key the flow still works — the operator can enter a domain by
hand or leave the capture pending.

### Where promotion may run

`contact_capture_promotion` was local-only until the hosted Beta. It is now
permitted in exactly two places, and `app/core/runtime.py` decides:

| Environment | Rule |
| --- | --- |
| `local` / `development` / `test` / `ci` | Unchanged. The switch alone is enough, and the operator-driven flow needs no provider key. |
| `staging` | Permitted **only** with `FEATURES__AUTOMATIC_COMPANY_DOMAIN_RESOLUTION`, `FEATURES__SALESNAV_DOMAIN_ENRICHMENT` and a configured `LOGO_DEV_API_KEY`. Any one missing and startup is refused. |
| `production` | Refused outright. Production has no operator surface to review a promoted Contact on, and a production promotion path is a separate design. |

The staging prerequisites are the four things automatic promotion actually
needs, and the refusal exists because the failure without them is silent: the
services above fail closed and leave the capture untouched, which is safe and
correct and looks exactly like a deployment where nothing was ever captured. A
half-configured hosted promotion would accept captures, file campaign requests,
record every Capture job as succeeded, and promote nothing, with no error
anywhere. Startup refusing is what makes that state visible.

Nothing about what promotion may *conclude* changes with the environment. The
DAT-014 rules hold everywhere: no fabricated domain, no provider rank treated as
confirmation, no merge on weak evidence, no promotion of a suppressed identity,
and a Campaign Contact only where the immutable capture already carried an
explicit filing request.

### Where a Contact actually gets created

Intake never constructs a `Contact` (`app/services/captures/intake.py`,
`CANONICAL_CREATION_NOTE`). Promotion does, in `app/services/captures/promotion.py`,
reached by one of three routes:

1. **In the intake request**, when all four prerequisites above are satisfied.
   The pass is bounded — 40 % of the request budget, 15 s, 10 provider calls —
   and reports how many captures it promoted in the `auto_resolved` response
   count. Anything it does not finish is left pending on purpose.
2. **In the agent worker** (`scripts/run_agent_worker.py` → `resolve_pending`),
   where time is not bounded by an HTTP request.
3. **By an operator** in the workbench, which is the only route that needs no
   provider key.

## Current Contact and Company boundary

The application now has an authoritative `Contact.company_id` association.
Promotion may set it only through the existing exact confirmed-domain path and
also retains `resolved_company_id` and `resolved_domain` on the promotion row as
historical capture lineage. Capture preserves the employer name, LinkedIn
company hints and resolved promotion decision; the Company Agent remains the
authority for later Company linking, correction and canonical-domain state.
Capture reporting shows a later current Company association separately and does
not rewrite the promotion record.

Person-level ambiguity and merge decisions remain Identity authority. A pending
capture stays a `LinkedInProfileSnapshot`, and promotion creates or links a
Contact only through exact persisted evidence. Existing label assignments and
append-only capture notes keep their own provenance anchors; CAP-002 reads those
records and adds no universal provenance framework or schema change.

## Known limitations

* A live logo.dev call requires `LOGO_DEV_API_KEY`, and Brand Search needs the
  **secret** key (`sk_…`), not the publishable key used for logo image URLs.
  Automated tests always stub the provider, and the Layer 4A acceptance run stubs
  it at the HTTP boundary. The live pass (Layer 4B) was performed on 2026-07-26
  and passed; see `docs/LINKEDIN_CAPTURE_ACCEPTANCE.md`.
* A Sales Navigator results-row capture has no experience history, so its title
  and company come from the visible employment hint.
* Promotion resolves one company per capture — the current employer the page
  showed. Historical employers stay snapshot evidence.
* Country and industry are not inferred from a captured location or headline;
  they stay null until a source that actually states them provides them.
