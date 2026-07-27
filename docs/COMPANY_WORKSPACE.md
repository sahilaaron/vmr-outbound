# The company workspace (APP-003)

The operator workspace for permanent organisations, and the schema a research
engine will land in. This document describes what is built, what is deliberately
not, and the rules that govern both.

Architecture and reasoning: `docs/decisions/0002-contact-first-architecture.md`.
The contact side of the same model: `docs/CONTACT_CRM.md`.

## The rules this release enforces

* **Company intelligence belongs to the Company, not to a Campaign.** No service
  in `app/services/companies/` accepts a campaign identifier, no company page
  offers a campaign control, and a company with no outreach history is a normal
  company rather than an incomplete one.
* **One Company, many Contacts.** Everyone who works somewhere shares one
  workspace. Two contacts at the same employer do not each get their own copy of
  the company's research.
* **Research is evidence, not an overwrite.** A dossier claiming an industry has
  not set one. The claim is recorded in the provenance ledger and the versioned
  freshness policy decides what wins, exactly as it does for contacts.
* **Unknown is not false, and not empty.** A field nobody has observed is
  unknown. A dossier section a version did not address is unknown. A section it
  addressed and found nothing in is empty. Three different facts, stored
  differently and displayed differently.
* **Identity conflicts stay visible.** When sources disagree about a domain or a
  LinkedIn identity, the disagreement is shown rather than resolved by a guess.
  Nothing is merged automatically.
* **Captured third-party text is untrusted evidence.** Everything in a research
  payload originated outside this system. It is displayed, quoted and cited —
  never obeyed. No component may treat stored text as an instruction.
* **No research engine lives in the web application.** APP-004 owns producing
  research. This release owns receiving it.

## Company identity has two axes

| Axis | Column | Unique? |
| --- | --- | --- |
| Canonical domain | `companies.domain` | yes, when present (partial index) |
| LinkedIn company | `companies.linkedin_company_url` / `linkedin_company_id` | **no** |

The LinkedIn identifier is deliberately not unique. Two companies claiming one
identifier is a disagreement worth seeing, and rejecting the second write would
destroy the evidence that it exists. The conflict is surfaced instead.

A company with no domain is unresolved, not invalid. Every domain-based identity
check is then *silent* rather than satisfied, and the workspace says so — because
an empty conflict list would otherwise read as agreement.

## The contact edge

`contacts.company_id` is the permanent link. `contacts.company_domain` stays,
stays `NOT NULL`, and is still what `natural_key` is built from.

They are allowed to disagree. The captured domain is evidence of what a source
said at capture time and is never rewritten; the company's canonical domain can
be corrected afterwards. When they differ, that is a reviewable conflict rather
than a bug.

### What the backfill did, and what it refused to do

`company_id` was set **only** where exactly one company carried the contact's
domain. It was left NULL when:

* no company carried that domain;
* more than one did;
* the domain was blank or whitespace.

An unlinked contact stays visible and reviewable afterwards. A wrongly linked one
does not, which is the whole argument for refusing to guess. The statement is
idempotent — it only touches rows where `company_id IS NULL` — so a rerun can
never overwrite a link that a person or a later process already made.

Contacts reachable only by the domain string are shown on the company page in
their own group, labelled transitional, and reported as a conflict. They are not
counted as linked contacts anywhere.

### Accepted limitation

`company_id` is nullable and stays nullable in this release. Making it `NOT NULL`
is a decision for after the backfill has converged in a real database, not
something to assert on the strength of a migration that has only run on test
data.

## Canonical fields and their provenance

`company_field_values` is the company equivalent of `contact_field_values`, and
reuses the same freshness policy (`freshness-v1`) — a company field has no reason
to age differently from a contact field.

Tracked fields: `industry`, `country`, `company_size`.

`domain` and `name` are deliberately **not** tracked. Changing a domain changes
company *identity*, and identity is not a freshness question: a source claiming a
different domain is a conflict to review, not an observation to out-age the
current one. `name` is what an operator recognises the company by, and letting an
automatic source rewrite it would make the workspace unrecognisable without
anyone having decided anything.

Source kinds are provider-neutral by construction: `manual`,
`linkedin_company_snapshot`, `capture_promotion`, `research_dossier`, `import`.
The ledger records that *a* dossier claimed something, never which engine, model
or vendor produced it. Swapping the research implementation must not require a
schema change.

Exactly one observation per `(company, field)` is the current winner, enforced by
a partial unique index rather than by code.

## Dossiers: two tables, one boundary

| Table | Holds | Mutable? |
| --- | --- | --- |
| `company_research_submissions` | one raw payload, verbatim | no |
| `company_dossier_versions` | one structured reading of one submission | no |

A submission is true by definition — it records what arrived. A version is a
claim, and claims can be wrong and get superseded. Keeping them apart is what
lets an extractor improve without either losing the original payload or
rewriting it for every re-read.

