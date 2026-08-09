# Decision 0007 — Human-controlled Google delivery

Status: Approved architectural direction; current sequencing superseded by Decision 0010

Date: 2026-08-07; reconciled 2026-08-09

## Decision

VMR does not build an autonomous Sending Agent as the default delivery model.

If and when Google/Gmail capabilities are introduced, they remain two deliberately separate permission boundaries:

1. **Google sign-in / Workspace identity** authenticates an internal person to VMR.
2. **Gmail mailbox authorization** separately grants only the Gmail permissions required for operator-triggered mailbox actions.

Google sign-in alone must never imply Gmail mailbox access.

## Current sequencing update

Decision 0010 changes the delivery order, not this architectural boundary.

The immediate milestone is now the hosted manual-copy Beta:

```text
Chrome Extension capture
→ authenticated hosted VMR
→ Campaign / Contact / Agent stages
→ seven-message sequence
→ operator inspects / optionally edits / copies exact email text
→ manual outreach outside VMR
```

Gmail mailbox authorization and Gmail draft creation are postponed until after the operator has personally used and accepted that workflow with real contacts.

## Deferred Gmail slice

When Gmail work resumes, the first intended capability remains narrow:

```text
operator opens one exact current VMR sequence message/version
→ operator clicks Create Gmail Draft
→ VMR creates or updates one draft in that operator's authorized mailbox
→ VMR stores durable mailbox/draft/message-version lineage
→ operator continues manually in Gmail
```

VMR does not auto-send.

Repeated/retried draft creation must be idempotent and must not create duplicate drafts accidentally.

## Deferred

The following are explicitly not required for the hosted manual-copy Beta:

- Gmail mailbox authorization;
- Gmail draft creation;
- automatic cadence scheduling;
- automatic creation of later follow-up drafts;
- sent-message observation;
- reply/thread monitoring;
- automatic suppression/stop transitions driven by Gmail state;
- automatic sending;
- Google Sheets synchronization.

## Consequences

- VMR remains authoritative for evidence, policy, generation versions and sequence/message/version identity.
- the Chrome extension authenticates only to VMR and never receives Google/Gmail tokens;
- future Gmail credentials/tokens remain server-side and encrypted;
- no provider action may fabricate sent/delivered state;
- automatic Sending remains deferred;
- no Gmail implementation may delay the hosted manual-copy Beta defined in Decision 0010.
