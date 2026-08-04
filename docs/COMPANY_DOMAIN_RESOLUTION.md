# Automatic company-domain resolution (DAT-017A)

What the system decides about a captured employer's domain, how sure it claims to
be, and what each level of certainty is allowed to authorize.

Read this before changing anything under `app/services/resolution/`, the
`company_domain_resolutions` table, or the promotion path in
`app/services/captures/promotion.py`.

Scope: this is the **practical v1** described by issue #171. Research-based
corroboration, multi-source weighted evidence, the APP-008 review queue, and
resolution metrics belong to DAT-017B / #183 and are deliberately not here.

## The problem it solves

A LinkedIn page never states a company domain. DAT-013 saves the capture,
DAT-014 lets an operator resolve the domain by hand through the DAT-010 logo.dev
candidate list, and until DAT-017A that hand step was required for every single
capture. The routine cases — a company we have already resolved, a company
already in the permanent store, an unambiguous provider answer — cost the same
operator attention as the genuinely hard ones.

The fix is not "decide more"; it is "say how sure you are, and mean it".

## The three states

| State | Means | Authorizes |
|---|---|---|
| `confirmed` | Evidence that was **already established** names this domain | Everything downstream |
| `provisional` | A provider-backed candidate, uncorroborated | Company research; everything else only for a Campaign that has accepted provisional domains |
| `unresolved` | Missing, ambiguous, conflicting, invalid, or the provider failed | Nothing; no domain is selected |

A fourth situation is deliberately *not* one of these: a company whose domain
never went through automatic resolution has **no decision record at all**. That
is not `unresolved`. A spreadsheet import and an operator's own confirmation were
never uncertain, and DAT-017A does not retroactively cast doubt on them. Code
reads this as `None` from `store.company_state`, and the gates treat it as
unrestricted.

`provider_only` is an operator-report label, not a fourth decision state. It
means a capture-scoped `SalesNavCompanyEnrichment` retains provider or model
candidates but no authoritative `CompanyDomainResolution` currently exists.
The candidates are useful evidence and remain visibly unconfirmed; the label
must never be passed through `store.company_state` or upgraded to `confirmed`.

## The policy, in order

`app/services/resolution/policy.py`, version
`company-domain-resolution/practical-v1`. Pure functions over a
`ResolutionEvidence` record — no database, no provider, no clock — so a stored
decision replays exactly.

1. **No company name** → `unresolved`. Nothing to look up.
2. **Approved mapping.** A domain already CONFIRMED for the same normalized
   company (with compatible LinkedIn company identity). Exactly one →
   `confirmed`, **no provider call**. More than one → `unresolved`
   (`conflicting_approved_mappings`).
3. **Existing permanent Company.** A Company matching by exact normalized name or
   by exact LinkedIn company id, which already carries a domain. One distinct
   domain → `confirmed`. Several → `unresolved`. A mapping and a Company naming
   different domains → `unresolved`.
4. **Provider candidates.** Only reached when 2 and 3 say nothing — which is also
   the only condition under which a provider call is authorized. Each candidate
   is checked for domain validity, then for suitability, then for name
   alignment. Exactly one survivor → `provisional`. None → `unresolved`.
   Several → `unresolved`.

### Provider evidence never reaches `confirmed`

The load-bearing decision. Issue #171 defines `confirmed` as "sufficient
deterministic evidence for normal downstream use" and `provisional` as "likely
provider-backed match ... not independently corroborated". One provider
answering one query is not independently corroborated, however well its name
matches. Corroboration is what #183 adds; until then the honest ceiling for a
provider candidate is `provisional`.

This also satisfies the rule the issue states twice — provider rank alone never
confirms. Rank is recorded so a reviewer can see what the provider thought. It is
never an input to the state.

### Alignment is exact, not fuzzy

A candidate aligns when its provider name, or its domain's registrable label,
normalizes to **exactly** the normalized company name. Normalization folds case,
punctuation, spacing and a trailing legal form, so `Acme Solutions, Inc.`,
`acme  solutions` and `acme-solutions.com` all meet at `acmesolutions`.

Substring and prefix matching were left out on purpose. `Acme` matches
`acmecorp.com`, `acme-dental.com` and `acmeholdings.io` equally well, and that is
exactly how an automatic resolver quietly attaches people to the wrong company.
Exact matching resolves fewer captures and misattributes far fewer; an unresolved
capture still reaches an operator with every candidate visible.

### Unsuitable domains

