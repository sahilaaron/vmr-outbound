# APP-003 acceptance record

Company workspace and dossier-ready data model (#159). Sanitized: no real
company name, domain, contact name or captured value appears here. Every figure
below comes from the automated suite run against a live PostgreSQL 16.

## Acceptance criteria, and what covers each

| # | Criterion (from #159) | Evidence |
| --- | --- | --- |
| 1 | Operator can browse Companies and linked Contacts | `/companies` and `/companies/{id}` render; `tests/test_company_web.py` — 19 route tests including the empty state, a company with people, a missing id, and a hand-edited id |
| 2 | Company detail shows research state and provenance-backed fields | Research card (state, last-researched, warnings) and Canonical fields card (value, source, observed, why it won, policy version); `test_a_reconciled_value_is_shown_with_its_reason` |
| 3 | Schema supports multiple immutable dossier submissions and a current selected interpretation | `company_research_submissions` + `company_dossier_versions`; one-current enforced by a partial unique index, asserted at the database in `test_two_current_versions_are_impossible_at_the_database` |
| 4 | No Campaign is required | `test_the_list_has_no_campaign_anywhere` inspects the filter dataclass; `test_no_page_mentions_a_campaign` inspects both rendered pages |
| 5 | Tests cover domain identity, Contact links, dossier versions, conflicts and stale states | 44 tests in `tests/test_company_workspace.py`, grouped under those headings |

## Product rules, and the test that would catch a regression

| Rule | Test |
| --- | --- |
| Research is evidence, not an unconditional overwrite | `test_recording_an_observation_does_not_change_the_canonical_value` |
| One Company, many Contacts, many dossier versions | `test_many_contacts_share_one_company_workspace`, `test_multiple_versions_coexist_and_only_one_is_current` |
| Unknown stays explicit | `test_an_unaddressed_section_is_unknown_not_empty`, `test_an_observed_empty_value_is_recorded_as_a_real_observation`, `test_an_unknown_field_reads_as_unknown_not_empty` |
| Conflicting fields stay explicit | `test_a_linked_contact_with_another_domain_is_a_visible_conflict`, `test_an_identity_conflict_stays_on_the_page` |
| No crawler logic in the web application | No fetch, no HTTP client and no scheduler in `app/services/companies/`; `submit()` accepts a payload and stores it |
| Older evidence never rewrites a newer value | `test_older_evidence_cannot_overwrite_a_newer_value` |
| An operator outranks every automatic source | `test_a_manual_override_outranks_every_automatic_source` |
| Provider neutrality | `test_dossier_storage_is_provider_neutral` — three different producer/interpreter pairs, no branch on either |
| The section boundary is closed | `test_the_section_boundary_is_closed`, `test_every_declared_section_has_a_column` |
| A dossier belongs to the company it is filed under | `test_a_cross_company_dossier_is_rejected_by_the_database`, `test_a_dossier_cannot_be_moved_to_another_company_by_update` |

## Dossier ownership, enforced by the database

Raised in ChatGPT's review of PR #184 and fixed on the branch: `interpret()`
refused a cross-company submission, but a service check only protects the path
that calls it. A direct write, a data migration, a fixture or a future import can
all reach the table without passing through it, and a dossier attributed to the
wrong organisation is the kind of wrong that reads as fact.

`company_dossier_versions (submission_id, company_id)` is now a composite foreign
key onto `company_research_submissions (id, company_id)`, which required a
`(id, company_id)` unique constraint on the submissions table — redundant against
its primary key, and required, because a composite key must reference a
uniquely-constrained set of columns. It **replaces** the single-column key rather
than supplementing it: referencing `(id, company_id)` already implies everything
the narrower key guaranteed.

`NO ACTION` rather than `RESTRICT`. Both refuse to orphan a version when a
submission is deleted directly. `RESTRICT` is checked immediately; `NO ACTION`
defers to the end of the statement, which is what lets `DELETE FROM companies`
cascade into both tables in one statement without the check firing on a
half-applied intermediate state.

Five regression tests, all bypassing the service and running against live
PostgreSQL:

| Test | Proves |
| --- | --- |
| `test_a_cross_company_dossier_is_rejected_by_the_database` | a hand-built cross-company INSERT raises `IntegrityError` |
| `test_a_dossier_cannot_be_moved_to_another_company_by_update` | the constraint holds on UPDATE, so a correct row cannot be walked across afterwards |
| `test_a_same_company_dossier_is_accepted` | the counterpart that makes the two above meaningful |
| `test_a_dossier_cannot_point_at_a_submission_that_does_not_exist` | the composite key still carries the existence guarantee it replaced |
| `test_deleting_a_company_removes_its_dossiers_without_tripping_the_key` | why `NO ACTION`, not `RESTRICT` |

The two cross-company tests were verified to **fail** against the previous
single-column key and pass against the composite one, so they discriminate rather
than passing for an unrelated reason. The existence and same-company tests pass
under both, which is correct — neither is about ownership.

## Migration backfill

`c48b1f70a3d2`, parent `a5feeb1bb50a`. `contacts.company_id` was backfilled only
where exactly one company carried the contact's domain.

`test_app_003_backfill_links_only_unambiguous_contacts` seeds the pre-migration
world, runs the real migration, and asserts each case:

| Case | Result |
| --- | --- |
| Exactly one company carries the domain | linked |
| No company carries the domain | left NULL |
| Two companies carry the domain | left NULL |
| Domain is blank or whitespace | left NULL |
| A link already exists | preserved on rerun, not recomputed |
| A domain that has since become unambiguous | linked on rerun |

The ambiguous case is exercised against two real rows sharing a domain, created
by dropping the partial unique index first — so the ambiguity is real rather
than hypothetical. The test asserts against the migration's own SQL, imported
from the module, so a change to the shipped statement cannot leave the test
passing against a copy.

`test_app_003_downgrade_refuses_while_the_workspace_holds_data` asserts the
downgrade reverses on an empty schema and refuses once a contact link exists.

## Gates

| Gate | Result |
| --- | --- |
| Backend pytest | 751 passed |
| `ruff check` | passed |
| `ruff format --check` | clean |
| `mypy app` | no issues, 101 source files |
| `alembic heads` | single head, `c48b1f70a3d2` |
| `alembic upgrade head` → `check` → `downgrade base` → `upgrade head` | clean; `check` reports no model drift |
| Extension suite | 236 passed, unchanged — nothing under `extensions/` was touched |

## Deliberate decisions worth reviewing

**`Insight` / `InsightEvidence` were left alone.** ADR 0002 says they "become the
Dossier". They are unreferenced stubs with no versioning, no owner-exclusivity
check, no current-selection and a `freshness_at` unconnected to any policy.
Adapting them would have meant rewriting them into something else while keeping a
name that no longer described them. They stay for the `ScoreEvidence` foreign key.
Retiring them is a separate decision, not one to make silently inside APP-003.

**A parallel provenance table, not a generalized one.** `contact_field_values` is
accepted, tested APP-002 behaviour with a database-enforced partial unique index.
Widening its key to `(entity_type, entity_id, field_name)` would have rewritten a
working subsystem to save a file, and the two ledgers will diverge anyway — a
company field can be claimed by a dossier and a contact field cannot. The
*policy* is shared; only the storage is separate.

**Conflicts are derived, not stored.** No conflict table and no second review
queue. A derived view stops reporting a conflict the moment the records agree,
and the repository already has one queue architecture bound to import rows.

**The company detail page is read-only.** Every write a company might need —
confirming a domain, promoting a capture — already exists on the capture
resolution screens. Two screens offering the same decision is two ways to make it
differently.

## Accepted limitations

* `contacts.company_id` stays nullable. Making it `NOT NULL` is a decision for
  after the backfill has converged in a real database, not one to assert on a
  migration that has only run against test data.
* The `conflicted` list view uses the cheap, indexable half of the conflict
  derivation (no domain, or a linked contact with a different one). The full
  derivation is per company and using it as a SQL filter would mean loading every
  company to paginate. A company whose only conflict is a shared LinkedIn
  identifier is therefore visible on its detail page but not in that view.
* Nothing writes `company_field_values` automatically yet. The ledger, the policy
  and the display are built and tested; wiring capture promotion and the company
  snapshot into it is follow-on work, and pretending otherwise would put an empty
  card on every company page with no explanation.
* Research state cannot report `queued`, `running`, `failed` or `stale`. Those
  describe a run, and no engine exists to report one.

## Sequencing note recorded for DAT-017A (#171)

No DAT-017 code was imported into APP-003, and the old branch was neither merged
nor rebased during this build.

`feat/dat-017-automatic-domain-resolution` carries migration `7c3a5d81be40` with
`down_revision = a5feeb1bb50a` — the same parent APP-003 takes. Two migrations on
one parent become sibling heads. That branch predates the DAT-017A / DAT-017B
split and **must be reconciled against the revised #171 scope and rebased onto
APP-003 after APP-003 merges**, regenerating its migration so its `down_revision`
points at `c48b1f70a3d2`. The repository must end with one Alembic head.

The permanent Company identity (`linkedin_company_url`, `linkedin_company_id`)
and the nullable `contacts.company_id` added here are the foundation DAT-017A
builds on.
