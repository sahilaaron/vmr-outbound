# ADR 0002 — Contact-first domain and workflow model (APP-001)

Status: Accepted (open decisions U1–U3 and D4 confirmed by Sahil, 2026-07-26)
Date: 2026-07-26
Issue: [#157](https://github.com/sahilaaron/vmr-outbound/issues/157)
Baseline: `feat/dat-013-contact-first-capture` @ `2cdf83f`, Alembic head `26f8ab7044f1`
Deciders: Sahil (owner); reconciliation produced by the development agent.

> Phase 2 update (2026-07-29): the ownership decision in this ADR remains in
> force. ADR 0004 supersedes two implementation-era constraints: capture
> contract 2.1 permits optional Campaign filing, and an accepted capture may
> create a permanent Contact with unresolved fields stored as `NULL`.

## Context

The product is moving from a campaign-first architecture to a contact-first one.
The governing principle is **save the person first, decide what to do with them
later**. Contacts and Companies become permanent system records; Campaigns become
downstream execution objects.

The canonical flow is:

```
Contact capture
  → capture validation
  → identity resolution
  → permanent Contact
  → Company association
  → company and contact research
  → qualification
  → email discovery
  → email verification
  → Saved Audience
  → Campaign
  → outreach
```

This ADR is a **repository-grounded reconciliation**, not a greenfield design.
Every claim below was verified against the code at `2cdf83f`. File and line
references are to that commit.

## Baseline evidence

| Gate | Result |
| --- | --- |
| `pytest` | **506 passed** |
| `node --test` (`extensions/salesnav-capture`) | **186 passed, 0 failed** |
| `alembic upgrade head` | clean, head `26f8ab7044f1` |
| `alembic check` | "No new upgrade operations detected." |
| `alembic heads` | single head (no divergence) |
| Migration chain | 15 revisions, strictly linear |

A fresh clone fails 14 of 60 node test files on `Cannot find module 'jsdom'` until
`npm install` is run in `extensions/salesnav-capture`. This is a setup step, not a
regression.

---

## 1. Current-state inventory

### 1.1 Tables by domain role

23 model modules, 34 tables. Grouped by the role they already play:

**Canonical records**

| Table | Model | Note |
| --- | --- | --- |
| `contacts` | `Contact` | **No campaign FK.** Already campaign-independent at table level. |
| `companies` | `Company` | `domain` nullable, partial unique where not null. |

**Capture / immutable observation**

| Table | Model | Note |
| --- | --- | --- |
| `linkedin_profile_snapshots` | `LinkedInProfileSnapshot` | Immutable person capture. `campaign_id` nullable. Carries `outcome`, `matched_contact_id`, `review_candidates`, `refresh_summary`. |
| `linkedin_profile_experience_observations` | `LinkedInProfileExperienceObservation` | Per-role observations, ordered, `is_current` + `dates_reliable`. |
| `linkedin_company_snapshots` | `LinkedInCompanySnapshot` | Immutable firmographic capture. |
| `contact_capture_submissions` | `ContactCaptureSubmission` | DAT-013 submission anchor. |
| `import_batches` / `import_rows` / `import_row_validations` / `import_row_errors` | — | CSV/XLSX/SalesNav intake. **`import_batches.campaign_id` NOT NULL.** |

**Evidence / provenance**

| Table | Model | Note |
| --- | --- | --- |
| `contact_field_values` | `ContactFieldValue` | Append-only field-level provenance with `is_current_winner`, `confidence`, `policy_version`, `decision_reason`. This is the Field Observation concept, already built (DAT-005). |
| `provenance_records` | `ProvenanceRecord` | Per-import observation provenance. |
| `contact_qa_evaluations` | `ContactQAEvaluation` | Append-only versioned employment QA judgments (DAT-012F). |
| `insights` / `insight_evidence` | `Insight`, `InsightEvidence` | Research claims with source URL, retrieval time, confidence, freshness. |

**Classification / operator annotation (DAT-013)**

| Table | Model | Note |
| --- | --- | --- |
| `contact_labels` | `ContactLabel` | Backend-owned reusable label registry. |
| `contact_label_assignments` | `ContactLabelAssignment` | **`contact_id` NOT NULL** — see gap G1. |
| `contact_capture_notes` | `ContactCaptureNote` | Append-only. **`capture_id` NOT NULL** — see gap G2. |

**Authority / policy**

| Table | Model | Note |
| --- | --- | --- |
| `suppressions` / `suppression_events` | `Suppression`, `SuppressionEvent` | Authoritative outreach gate, append-only lifecycle (DAT-006). |
| `identity_resolutions` | `IdentityResolution` | Operator resolution ledger with unique `idempotency_key`. |
| `audit_events` | `AuditEvent` | Global audit trail. |

**Email pipeline**

| Table | Model | Note |
| --- | --- | --- |
| `email_candidates` | `EmailCandidate` | Ranked, one selected per contact. |
| `exact_email_verifications` | `ExactEmailVerification` | Verification evidence. |
| `domain_pattern_observations`, `mail_domain_observations` | — | Domain-level evidence. |
| `verification_jobs` | `VerificationJob` | `campaign_id` nullable. |
| `verification_usage_events`, `usage_ledger_entries` | — | Cost/usage ledger. |

**Company resolution (DAT-010)**

| Table | Model | Note |
| --- | --- | --- |
| `salesnav_company_enrichments` | `SalesNavCompanyEnrichment` | logo.dev candidates, lookup status, operator confirmation. **Currently scoped per `batch_id`** — see decision D4. |

**Campaign execution**

| Table | Model | Note |
| --- | --- | --- |
| `campaigns` | `Campaign` | — |
| `campaign_contacts` | `CampaignContact` | **`campaign_id` NOT NULL.** Owns `state: ContactWorkflowState`. |
| `draft_versions` / `draft_approvals` | — | **`draft_versions.campaign_id` NOT NULL.** |
| `scores` / `score_components` / `score_evidence` | — | `scores.campaign_id` already nullable. |
| `external_events` | `ExternalEvent` | Provider callbacks. |

### 1.2 Surfaces

- **Web** (`app/web/routes.py`, mounted only when `FEATURES__WORKBENCH=true`): 44 routes across overview, campaigns, imports/staging/mapping/enrich/preview, review queue, contacts, verification, local tools, snapshot viewers, and six `unavailable_page` placeholders (`/scoring`, `/research`, `/drafts`, `/sequences`, `/activity`, `/settings`).
- **API** (`app/api/routes.py`): Sales Navigator / LinkedIn profile / LinkedIn company staging, the DAT-013 contact-capture intake, label + contact-lookup companions, a campaign selector, and campaign-scoped CSV import.
- **Templates**: 27 Jinja2 files.
- **Services**: 39 modules under `app/services/`.

---

## 2. Campaign-decoupling audit

`Contact` itself has zero campaign references. All coupling is indirect.

### 2.1 Schema-level

Three tables require a campaign; five already allow none.

| Table | Column | Disposition | Reasoning |
| --- | --- | --- | --- |
| `campaign_contacts` | `campaign_id` NOT NULL | **Retain as-is** | This *is* Campaign Membership. A membership without a campaign is meaningless. Correct by the target model. |
| `import_batches` | `campaign_id` NOT NULL | **Defer to APP-007** | The legacy CSV/XLSX importer is the only writer. Contact-first capture (`app/services/captures/intake.py`) never touches it. Making it nullable now would change the legacy import path for no APP-002 benefit. |
| `draft_versions` | `campaign_id` NOT NULL | **Retain as-is** | A draft is campaign-specific execution copy by definition. Correct by the target model. |
| `scores` | nullable | **Keep** | Already correct — a contact-level Initial Fit Score needs no campaign. |
| `usage_ledger_entries`, `verification_jobs`, `linkedin_profile_snapshots`, `linkedin_company_snapshots` | nullable | **Keep** | Already correct. |

**No destructive schema change is required for APP-002.** This is the single most
important finding of the audit.

### 2.2 The real blocker — workflow state has no contact-level home

`ContactWorkflowState` (`imported`, `awaiting_verification`, `suppressed`,
`excluded`) is stored on `CampaignContact.state`, and
`app/services/contact_state.py:35` takes a `CampaignContact` as a required
positional argument:

```python
def transition_contact_state(session, membership: CampaignContact, *, target, actor="system", reason=None) -> CampaignContact
```

Consequences:

1. A Contact with no campaign has **no workflow state at all**.
2. The one enum conflates three unrelated dimensions: intake (`imported`), email (`awaiting_verification`), and policy (`suppressed`, `excluded`).
3. `workbench.list_contacts` (`app/services/workbench.py:269-271`) joins `CampaignContact` whenever `state` is filtered — **so filtering by state silently drops every campaign-less contact.** This is a live defect, not a theoretical one.

This is exactly the "one overloaded Contact status field" that #157 says must not
be preserved.

### 2.3 Silent-drop INNER JOINs

Six queries inner-join `Campaign`, so a row without one disappears rather than
erroring:

| File:line | Query |
| --- | --- |
| `app/services/identity.py:108` | unresolved-ambiguity queue |
| `app/services/identity.py:280` | `get_row_review()` |
| `app/services/identity.py:200` | `_memberships_of()` |
| `app/services/workbench.py:115` | `list_batches()` |
| `app/services/workbench.py:126` | `get_batch()` |
| `app/services/workbench.py:322` | `get_contact_detail()` memberships block |

`workbench.py:322` is in APP-002's path and is **made an outer join** by APP-002.
The `identity.py` and batch queries stay as-is: they operate on import batches,
which legitimately always have a campaign until APP-007.

### 2.4 Service signatures requiring a campaign

Classified per #157's vocabulary:

| Service | Disposition |
| --- | --- |
| `imports/importer.py:549` `run_import(*, campaign_id, ...)` | **Retain as compatibility behaviour** — legacy CSV/XLSX path. |
| `imports/salesnav_intake.py:304` `_resolve_campaign` (raises `CampaignInvalidError`) | **Defer to APP-007** — superseded in practice by contact-first capture. |
| `contact_state.py:35` `transition_contact_state(membership, ...)` | **Move to Campaign Membership** — it is already correctly a membership-scoped operation. APP-002 adds *contact-level* dimensions alongside it rather than changing it. |
| `identity.py` `QueueItem.campaign`, `RowReview.campaign` (non-optional) | **Make optional** — deferred to APP-007; not on APP-002's path. |
| `imports/staging.py:80` `StagedUpload.campaign_id: str` (no default) | **Retain as compatibility behaviour.** |
| `campaigns.py`, `devtools.py` campaign helpers | **Retain** — legitimately campaign-scoped. |
| `workbench.py:243` `list_contacts(campaign_id=None, ...)` | **Adapt** — already optional, but the `state` filter forces the join (§2.2). |
| `verification/*`, `usage_ledger.py` | **Keep** — already optional. |
| `captures/intake.py` | **Keep** — already fully decoupled; the precedent to follow. |

### 2.5 The existing decoupled precedent

`app/services/captures/intake.py` already does what the whole application is
moving toward. Its docstring states there is no campaign anywhere in the path; it
sets `campaign_id=None` on the snapshot (`:568`), never creates a membership, and
`contact-capture.schema.json` sets `"additionalProperties": false` so a campaign
field is actively *rejected*. `tests/test_contact_capture_intake.py:207` asserts
this.

**APP-002 extends this precedent rather than inventing a new pattern.**

---

## 3. Target entity model

```
                 ┌──────────────────────┐
                 │      Company         │  permanent
                 └──────────┬───────────┘
                            │ 0..1
                            │
   Capture ──validation──> Identity ──> ┌──────────────────────┐
 (immutable)   resolution   resolution  │      Contact         │  permanent
      │                                 └──────────┬───────────┘
      │ evidence                                   │
      ▼                                            ├── Label (n:m)
 ┌──────────────────┐                              ├── Note (append-only)
 │ Field Observation│──supports canonical field────┤
 └──────────────────┘                              ├── Research Job → Dossier
                                                   ├── Qualification Assessment
                                                   ├── Email Candidate → Verification
                                                   │
                                                   ▼
                                          Saved Audience (APP-005)
                                                   │
                                                   ▼
                                              Campaign
                                                   │
                                                   ▼
                                        Campaign Membership
                                     (status, copy, sending, outcomes)
```

Mapping of target concepts to what already exists:

| Target concept | Existing implementation | Action |
| --- | --- | --- |
| Contact | `contacts` | **Keep** |
| Company | `companies` | **Keep** |
| Capture | `linkedin_profile_snapshots` + `contact_capture_submissions` + `import_rows` | **Keep** |
| Evidence / Field Observation | `contact_field_values` (DAT-005) | **Keep** — already exactly this |
| Research Job | — | **Defer to APP-004** |
| Dossier | `company_research_submissions` + `company_dossier_versions` | **Keep** — APP-003 owns raw submissions and immutable dossier readings; INS-001 keeps `insights` as the individual claims derived from evidence. |
| Qualification Assessment | `contact_qa_evaluations` (employment QA only) | **Adapt in APP-006** |
| Label | `contact_labels` + `contact_label_assignments` | **Adapt** (gap G1) |
| Saved Audience | — | **Defer to APP-005** |
| Campaign | `campaigns` | **Keep** |
| Campaign Membership | `campaign_contacts` | **Keep** |

---

## 4. Separate workflow state dimensions

#157 requires distinct dimensions rather than one status. The repository already
contains most of this vocabulary; APP-001 **reuses it and does not introduce
parallel enums**.

### 4.1 Capture / identity state — reuse `LinkedInSnapshotOutcome`

Already implemented, 7 values, written by `captures/intake.py`:

```
stored → exact_match_refreshed | exact_match_unchanged
       → unmatched_staged        (pending — awaiting company resolution)
       → ambiguous_review        (pending — awaiting identity decision)
       → suppressed
       → duplicate_in_submission
```

This maps onto #157's suggested `received / validated / identity_pending /
resolved / rejected` without needing a second enum:

| #157 vocabulary | Existing value |
| --- | --- |
| received | `stored` |
| validated / resolved | `exact_match_refreshed`, `exact_match_unchanged` |
| identity_pending | `ambiguous_review`, `unmatched_staged` |
| rejected | `suppressed`, `duplicate_in_submission` |

**Decision:** keep `LinkedInSnapshotOutcome`. Adding a second capture enum would
create the duplicate state system #157 forbids.

### 4.2 Email state — reuse `EmailPreciseStatus`

Already implemented (15 values) and already **computed, not stored**, by
`app/services/verification/status.py`, with a `PRECISE_TO_VISUAL` projection to a
4-value display status. This already satisfies #157's email dimension and its
"computed rather than assigned" principle.

**Decision:** keep. No new email enum.

### 4.3 Company resolution state — reuse DAT-010 enums

`EnrichmentLookupStatus` (`not_started`, `ok`, `no_match`, `api_unavailable`,
`rate_limited`, `malformed`, `error`) and `EnrichmentConfirmationStatus`
(`unconfirmed`, `confirmed`, `unresolved`) already model the logo.dev path.

**Decision:** keep the enums. See D4 for the scoping change they need.

### 4.4 Research state — **new, deferred to APP-004**

No table exists. APP-001 specifies the vocabulary; APP-002 displays
`not_requested` for every Contact because that is the truthful current value —
it does not fabricate data.

```
not_requested → queued → running → completed | completed_with_warnings | failed
                                             → stale
```

### 4.5 Qualification state — **new, deferred to APP-006**

`contact_qa_evaluations` holds employment QA outcomes but is not a general
qualification judgment. APP-002 displays `not_assessed`.

```
not_assessed → pending → qualified | borderline | disqualified | needs_review
```

### 4.6 Suppression — unchanged, authoritative

`suppressions` + `suppression_events` remain the single authority. APP-002 reads
this and never writes it.

### 4.7 Outreach readiness — computed, never stored

No column. Derived at the point of use from: qualification AND usable email AND
acceptable verification AND not suppressed AND research satisfied AND no company
saturation AND approval. APP-001 defines the boundary only; the policy itself
belongs to APP-007.

---

## 5. Gaps found in the DAT-013 tables

Two constraints block #158's acceptance criteria. Both were verified in
`app/models/contact_capture.py`.

### G1 — a pending capture cannot be labelled

```python
class ContactLabelAssignment(Base):
    __table_args__ = (UniqueConstraint("contact_id", "label_id", ...), ...)
    contact_id: Mapped[uuid.UUID] = mapped_column(..., nullable=False)  # ← blocks
    capture_id: Mapped[uuid.UUID | None] = mapped_column(..., nullable=True)  # provenance only
```

`capture_id` records *which capture caused* the label; it is not an alternative
anchor. Because an unmatched capture has no `Contact` row, it cannot carry a
label — but #158 requires pending captures to remain "visible **and actionable**".

### G2 — a CSV-imported Contact cannot carry a note

```python
class ContactCaptureNote(Base):
    capture_id: Mapped[uuid.UUID] = mapped_column(..., nullable=False)  # ← blocks
    contact_id: Mapped[uuid.UUID | None] = mapped_column(..., nullable=True)
```

Notes anchor to a capture, so a Contact created by CSV import has nowhere to put
one. #158 requires append-only operator notes on Contacts.

### Resolution (APP-002, additive and reversible)

For both tables: make the over-strict anchor nullable, add a CHECK that **exactly
one** anchor is set, and replace the unique constraint with two partial unique
indexes.

**Important subtlety.** On `contact_label_assignments`, `capture_id` is already
in use as *provenance* — a row may legitimately have **both** `contact_id` and
`capture_id` set. The anchor constraint must therefore be an inclusive OR, not an
exclusive one. An XOR check would reject existing valid rows. The anchor is read
as "`contact_id` if present, otherwise `capture_id`".

```sql
-- labels
ALTER TABLE contact_label_assignments ALTER COLUMN contact_id DROP NOT NULL;
ALTER TABLE contact_label_assignments ADD CONSTRAINT ck_contact_label_assignments_anchor
  CHECK (contact_id IS NOT NULL OR capture_id IS NOT NULL);   -- inclusive OR, not XOR
ALTER TABLE contact_label_assignments DROP CONSTRAINT uq_contact_label_assignments_contact_id;
CREATE UNIQUE INDEX uq_cla_contact ON contact_label_assignments (contact_id, label_id)
  WHERE contact_id IS NOT NULL;
CREATE UNIQUE INDEX uq_cla_capture ON contact_label_assignments (capture_id, label_id)
  WHERE contact_id IS NULL;

-- notes
ALTER TABLE contact_capture_notes ALTER COLUMN capture_id DROP NOT NULL;
ALTER TABLE contact_capture_notes ADD CONSTRAINT ck_contact_capture_notes_anchor
  CHECK (capture_id IS NOT NULL OR contact_id IS NOT NULL);
```

Dropping `NOT NULL` is **widening** — every existing row stays valid. Both CHECKs
are satisfied by all current data by construction: every existing label row has
`contact_id` set, and every existing note row has `capture_id` set. The two
partial unique indexes together preserve the old guarantee (one label per
contact) and extend it to pending captures without the two anchor spaces
colliding.

Downgrade is a true inverse provided no row uses the new freedom; it therefore
refuses to run if capture-anchored assignments or capture-less notes exist,
rather than deleting them.

---

## 6. The central design decision — one list, two record kinds

**D1. `/contacts` presents canonical Contacts and pending captures through a
service-layer read model, not through new rows or a new table.**

#158 requires that "pending DAT-013 captures must not disappear merely because
they are not yet canonical Contact rows", while #157 forbids casually making
`Contact.company_domain` nullable or bypassing DAT-010.

Options considered:

| Option | Verdict |
| --- | --- |
| Make `Contact.company_domain` nullable and create provisional Contacts | **Rejected** — explicitly forbidden by #157; destroys the invariant that a canonical Contact has a resolved company. |
| Materialise a `crm_records` table | **Rejected** — duplicates truth, needs sync, violates "prefer a temporary manual step over premature automation". |
| **Service-layer read model projecting both sources into one row shape** | **Accepted** — no schema change to canonical truth, fully reversible, and the query logic is reusable by Saved Audiences (APP-005). |

The read model is a frozen dataclass `CrmRow` with a `kind` discriminator
(`contact` | `pending_capture`), produced by a new `app/services/crm/` package.
Both sources project into it; the template renders one table.

**D2. Contact-level workflow state is computed, not stored.** Rather than adding
status columns to `contacts` (which would create the second overloaded status
field #157 warns against), each dimension is derived at read time from the
authoritative source: capture state from the latest snapshot's `outcome`, email
state from `verification/status.py`, suppression from the suppression ledger,
research and qualification as literal constants until APP-004/006 build them.
This keeps a single source of truth per dimension and needs no backfill.

**D3. `CampaignContact.state` is left untouched.** It stays as per-campaign
execution state, which is what the target model says Campaign Membership owns.

**D4. Company resolution stays batch-scoped for APP-002.**
`salesnav_company_enrichments` is keyed `(batch_id, company_key)`, so a capture
arriving outside an import batch has no enrichment row. Re-keying it to be
batch-independent is a real change with migration cost and belongs with the
promotion work in **DAT-014**. APP-002 therefore *displays* company-resolution
state for pending captures — captured company name, LinkedIn company hint, and
any existing DAT-010 candidate rows — and shows an explicit "not yet requested"
state where none exists. It does not trigger lookups. This is recorded as an
accepted limitation, not an omission.

---

## 7. Keep / adapt / replace / retire

### Models

| Model | Decision | Reasoning |
| --- | --- | --- |
| `Contact` | **Keep** | Already campaign-free. `company_domain` NOT NULL is retained deliberately (#157). |
| `Company` | **Keep** | — |
| `LinkedInProfileSnapshot` + experiences | **Keep** | The Capture concept, already immutable. |
| `LinkedInCompanySnapshot` | **Keep** | — |
| `ContactCaptureSubmission` | **Keep** | — |
| `ContactLabel` | **Keep** | — |
| `ContactLabelAssignment` | **Adapt** | Gap G1 — nullable `contact_id` + CHECK. |
| `ContactCaptureNote` | **Adapt** | Gap G2 — nullable `capture_id` + CHECK. |
| `ContactFieldValue` | **Keep** | Already the Field Observation model. |
| `ProvenanceRecord` | **Keep** | — |
| `ContactQAEvaluation` | **Keep** (adapt in APP-006) | — |
| `Suppression`, `SuppressionEvent` | **Keep** | Authoritative. |
| `IdentityResolution` | **Keep** | — |
| `AuditEvent` | **Keep** | — |
| `EmailCandidate`, `ExactEmailVerification`, `DomainPatternObservation`, `MailDomainObservation`, `VerificationJob` | **Keep** | — |
| `SalesNavCompanyEnrichment` | **Adapt in DAT-014** | Batch-scoped (D4). |
| `Insight`, `InsightEvidence` | **Adapt in INS-001** | Reusable Company/Contact claims and traceable observations; dossiers remain the APP-003 submission/version boundary. |
| `Score`, `ScoreComponent`, `ScoreEvidence` | **Keep** | `campaign_id` already nullable. |
| `Campaign`, `CampaignContact` | **Keep** | Correct as execution objects. |
| `DraftVersion`, `DraftApproval` | **Keep** | Campaign-scoped by definition. |
| `ImportBatch` + row models | **Retain as compatibility** | Legacy path; revisit APP-007. |
| `UsageLedgerEntry`, `VerificationUsageEvent`, `ExternalEvent` | **Keep** | — |

### Routes

| Route | Decision |
| --- | --- |
| `GET /contacts` | **Adapt** — becomes the CRM list with the four views. |
| `GET /contacts/{id}` | **Adapt** — becomes sectioned Contact detail. |
| `GET /captures/{id}` (new) | **Add** — pending-capture detail. |
| `POST /contacts/{id}/labels`, `DELETE .../labels/{slug}` (new) | **Add** |
| `POST /contacts/{id}/notes` (new) | **Add** |
| `GET /` overview | **Adapt** — contact-first stat tiles. |
| `GET /campaigns*` | **Keep** — unchanged, must still load. |
| `GET /imports*`, `/review*` | **Keep** — unchanged compatibility paths. |
| `GET /contact-captures/*` | **Keep** — existing read-only viewers. |
| `/scoring`, `/research`, `/drafts`, `/sequences`, `/activity`, `/settings` | **Keep** — existing `unavailable` placeholders; no new decorative sections. |
| `POST /api/intake/contact-captures` | **Keep** — the decoupled precedent. |
| `GET /api/campaigns` | **Retain as compatibility** — extension selector; retire in APP-007. |
| `POST /campaigns/{id}/imports` | **Retain as compatibility** |

---

## 8. Migration and compatibility plan

**Single additive migration** on top of `26f8ab7044f1`.

| Change | Type | Data impact |
| --- | --- | --- |
| `contact_label_assignments.contact_id` → nullable | Widening | None — all rows keep their value |
| CHECK `ck_contact_label_assignments_one_anchor` | Additive | Holds for existing rows (see R2) |
| Replace unique with two partial unique indexes | Equivalent | Same guarantee, extended |
| `contact_capture_notes.capture_id` → nullable | Widening | None |
| CHECK `ck_contact_capture_notes_one_anchor` | Additive | Holds — every existing row has `capture_id` |

No column is dropped or renamed. No table is renamed. No enum is rebuilt. No
backfill is required, because D2 computes state rather than storing it.

**Rollback.** `downgrade()` is a true inverse: drop the CHECKs, restore the
single unique constraint, restore `NOT NULL`. It guards first — if any
capture-anchored label assignment or contact-only note exists, it raises with a
clear message instead of destroying rows. The operator resolves those records
first. This is documented rather than silently lossy.

**Compatibility.** Existing Contacts, Campaigns, memberships, snapshots,
suppressions and provenance are untouched and remain readable. Old intake
payloads keep working — no intake contract changes in APP-002.

---

## 9. Navigation and screen map

APP-002 introduces the smallest coherent navigation. Existing sections stay.

```
Home        /                  adapted    contact-first stat tiles
Contacts    /contacts          adapted    ← APP-002 primary surface
              ?view=all | awaiting_company | ambiguous | suppressed
            /contacts/{id}     adapted    sectioned detail
            /captures/{id}     new        pending-capture detail
Companies   /companies         deferred   APP-003
Campaigns   /campaigns         unchanged  must still load
Imports     /imports           unchanged  compatibility
Review      /review            unchanged  compatibility
Verification/verification      unchanged
(later)     /research /scoring /drafts /sequences /activity /settings — existing placeholders
```

Contact detail sections: Overview · Employment · Company · Captures & Evidence ·
Research · Qualification · Email · Labels · Notes · Activity & History.
Research and Qualification render a truthful "not yet assessed" empty state.

---

## 10. API and contract boundaries

```
Chrome extension ──▶ linkedin-contact-capture/2.0.0 ──▶ POST /api/intake/contact-captures ──▶ Contact CRM
Company Domain Insights Engine ──▶ (Company Dossier contract, APP-003) ──▶ Company workspace
```

- The web application **never** depends on extension DOM details; it reads only persisted records.
- The backend owns identity resolution. Exact normalized LinkedIn URL may match automatically; name, title, company, location or headline alone must not.
- APP-002 changes **no** intake contract and touches **no** file under `extensions/`, which is owned by the parallel session.

---

## 11. Implementation sequencing

| Issue | Scope | Depends on |
| --- | --- | --- |
| **APP-001** | This ADR | — |
| **APP-002** | Contact CRM foundation | APP-001, DAT-013 |
| **DAT-014** | Capture → Contact promotion; batch-independent company resolution | APP-002, DAT-010 |
| APP-003 | Company workspace + Dossier contract | **DAT-014**, company engine |
| APP-004 | Research jobs + dossiers | APP-003 |
| APP-005 | Qualification assessments | APP-004 |
| APP-006 | Saved Audiences (reuses APP-002 filter predicates) | APP-005 |
| APP-007 | Campaign reattachment + full decoupling | APP-006 |

---

## 12. Risks and unresolved decisions

| # | Risk | Mitigation |
| --- | --- | --- |
| R1 | The union read model could diverge from a future Saved Audience query | Filter predicates live in one module and are reused by APP-006 rather than duplicated. |
| R2 | `capture_id` on `contact_label_assignments` is already used as *provenance*, so rows may have both anchors set | The anchor CHECK is an inclusive OR, not XOR (§5). An XOR constraint would reject valid existing rows — this was caught and corrected during APP-001. |
| R3 | Pending captures cannot be labelled until the migration lands | Sequenced first in APP-002. |
| R4 | Company resolution for non-batch captures is display-only (D4) | Recorded as an accepted APP-002 limitation; DAT-014 owns it. |
| R5 | Two unpushed branches (`cmp-001-003`, `fnd-009`) may conflict later | Neither touches contact CRM surfaces; flagged for Sahil. |

**Resolved by Sahil, 2026-07-26:**

1. **U1 — pending-capture retention: keep indefinitely.** Unmatched captures are
   never auto-deleted or auto-archived. Instead APP-002 surfaces age and
   freshness so an ageing queue is visible rather than silently accumulating: a
   `fresh` / `aging` / `stale` band (≤14 days, ≤60 days, beyond) and an
   `older_than_days` filter that combines with any view. A real retention policy
   may only be introduced once real usage data exists to base one on.
   These thresholds are display bands, not policy, and are deliberately *not*
   taken from `provenance/freshness.py` — that module decides which field
   observation wins for a contact, which is a different question from how long a
   record has been waiting in a queue.
2. **U2 — `import_batches.campaign_id` stays required until APP-007.** It belongs
   to the legacy campaign-scoped import path and does not block DAT-013 intake.
3. **U3 — sequencing is APP-002 → DAT-014 → APP-003.** APP-002 establishes the
   operator workspace; DAT-014 then makes the "Awaiting Company Resolution" queue
   actionable; APP-003 builds the Company workspace over resolved Companies.
4. **D4 — the batch-scoped logo.dev limitation is accepted for APP-002 only.**
   APP-002 displays truthful states (`not_requested`, `pending`, unavailable) and
   **must not introduce a second enrichment mechanism**. DAT-014 must make
   company resolution work independently of legacy import batches.

---

## 13. Test strategy

- **Preserve**: all 506 pytest and 186 node tests must still pass unchanged.
- **Add** (APP-002): contact exists without campaign; contact-first intake without campaign ID; exact normalized LinkedIn URL match; ambiguous match stays unresolved; duplicate intake idempotency; list pagination/search/filter; each of the four views; label add/remove/create + duplicate prevention; label on a pending capture; append-only notes on both anchors; note immutability; evidence and capture history; contact↔company link; unresolved company hint; separate state display; suppression visibility; campaign independence of every new service; migration upgrade **and** downgrade; downgrade guard; malformed intake payload; HTML route smoke tests for every new and adapted route.
- **Rollback test**: upgrade → seed → downgrade → assert no data loss, on a populated database.
- No live LinkedIn access is used for acceptance.

---

## 14. Consequences

**Positive.** No destructive migration. Contacts are already campaign-free at the
schema level, so APP-002 is mostly additive read-model and UI work. Existing
provenance, suppression, verification, identity resolution and audit foundations
are preserved untouched. State vocabulary is reused rather than duplicated.

**Negative.** The union read model is more complex than a single-table query, and
pays a cost until DAT-014 promotes captures into Contacts. Company resolution for
non-batch captures is display-only in APP-002 (D4). Research and qualification
render as constants until APP-004/006.

**Deliberately deferred.** Full Company dossier UI, domain crawler, automated
research, final qualification algorithm, Saved Audience builder, campaign
reattachment, email copy generation, outbound scheduling, SalesHandy, autonomous
orchestration, extension DOM changes.