A brand search asked about a company it does not know answers with the places
that company *appears*. Five categories are rejected outright, matched on the
host and every parent of it (so `acme.wixsite.com` is rejected like
`wixsite.com`): social networks, directories and data aggregators, marketplaces,
generic platforms and mailbox providers, and parked or registrar domains.

The lists are conservative in both directions. A few entries — `google.com`,
`cloudflare.com` — are real companies somebody could genuinely be captured at.
Rejecting them costs one unresolved capture an operator settles by hand;
accepting them risks attaching a person to a platform they merely use. For
internal-use v1 that trade is worth making, and it is written down rather than
discovered later.

## Uncertainty must not launder itself

The subtlest failure mode, closed in two places. A `provisional` decision is a
guess; the danger is that guess coming back later disguised as evidence.

* **Through the mapping store.** A provisional decision writes *no* confirmation
  onto the DAT-010 record. If it did, `prior_confirmed_domains` would read it
  back as an approved mapping and the next capture would `confirm` from evidence
  nobody confirmed. Only `confirmed` decisions write there, with
  `EnrichmentConfirmationSource.AUTOMATIC_POLICY`.
* **Through the Company store.** A provisional decision *does* create a permanent
  Company, because research needs one to exist. So the next evaluation would find
  that Company and read "an existing company already has this domain" as settled
  — the guess citing itself. `_is_established` excludes any Company whose own
  current resolution state is provisional.

Both are covered by named tests. Removing either reopens the same hole by a
different route.

## What a decision stores

One append-only row per decision in `company_domain_resolutions`, at most one
current per capture (partial unique index). Each row carries: state, policy
version, original and normalized company name, the full candidate set with each
candidate's eligibility and rejection reason, the selected candidate, provider,
provider rank, deterministic reason codes, warnings, whether a paid provider call
happened, the decision timestamp and actor, and links to the capture, the DAT-010
candidate record and the resolved Company.

A check constraint makes the state and the domain agree: `unresolved` carries no
selected domain, and the other two must carry one. "Resolved, but to nothing" is
unrepresentable rather than merely discouraged.

## Idempotence

* **Retry** (`resolve` without `force`): returns the existing decision. No
  evidence is re-read, no provider is called, no row is written.
* **Recalculation** (`resolve(force=True)`): re-reads the evidence, but writes a
  new row only if the answer actually changed — state, domain, policy version,
  reasons and warnings are all compared. An identical re-evaluation keeps the
  original `decided_at`, so a decision never looks newer than the evidence
  behind it.
* **Provider calls**: authorized only when steps 2–3 decide nothing *and* the
  candidate store is empty. Stored candidates are always reused.
* **Companies and Contacts**: `companies.domain` is uniquely indexed, so a
  resolved domain reuses the company that owns it. Promotion is unchanged and
  still idempotent.

## Correction

An operator correction supersedes; it never edits or deletes. The earlier row
keeps its state, its candidates and its reasons, is marked `is_current = false`
with a `superseded_at`, and stays visible on the capture page.

A correction re-points the permanent `Contact.company_id`. It deliberately does
**not** rewrite `Contact.company_domain`: that string is captured evidence and
dedup input (`natural_key` is built from it), and APP-003 already treats a
disagreement between the two as a reviewable identity conflict. Rewriting one to
match the other would erase the disagreement instead of surfacing it. Both
company rows survive — a re-link is not a merge, and there are no silent merges.

Automatic recalculation **refuses** to run over an operator correction. Correct
the correction instead.

## Downstream gates

`app/services/resolution/gates.py`. **By default** a provisional domain opens
`COMPANY_RESEARCH` and refuses `FINAL_QUALIFICATION`,
`PERSONALIZED_DRAFTING`, `EMAIL_DISCOVERY`, `CAMPAIGN_ELIGIBILITY` and `SENDING`.
`unresolved` refuses all six. `confirmed`, and no-decision-at-all, allow all six.

**A Campaign may widen this.** `campaigns.allow_provisional_domains` (default
false) opens every stage to a provisional domain for that Campaign only, via
`gates.provisional_allows_for`. This is a product decision the operator makes per
audience: a campaign into a long tail of small firms will resolve very few domains
to `confirmed`, and waiting for corroboration that this release cannot produce
means never contacting them. Accepting a provisional domain means accepting that
some addresses are guessed at a guessed domain, and that bounces cost sending
reputation.

The switch is all-or-nothing across stages on purpose. A Campaign that verified
addresses on a guessed domain but then refused to draft from it would have already
spent the money the strict rule exists to protect.