The nine sections are **columns, not a blob**: `overview`, `products_services`,
`industries`, `geography`, `leadership`, `activity_signals`, `public_contacts`,
`sources`, `unknowns`. The boundary is closed on purpose. A research
implementation that wants a tenth section needs a schema change and a review, not
a new key; an unknown key is rejected rather than silently dropped.

`NULL` in a section means the version did not address it. A present-but-empty
value means it looked and found nothing. Collapsing those would turn "we do not
know" into "there is none", which is the failure this whole model exists to
prevent.

At most one version per company is current, enforced by a partial unique index.
Selecting a different one supersedes the previous — it does not delete it.

### Ownership is a schema guarantee, not a service check

A dossier version must interpret a submission about the **same** company. That is
enforced by a composite foreign key:

```
company_dossier_versions (submission_id, company_id)
  -> company_research_submissions (id, company_id)
```

`interpret()` also validates it, but a service check only protects the path that
calls it. A direct write, a data migration, a fixture or a future import path can
all reach the table without passing through the service, and a dossier attributed
to the wrong organisation is the kind of wrong that reads as fact.

A single-column key on `submission_id` would prove the submission exists and say
nothing about whose it is. Referencing `(id, company_id)` is what makes the pair
inseparable, which is why it *replaces* the narrower key rather than supplementing
it — it already implies everything that one guaranteed. `company_research_submissions`
therefore carries a `(id, company_id)` unique constraint: redundant against its
primary key, and required, because a composite foreign key must reference a
uniquely-constrained set of columns.

The key is `NO ACTION` rather than `RESTRICT`. Both refuse to orphan a version
when a submission is deleted directly — an interpretation without its payload is
an unfalsifiable claim — but `RESTRICT` is checked immediately while `NO ACTION`
defers to the end of the statement. That difference is what lets
`DELETE FROM companies` cascade into both tables in one statement without the
check firing on a half-applied intermediate state. Both behaviours have their own
regression test.

## Research state

Reuses `ResearchState`, the same vocabulary the contact CRM already displays.
Only three values are reachable from what this release can observe:

* `not_requested` — no dossier is selected;
* `completed` — a dossier is selected and carries no warnings;
* `completed_with_warnings` — a dossier is selected and carries warnings.

`queued`, `running`, `failed` and `stale` describe a research *run*. No engine
exists to report one, so claiming them here would be inventing a status nothing
measured. APP-004 owns them.

`last_researched_at` records when research last **completed**, not when it was
last attempted, so it never overstates what is known.

## Conflicts are derived, not queued

`app/services/companies/conflicts.py` computes every disagreement from records
that already exist. There is no conflict table and no second review queue.

* A stored queue needs someone to close its rows. A derived view stops reporting
  a conflict the instant the underlying records agree, which is what an operator
  actually wants from "is this still a problem?".
* The repository already has one review queue, bound to import rows. A second
  architecture beside it would be two places to look and two places to forget.
* A conflict that cannot be re-derived from the data was never really evidence.

Five kinds: `contact_domain_mismatch`, `contact_link_unresolved`,
`linkedin_id_shared`, `snapshot_domain_mismatch`, `no_canonical_domain`.

None of them blocks anything. The company detail page is read-only: domains are
confirmed on the capture resolution screen, so there is exactly one place that
decision gets made.

## Compatibility

* Every schema change is additive. No existing column changed type or nullability.
* `contacts.company_domain` is untouched and still `NOT NULL`.
* No intake contract changed; the extension was not modified.
* Campaign, import, review, contact and verification screens are unchanged.
* APP-003 left `Insight` / `InsightEvidence` unchanged. INS-001 now gives those
  records their narrower intended job: reusable versioned claims and their
  traceable source observations. They do not replace the raw submission or the
  nine-section dossier. A dossier is a versioned reading of one research
  submission; insights are individual claims derived from research or other
  evidence and remain the records cited by `ScoreEvidence`.

## Schema

One migration, `c48b1f70a3d2`, whose parent is the DAT-014 head `a5feeb1bb50a`.

Creates the PostgreSQL `research_state` type — it has existed in Python since
APP-002 but was only ever computed, never stored.

The downgrade reverses cleanly on an empty schema and **refuses** once the
workspace holds contact links, field provenance or dossiers. None of that can be
re-derived, and a rebuilt link is a guess rather than the decision somebody made.

### Sequencing note for DAT-017A (#171)

`feat/dat-017-automatic-domain-resolution` currently carries migration
`7c3a5d81be40` with `down_revision = a5feeb1bb50a` — the same parent this
migration takes. That branch predates the DAT-017A / DAT-017B split and **must be
reconciled against the revised #171 scope and rebased onto APP-003 after APP-003
merges**, regenerating its migration so its `down_revision` points at
`c48b1f70a3d2`. No DAT-017 code was imported into APP-003; the permanent Company
identity and the nullable `contacts.company_id` this release adds are the
foundation DAT-017A builds on.
