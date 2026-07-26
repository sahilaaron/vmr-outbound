# The contact CRM (APP-002)

The operator workspace for permanent people. This document describes what is
built, what is deliberately not, and the rules that govern both.

Architecture and reasoning: `docs/decisions/0002-contact-first-architecture.md`.

## The rules this release enforces

* **Contacts are permanent.** A contact may exist without a campaign, an email,
  research, qualification, or outreach readiness.
* **Campaigns are downstream.** No CRM service accepts a campaign identifier, no
  CRM page offers a campaign selector, and a contact with no membership is a
  normal contact rather than a broken one.
* **Captures are immutable observations.** A refresh appends another capture; it
  never rewrites an earlier one.
* **Canonical fields retain provenance.** The winning value, its source, when it
  was observed, and why the policy chose it are all shown.
* **Research and qualification are separate**, from each other and from
  everything else, and neither has an engine yet.
* **Email discovery and verification are downstream** of all of the above.
* **Labels classify contacts.** A label is not a campaign, not an audience, and
  never an eligibility signal.
* **Saved Audiences combine rules later** (APP-006) and reuse the filter
  predicates in `app/services/crm/records.py` rather than duplicating them.
* **Contact intake does not require a campaign.**
* **The backend owns identity resolution.** An exact normalized LinkedIn URL may
  match automatically; a name, title, company, location or headline may not.
* **Suppression remains authoritative.** A suppressed person stays visible and
  clearly marked, and no CRM action can make them outreach-ready.

## Two kinds of record, one list

`/contacts` is a union of:

| Kind | Row | Why it is here |
| --- | --- | --- |
| `contact` | a `contacts` row | the permanent canonical person |
| `pending_capture` | a `linkedin_profile_snapshots` row with outcome `unmatched_staged` or `ambiguous_review` | the operator saved this person; the system has not finished resolving them |

A pending capture is never hidden for not being canonical. It can be labelled,
annotated and inspected while it waits.

The union is performed in SQL. Merging two independently-paginated result sets
would make `LIMIT`/`OFFSET` lie — page 2 would silently skip or repeat people.

`Contact.company_domain` stays `NOT NULL` deliberately. That invariant is *why*
unmatched captures stay pending, and the route to a domain is DAT-010's logo.dev
candidates plus an operator confirmation, never a guess.

## Views

| View | Shows |
| --- | --- |
| All | canonical contacts and pending captures together |
| Awaiting company resolution | captures with no resolved company domain |
| Ambiguous identity | captures matching more than one existing contact |
| Suppressed | contacts blocked by the suppression ledger |

## Four workflow dimensions, never one status

One overloaded field cannot say "captured but unresearched, and suppressed",
which is a real state. Each dimension is **derived from whatever record owns
that truth**, never stored on the contact — so there is no second source to
drift and no backfill.

| Dimension | Authority | Today |
| --- | --- | --- |
| Capture / identity | `linkedin_profile_snapshots.outcome` | live |
| Research | — | always `not_requested` (APP-004) |
| Qualification | — | always `not_assessed` (APP-006) |
| Email | `services/verification/status.py` | live |
| Suppression | the suppression ledger | live, authoritative |

Outreach readiness is deliberately absent: it is a computed decision over all of
these plus policy, and the policy belongs to APP-007.

## Age and freshness

Unmatched captures are kept **indefinitely** and are never auto-deleted or
auto-archived. A person the operator saved deliberately is not thrown away by a
background rule.

What the workspace owes them instead is visibility of how long something has
waited: a `fresh` / `aging` / `stale` band (≤14 days, ≤60 days, beyond) and an
`older_than_days` filter that combines with any view — so "what has been stuck
in Awaiting Company Resolution for over 90 days" is one query.

These are display bands, not policy, and deliberately **not** taken from
`provenance/freshness.py`: that module decides which *field observation* wins for
a contact, which is a different question from how long a *record* has waited. A
real retention policy waits for real usage data.

## Labels and notes

Both work on a contact **or** a pending capture, and both are audited.

* Applying an existing label twice is an idempotent no-op, not an error.
* Names differing only in case, spacing or punctuation resolve to one registry
  entry (`Venture Capital` = `venture-capital`).
* Removing a label is recorded; the registry entry survives for reuse.
* Notes are **append-only**. There is no edit and no delete: a correction is a
  new note, because someone reading the history later needs to see what was
  believed at the time and when it changed.
* A capture's notes stay visible after the person is promoted to a contact.

## Company resolution — an accepted APP-002 limitation

`salesnav_company_enrichments` is keyed `(batch_id, company_key)`, so a capture
that arrived outside an import batch has no enrichment row.

APP-002 therefore **displays** company-resolution state (`not requested`,
`pending`) and does **not** trigger lookups or introduce any second enrichment
mechanism. Making resolution work independently of legacy import batches is
**DAT-014**, which is sequenced immediately after this release and before
APP-003.

## Compatibility

* Existing contacts, campaigns, memberships, snapshots, suppressions and
  provenance are untouched and remain readable.
* No intake contract changed; old payloads keep working.
* Campaign, import, review and verification screens are unchanged.
* `import_batches.campaign_id` stays required until APP-007.
* Nothing under `extensions/` was modified.

## Schema

One additive migration, `4b7d1e92c530`, widening two DAT-013 anchors:

* `contact_label_assignments.contact_id` → nullable, so a pending capture can be
  labelled;
* `contact_capture_notes.capture_id` → nullable, so a spreadsheet-imported
  contact can carry a note.

The anchor check on label assignments is an **inclusive OR, not XOR**:
`capture_id` there is already used as *provenance* alongside `contact_id`, so a
row may legitimately carry both, and an exclusive check would reject rows
DAT-013 deliberately creates. Uniqueness is preserved by two partial unique
indexes, one per anchor space.

Downgrade is a true inverse that **refuses** rather than deleting rows only the
widened schema permits — verified by attempting it with a capture-anchored label
present.

## Not in this release

Full company dossier UI · domain crawler · automated research · final
qualification algorithm · Saved Audiences · campaign reattachment · email copy
generation · outbound scheduling · SalesHandy · autonomous orchestration ·
extension changes.
