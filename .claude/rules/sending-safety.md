---
description: Draft approval, suppression and sending boundaries
paths:
  - "app/services/email/**"
  - "app/services/gmail/**"
  - "app/services/sequences/**"
  - "app/services/crm/**"
  - "app/models/**"
---

# Sending and contact-safety boundaries

Authority: `docs/AGENTS.md`, `docs/ARCHITECTURE.md`, `docs/EMAIL_SEQUENCE.md`,
`docs/GMAIL_DRAFTS.md`.

- **Approval is not sending authority.** `DraftVersion` is immutable; `DraftApproval`
  records a human decision against one exact version. Editing a draft invalidates the
  approval. Approval alone never authorises a send.
- **There is no automatic sending.** `AgentIdentifier.SENDING` has no adapter. Do not
  add one, and do not add scheduling that would send, without a separately implemented
  and explicitly approved slice.
- **Never contact suppressed, opted-out, hard-bounced, or invalid addresses.** Check
  suppression at the point of use, not only at list build time.
- **Contacts are permanent and never require a campaign.** Acquiring a contact never
  makes it outreach-eligible. Only an exact normalized LinkedIn profile URL may
  auto-match an existing contact.
- Default sequence ladder is **0, 3, 7, 12, 18, 25, 35** elapsed days
  (`app/services/personalization/cadence.py`). Changing it is a product decision, not
  an implementation detail.
