# Capture promotion — current Hosted Beta contract

**Status date:** 14 August 2026

This document describes how an immutable LinkedIn/Sales Navigator capture becomes a permanent Contact and, when explicitly requested by the capture, a Campaign membership.

For the current live UAT result, see [`CURRENT_PRODUCT_STATE.md`](CURRENT_PRODUCT_STATE.md).

## 1. Contact-first rule

> Save the person first. Decide what to do with them later.

VM Prospector captures immutable evidence. It does not create canonical Contacts directly, resolve domains in the browser, hold provider keys, verify email, research companies or send outreach.

The backend owns resolution, promotion and filing.

## 2. Current flow

```text
VM Prospector capture
→ immutable LinkedIn snapshot / capture evidence
→ optional Campaign filing intent
→ company/domain resolution
→ permanent Company
→ permanent Contact
→ optional CampaignContact filing
→ Identity / Company / downstream Agent pipeline
```

A Campaign filing intent is optional. The Contact remains a permanent Campaign-independent object.

## 3. Hosted Beta feature group

Hosted automatic promotion currently requires the intended staging group:

```text
FEATURES__CONTACT_CAPTURE_PROMOTION=true
FEATURES__AUTOMATIC_COMPANY_DOMAIN_RESOLUTION=true
FEATURES__SALESNAV_DOMAIN_ENRICHMENT=true
LOGO_DEV_API_KEY=<configured secret>
```

Model company-domain fallback additionally uses:

```text
FEATURES__MODEL_COMPANY_DOMAIN_LOOKUP=true
```

The Logo.dev key is a deployment secret and must never be rendered/logged.

The staging runtime validator refuses a partially enabled hosted promotion configuration instead of accepting captures into a box that cannot resolve/promote them.

## 4. Provider/domain outcomes

The current automatic policy distinguishes confirmed, provisional and unresolved identity.

A provider result is not silently promoted as operator-confirmed truth.

An aligned provider-only domain may be retained as **provisional** under the current policy. `DOMAIN_PROVISIONAL` is a resolved-enough outcome for Contact promotion, but it remains distinct from a human-confirmed domain.

Ambiguous or non-aligned results remain unresolved rather than being guessed.

Real Hosted Beta recovery demonstrated the intended behavior:

- Logo.dev returned candidates successfully;
- some captures resolved provisionally and promoted;
- ambiguous/no-aligned cases remained `UNRESOLVED`;
- no domain was forced merely to make the Campaign count increase.

## 5. Provisional domains and Campaign membership

A provisional company/domain does **not** prevent Campaign membership creation.

If a capture is otherwise promotable and carries an explicit Campaign filing request, the membership row may be created with truthful eligibility state.

Do not confuse:

- Campaign membership creation;
- downstream stage eligibility;
- the Campaign setting that may allow provisional domains farther downstream.

The provisional-domain gate is not an explanation for a Campaign showing zero members when no `campaign_contacts` rows exist.

## 6. Contact promotion

Promotion remains deterministic and conservative.

It requires enough person/company identity to create or link a permanent Contact without fabricating data.

Strong existing identity is reused rather than duplicated. Ambiguous person matches and suppression constraints fail closed.

Promotion outcomes include creation, existing/exact linking, already-promoted/idempotent results and blocked/unresolved states.

No email is invented during promotion.

## 7. Campaign filing

A capture can carry an immutable request to file the resulting Contact into a Campaign.

Filing is applied only after a Contact exists.

Therefore a capture may correctly show:

- filing request `PENDING`;
- `attempts=0`;
- no filing error;

when promotion has never produced a Contact. That shape means `apply_filing` was never entered; it is not automatically a Campaign readiness failure.

Campaign enrollment is idempotent per `(Campaign, Contact)`. Repeated sightings/captures of the same person can therefore produce more applied filing intents than distinct Campaign memberships.

## 8. Pending worker recovery

The worker has a supported pending-resolution/backfill path.

During Hosted Beta UAT, captures staged while promotion was disabled were recovered simply by enabling the valid runtime group and restarting the normal worker. The worker drained pending undecided captures through the application service path.

No manual SQL, hand-created Contacts or hand-created CampaignContact rows were required.

## 9. Important retry semantics

`pending_capture_ids` deliberately targets captures that have not already received a current resolution decision.

Consequences:

- a capture that was never attempted can be picked up when runtime capability is restored;
- a capture with a current `UNRESOLVED` decision does not loop forever on every worker pass;
- operator confirmation or explicit forced re-resolution is required to revisit a decided unresolved capture.

This is why the real UAT remainder stays pending until an operator resolves the ambiguity.

## 10. Model fallback

Model company-domain fallback is optional and bounded.

Current Hosted Beta has the model lookup feature switch enabled, but the VPS lacks the `claude` executable on PATH. Real fallback calls therefore returned `API_UNAVAILABLE`.

A provider/model feature switch is not proof that the underlying executable/API runtime is actually available.

Records whose model lookup already moved from `NOT_STARTED` to `API_UNAVAILABLE` are not automatically retried by the ordinary non-force path; a later intentional retry requires explicit force.

## 11. Real UAT evidence

Original diagnosed cohort: 50 recent VM Prospector captures targeting Campaign `PE&VC MENA 200-1000`.

Before configuration repair:

- filing request present: 50;
- requested target Campaign: 50;
- promotion attempted: 0;
- Contact rows: 0;
- CampaignContact rows: 0;
- filings: 50 `PENDING`, `attempts=0`.

Root cause: `CONTACT_CAPTURE_PROMOTION` and `AUTOMATIC_COMPANY_DOMAIN_RESOLUTION` were absent/default false.

After controlled runtime repair and normal worker recovery:

- original 50: 18 provisional/resolved enough to promote, 32 unresolved;
- 18 distinct target Campaign memberships;
- 32 filings remain pending;
- 0 filing failures.

Wider filing-requested cohort observed in the same recovery:

- 72 filing-requested captures;
- 27 applied filings;
- 45 pending;
- 0 failed;
- 18 distinct memberships because 9 applied filings were repeat sightings of already-enrolled people.

## 12. What promotion does not do

Promotion does not:

- weaken suppressions;
- fabricate a company domain;
- invent an email;
- research the Company;
- verify email;
- generate Insights or Personalization;
- approve a draft;
- grant sending authority;
- send email.

It creates/links permanent identity and applies only the explicit filing intent already carried by the capture.

## 13. Operator workflow

Unresolved captures remain visible for operator domain/company confirmation.

The operator should be able to inspect candidate evidence, confirm/reject/enter a domain or deliberately leave the capture unresolved.

Do not bulk-force ambiguous companies into Campaign processing merely to improve throughput numbers.

## 14. Current next gate

Capture connectivity, filing intent, automatic promotion and Campaign membership have now been proven on Hosted Beta.

The current real-contact UAT blocker is downstream Research effective control, not capture promotion.
