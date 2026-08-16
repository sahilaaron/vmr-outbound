# Domain-resolution yield diagnosis

Base: `1260586a35c538b197e48e95df3923d3291981fa`

This note records the Workstream F diagnosis before any production-code change.

## Canonical call path

For a Google Sheets row with a usable `company_name`, no established
`company_domain`, and no `company_id`:

1. `app.api.integrations_sheets` validates the request and calls
   `app.services.integrations.sheets.submit.submit_rows`.
2. `_submit_one` asks `sheet_companies.link_established_company` only for
   already-established, free database evidence. A miss is not a refusal and
   does not call a provider.
3. `_resolve_contact` creates or reuses the permanent Contact. A new unseen
   employer is stored with its submitted name and null domain/company link.
4. `campaign_contacts.enrol_contact` creates the Campaign membership,
   initializes Capture as complete, and queues Identity.
5. The worker completes `IdentityAgentAdapter`; `schedule_next` then queues the
   Company Agent.
6. `CompanyAgentAdapter.execute` sees no linked Company and no Contact domain and
   invokes `_resolve_company_domain`.
7. `_resolve_company_domain` applies the effective automatic-resolution control,
   obtains the shared provider/model access policy, and calls
   `resolution.service.resolve_contact`.
8. `resolve_contact` creates/reuses the Contact-owned candidate record, runs the
   canonical provider/model ladder, evaluates the shared policy, and `_apply`
   writes `CompanyDomainResolution` plus its provenance.

## Pre-ledger exits and skips

Before `resolution.store.record` is reached, resolution can legitimately be
skipped or stopped when:

- the Contact already has a live `company_id` link (resolver is unnecessary);
- the Contact already has a domain (the Company Agent uses exact domain matching);
- the Contact has no usable company name;
- the effective `automatic_company_domain_resolution` control is off;
- no provider is available (intentionally left as no decision so a later retry
  can still decide without `force`);
- an existing current decision is reused (no new row, but the existing ledger
  row remains authoritative);
- `resolve_contact` raises a deterministic `ResolutionError` before applying a
  decision, for example unusable subject state or recalculation over an operator
  correction;
- an unexpected database/provider implementation exception escapes before the
  policy decision can be applied.

None of those paths explains why only resolver failures in the UAT had no
ledger row while successful contacts did.

## Exact failure

An unsuccessful but completed provider/policy evaluation *does* reach `_apply`
and writes an `UNRESOLVED` decision. `CompanyAgentAdapter` then sees that no
Company/domain was selected and raises `AgentBlocked("company_domain_missing")`.
That exception does not set `preserve_outcome`.

`execute_started_job` runs every adapter inside a savepoint and rolls the
savepoint back for an `AgentExecutionError` unless `preserve_outcome` is true.
The rollback therefore deletes the just-written decision, candidates, provider
status, and audit evidence. It cannot undo the external provider call. The job
is then paused with `company_domain_missing`, leaving exactly the observed
combination: no domain and no resolution ledger row.

The six successful Contacts selected a domain, returned normally from the
Company Agent, and committed their savepoints, so their decisions survived. The
four unsuccessful Contacts produced a truthful unresolved policy outcome, then
rolled that outcome back while projecting only the block.

This is primarily a **persistence failure**, with a directly resulting
**observability failure**. It is not an orchestration/eligibility omission and
does not establish that Logo.dev or the model provider malfunctioned.

## Reproduction

The focused regression drives the real Sheets-shaped Contact through enrollment,
Identity, Company, the stubbed provider, policy, and the worker savepoint. A
provider response with no candidate must pause the Company job while retaining
one current `UNRESOLVED` Contact decision. On the accepted base, the assertion
finds no decision because the Company Agent block rolls back the savepoint.

## Repair invariant

Once the canonical resolver has reached and persisted a decision, a downstream
Company-stage block must retain that decision and its provenance. A skipped
lookup (feature off or provider unavailable) must remain distinguishable and
must not fabricate an unresolved decision.
