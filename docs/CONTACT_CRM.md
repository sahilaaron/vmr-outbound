# The contact CRM (APP-002)

The operator workspace for permanent people. This document describes what is
built, what is deliberately not, and the rules that govern both.

Architecture and reasoning: `docs/decisions/0002-contact-first-architecture.md`.
The Phase 2 Campaign and execution model is documented separately in
`docs/PHASE_2_EXECUTION_MODEL.md`.

## The rules this release enforces

* **Contacts are permanent.** A contact may exist without a campaign, an email,
  research, qualification, or outreach readiness.
* **Campaigns are downstream.** No CRM service accepts a campaign identifier, no
  CRM page offers a campaign selector, and a contact with no membership is a
  normal contact rather than a broken one. The capture intake may optionally
  invoke the separate Campaign Contact filing service after permanent storage.
* **Captures are immutable observations.** A refresh appends another capture; it
  never rewrites an earlier one.
* **Canonical fields retain provenance.** The winning value, its source, when it
  was observed, and why the policy chose it are all shown.
* **Research and qualification are separate**, from each other and from
  everything else, and neither has an engine yet.
* **Email discovery and verification are downstream** of all of the above.
* **Collections classify contacts.** The extension and legacy CRM call them
  Labels. Collection membership is not Campaign membership or an eligibility
  signal.
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
| `pending_capture` | a legacy unmatched capture or an unresolved exact-identity conflict | capture evidence exists but cannot yet be assigned safely |

A pending capture is never hidden for not being canonical. It can be labelled,
annotated and inspected while it waits.

The union is performed in SQL. Merging two independently-paginated result sets
would make `LIMIT`/`OFFSET` lie — page 2 would silently skip or repeat people.

Phase 2 permits `Contact.company_domain` and other unobserved identity fields to
remain `NULL`. A new accepted capture can therefore persist the permanent person
immediately without a fabricated domain. Company-dependent Agents remain
blocked until evidence resolves the missing value.

## Views

| View | Shows |
| --- | --- |
| All | canonical contacts and pending captures together |
| Awaiting company resolution | Contacts or legacy captures with no resolved company domain |
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

Unresolved Contacts and legacy pending captures are kept **indefinitely** and
are never auto-deleted or auto-archived. A person the operator saved deliberately
is not thrown away by a background rule.

What the workspace owes them instead is visibility of how long something has
waited: a `fresh` / `aging` / `stale` band (≤14 days, ≤60 days, beyond) and an
`older_than_days` filter that combines with any view — so "what has been stuck
in Awaiting Company Resolution for over 90 days" is one query.

These are display bands, not policy, and deliberately **not** taken from
`provenance/freshness.py`: that module decides which *field observation* wins for
a contact, which is a different question from how long a *record* has waited. A
real retention policy waits for real usage data.

## Collections (Labels) and notes

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
  provenance remain readable.
* Contact capture 2.1 adds optional Campaign filing; 2.0 payloads remain
  accepted and identical retries replay their stored response.
* Campaign, import, review and verification screens are unchanged.
* `import_batches.campaign_id` stays required until APP-007.
* The Phase 2 extension adds an optional Campaign selector and persists that
  filing preference separately; selecting no Campaign preserves the original
  contact-only behavior.

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

## Historical APP-002 exclusions

Full company dossier UI · domain crawler · automated research · final
qualification algorithm · Saved Audiences · campaign reattachment · email copy
generation · outbound scheduling · SalesHandy · autonomous orchestration ·
extension changes.

Several of those historical exclusions now have later implementations. See the
Phase 2 execution document and current README for the active boundary.