Two things the switch does **not** reach. `unresolved` stays closed under it —
there is no domain to act on, so no setting can authorize acting on one. And
neither anti-laundering guard is affected: a provisional decision still writes
nothing to the approved-mapping store, and a provisional-backed Company is still
not established evidence. Those keep a guess from *becoming* certainty, which is a
different question from whether one Campaign is willing to act on a known guess.

A caller with no Campaign in scope — `generate_candidates`, the company workspace
— always gets the strict rule. A Contact can belong to several Campaigns, so
"the campaign's setting" is not a well-defined question there, and the permissive
answer must never be inherited by default.

The gate is enforced in the **service** that would otherwise act — today that is
`generate_candidates`, which is the single door to a paid MillionVerifier call
(`prepare_and_enqueue_contact` reaches the provider through it). A rule enforced
only in a route is one refactor away from not being enforced. Stages that do not
exist yet are named in the enum so that when they are built they wire into this
rule rather than each inventing its own reading of what provisional allows.

The Company Agent now applies the existing Research-readiness gate before it
can enqueue its Research child. A confirmed domain and a legacy no-decision
domain continue. A provisional domain continues to Research with its provisional
flag; the Campaign setting controls later domain-dependent stages. Unresolved
or missing domains block at Company and retain the exact reason in the job.

## CMP-003 execution lineage

Company Agent Studio does not create another resolver or decision ledger. For
new executions the existing `AgentJob.result` (or classified blocked error
detail) carries a bounded snapshot linking the execution to:

- the exact-match key and exact Company candidates actually considered;
- the selected permanent Company and whether the Contact edge was reused or
  linked (the current adapter does not create Companies);
- the exact capture and strongest Company-aggregate decision ids;
- the Company name/domain and resolution state seen by that execution;
- the effective `allow_provisional_domains` setting and Campaign settings
  version;
- the Research gate and later-stage eligibility result and reason.

The underlying decision row already preserves candidate order, rejected
candidates, selected candidate, source/provider, rank, policy, reasons,
warnings, observation scope, decision time and supersession. No migration or
duplicate persistence was needed.

The read model keeps four truths separate: the execution snapshot, the current
capture decision, the current Contact/Company aggregate, and current Campaign
policy. It never attaches today's Company domain to an older execution, rebuilds
candidate order from current policy, or infers missing confidence/provider
facts. Jobs predating CMP-003 therefore remain partial or unavailable when their
result did not pin enough lineage. That is an observability limitation, not a
request to run fuzzy retrospective matching or backfill guessed values.

Identity still owns person-level identity. Company owns employer linking and
domain authority. Research and Email only consume the resolved Company/domain.
Company Intelligence remains a separate classification system; its industry,
specialty, geography, queue and review concepts do not appear here.

## Feature switch

`FEATURES__AUTOMATIC_COMPANY_DOMAIN_RESOLUTION`, default **off**. While off, no
decision row is ever written and every capture behaves exactly as DAT-014 left
it. The lookup half additionally requires `salesnav_domain_enrichment` and a
configured `LOGO_DEV_API_KEY`; without them the policy decides from stored
evidence and reports the provider truthfully as not run.

## Known limitations (v1)

* **Hit rate is modest by design.** Exact alignment means many captures stay
  unresolved and reach an operator. That is the intended trade; loosening it is a
  product decision, not a bug fix.
* **No public-suffix list.** Two-part suffixes are recognised from a small
  hard-coded set. An unrecognised one makes a candidate fail to align, which
  leaves the capture unresolved rather than misattributed.
* **The name pre-filter has one blind spot.** A company name beginning with `&`
  normalizes its first token to `and`, which the raw name does not contain, so
  such a company is not found by the SQL pre-filter. A miss, never a mismatch.
* **Existing-company matching scans a filtered set** rather than an index, since
  the normalization has no SQL equivalent. Fine at internal-use scale; it would
  need a stored normalized-name column before it is not.
* **Enum labels and existing test databases.** This migration adds labels to
  `company_resolution_outcome` and `enrichment_confirmation_source`. The test
  suite builds its schema with `create_all`, which does not alter an existing
  enum type, so a developer with a pre-existing `vmr_test` database must drop it
  once. CI creates a fresh database and is unaffected.

## Not in this task

Research-dossier corroboration, multi-source weighted resolution, the APP-008
review queue, recommended operator decisions, reviewed mappings at scale,
automatic-resolution and provider-cost metrics, and the hardened reversible
policy for ambiguous cases — all DAT-017B / #183. Email discovery, qualification,
drafting, campaign execution and sending remain out of scope entirely.
