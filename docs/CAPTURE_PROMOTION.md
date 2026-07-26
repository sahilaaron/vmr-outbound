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

No campaign or campaign membership · no email candidate or verification · no
score or qualification · no draft or approval · no outreach readiness · no
change to the captured payload, profile fields, content hash, or experience
observations · no suppression weakened · no automatic merge.

## Idempotency and failure

`contact_capture_promotions.capture_id` is unique, so a second promotion is
impossible at the database level, not merely discouraged in code. A retry
returns `already_promoted` with the original contact. A provider failure is
retryable and records the attempt count. A mid-promotion failure rolls back to
nothing — proven by a test that injects one.

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

## Boundaries

**The extension is not involved.** Its responsibility ends when DAT-013 accepts
the submission. It never calls logo.dev, never holds a provider key, and never
resolves a domain — the backend and the workbench own the provider lookup,
candidate storage, operator confirmation, company resolution, and promotion.

**Feature switches.** `FEATURES__CONTACT_CAPTURE_PROMOTION` (default off) gates
the whole flow; `FEATURES__WORKBENCH` gates the operator pages, which the app
factory refuses to mount outside `APP_ENV=local`; a provider lookup additionally
needs `FEATURES__SALESNAV_DOMAIN_ENRICHMENT` and `LOGO_DEV_API_KEY`. Without a
key the flow still works — the operator can enter a domain by hand or leave the
capture pending.

## APP-001 dependency (unresolved)

`Contact` has no foreign key to `Company`; it carries `company_name` and
`company_domain` strings, and `companies.domain` is uniquely indexed. DAT-014
therefore retains the resolved company relationship on the **promotion record**
(`resolved_company_id`, `resolved_domain`) and does not add a
`Contact.company_id`.

That is a narrow compatibility seam, not a recommendation. Whether a contact
should reference a company directly is an application-domain decision belonging
to APP-001 (#157).

**The issue body itself was not readable from the session that built this** —
the GitHub API is not available to it — but the APP-001/APP-002 working notes
carried over from the session doing that work record its substance, and they
corroborate this seam rather than contradict it:

* #157 forbids casually making `Contact.company_domain` nullable or bypassing
  DAT-010. The sanctioned domain path is exactly the one implemented here:
  captured company name → DAT-010 logo.dev candidates → operator confirmation →
  canonical Company → promotion (DAT-014).
* Pending captures must remain `LinkedInProfileSnapshot` rows and must **not**
  become provisional `Contact` rows. DAT-014 creates a contact only on an
  explicit operator promotion with a confirmed domain, so it holds that line.
* `ContactWorkflowState` lives on `CampaignContact`, so a campaign-less contact
  has no workflow state. That is APP-001's problem to solve; DAT-014 does not
  invent a parallel one.

DAT-014 consequently avoids every application-level choice it could:

* no `Contact.company_id` column or relationship;
* no changes to application navigation;
* no contact-list or contact-detail redesign;
* no new workflow states or transitions;
* no changes to `ContactWorkflowState` or campaign membership.

If APP-001 introduces a contact→company relationship, `resolved_company_id` is
the migration source: it already records, per promoted contact, which company it
resolved to and how.

### Two DAT-013 anchor gaps that belong to APP-002 (#158), not here

* `ContactLabelAssignment.contact_id` is `NOT NULL`, so a capture cannot be
  labelled before it is promoted. DAT-014 works within that: requested labels
  stay on the capture as evidence and are assigned at promotion.
* `ContactCaptureNote.capture_id` is `NOT NULL`, so a contact with no capture
  (a CSV import) cannot carry one of these notes.

Both need an additive migration making each anchor nullable with a check that
exactly one is set. That is APP-002 scope. DAT-014 deliberately does not change
those constraints, because doing so from this branch would collide with the
APP-002 migration.

## Known limitations

* A live logo.dev call requires `LOGO_DEV_API_KEY`. Automated tests always stub
  the provider, and the sanitized acceptance run stubs it at the HTTP boundary.
* A Sales Navigator results-row capture has no experience history, so its title
  and company come from the visible employment hint.
* Promotion resolves one company per capture — the current employer the page
  showed. Historical employers stay snapshot evidence.
* Country and industry are not inferred from a captured location or headline;
  they stay null until a source that actually states them provides them.
