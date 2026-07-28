# DAT-020B — deterministically attach redirected vanity captures using operator-click/tab provenance

**Status:** recorded, NOT implemented. Deliberately excluded from
`fix/acquisition-friction-dat-020-dat-017a`.

This file is the paste-ready body for the GitHub issue. It exists because
DAT-020A completed without it, and the gap it describes is real, narrow, and
should not be rediscovered later by accident.

---

## The gap DAT-020A deliberately left open

DAT-020A stores three distinct values for one person — the observed vanity URL,
the derived resolving alias, and the opaque Sales Navigator member id — and
promotes a Contact from the member id alone when no handle is known yet.

What it does **not** do is close the loop the operator actually walks:

1. A Sales Navigator row yields member id `ACwAAAB1x9k`, and a Contact is created
   carrying an active `SALESNAV_MEMBER_ID` claim and no LinkedIn URL.
2. The operator clicks the derived alias. LinkedIn redirects to the person's real
   `/in/<vanity-handle>` page.
3. The operator captures that page. It is a normal profile capture: it shows a
   handle and **no member id**, because a profile page never displays one.

At step 3 the system has two captures of one human and **no deterministic
evidence linking them**. DAT-019's only automatic bridge,
`SAME_CAPTURE_OBSERVED`, requires both identifiers observed in one capture, which
is exactly what did not happen. So a second Contact is created.

This is the correct outcome under current evidence. Name, company, title,
headline, location and AI judgment are all explicitly insufficient, and bridging
on them would be the fabrication DAT-019 was built to stop. The missing thing is
not a matching rule — it is **evidence**.

## What would make the link deterministic

The evidence exists at capture time and is thrown away: the extension knows it
opened the alias for member `X` in tab `T`, and it knows the capture at step 3
happened in tab `T` after that navigation. Carrying that fact across the redirect
turns an inference into an observation.

Sketch, to be designed properly rather than treated as settled:

- When the operator opens a derived alias, the extension records
  `(tabId, member_id, opened_at)` in service-worker state.
- When a profile capture occurs in that tab and the landing URL is the redirect
  target of that navigation, the capture carries a new, explicitly named
  provenance field — e.g. `alias_redirect_from_member_id` — alongside the
  observed handle.
- Intake persists it as capture evidence, never as an identifier in its own right.
- Promotion treats "this capture observed handle `H`, and the extension witnessed
  it as the redirect destination of alias for member `X`" as a new decision kind
  (a sibling of `SAME_CAPTURE_OBSERVED`, **not** a loosening of it), attaching a
  `PUBLIC_VANITY_URL` claim to the Contact that already holds the `X` member-ID
  claim.

## Requirements the implementation must satisfy

- The member-ID claim is **retained**, never replaced or deleted.
- The vanity claim is added only where the redirect provenance is genuinely
  witnessed; a stale or ambiguous tab record must fail closed to two Contacts.
- Never bridge by name, company, title, headline, location or AI judgment.
- Historical suspected aliases stay flagged and excluded from canonical matching.
- Recapture stays idempotent: no duplicate Contact, no duplicate active claim.
- The provenance is operator-initiated. No autonomous browsing, no navigation the
  operator did not perform, no anti-bot or platform-terms bypass.
- Trust boundary: this field is extension-asserted. It must be reviewable, and it
  must be impossible for a forged or replayed value to merge two real people
  silently — prefer routing a disagreement to review over accepting a merge.

## Acceptance criteria

- [ ] Opening a derived alias and capturing the redirected profile attaches the
      observed vanity URL to the **same** Contact created from the member id.
- [ ] That Contact ends with both claims active: `SALESNAV_MEMBER_ID` and
      `PUBLIC_VANITY_URL`.
- [ ] The Contact then displays the real handle as canonical, with the member id
      still retained and visible separately.
- [ ] Capturing the same profile without the redirect provenance still creates a
      separate Contact rather than guessing (fail closed).
- [ ] A conflicting provenance claim routes to operator review; it never merges.
- [ ] Repeat capture from either surface creates no duplicate Contact or claim.
- [ ] Extension and backend tests cover the witnessed redirect, the missing-
      provenance case, the conflicting case, casing, and idempotency.

## Relationship

- Completes the #215 acceptance path that DAT-020A intentionally stopped short of.
- Depends on DAT-020A (three-value separation and the member-ID promotion path).
- Does **not** block DAT-011 / #131 any more than DAT-020A already unblocks it:
  the daily operator navigation capability is restored; only the automatic
  re-linking after the redirect remains manual.
