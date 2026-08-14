# Customer Operating Model

## Governing product rule

> **VMR Outbound is autonomous until Ready for Sending.**

The customer is not an operator of the internal Agent pipeline.

A normal customer:

1. creates and configures a Campaign;
2. adds or captures Contacts, including through the Chrome extension or supported import paths;
3. lets VMR process those Contacts autonomously;
4. monitors progress when useful;
5. takes over once a Contact is **Ready for Sending**.

From that point the customer may inspect or edit the seven-message sequence and perform sending-related actions manually. VMR must not manufacture a customer task list from internal machine state.

## Customer-visible states

The primary customer journey uses three high-level states:

- **Processing** — VMR is still preparing the Contact.
- **Ready for Sending** — the usable outbound package exists.
- **Could not prepare** — VMR reached a terminal condition and could not produce the outbound package.

Detailed Agent states remain available for diagnostics and observability. They are not nine separate customer tasks.

## Ready for Sending

A Contact is Ready for Sending when the current Campaign Contact is still eligible and unsuppressed and VMR has successfully produced the usable outbound package required by policy, including:

- sufficient usable Company/Research knowledge;
- a usable email address accepted by the current verification policy;
- completed Insights;
- completed Personalization;
- a validated seven-message sequence.

The default cadence is:

`0, 3, 7, 12, 18, 25, 35` days.

The sequence is usable without a human approval click. A missing review row is not a backlog and does not prevent Ready for Sending.

## Optional inspection and editing

Customers may inspect any generated message and may edit it.

Editing writes a new immutable version. Historical versions and any real human decisions remain auditable. Inspection and editing are optional; neither is a prerequisite for readiness.

## Human action versus machine state

The customer UI must distinguish these two categories.

### User input required

A specific customer-owned input may be requested when the system genuinely cannot proceed without it, for example incomplete Campaign setup or a required Campaign-level consent for live paid work.

These requests must be specific and contextual. They must never be mixed into a generic "Needs you" or "things want you" total.

### System could not complete work

Provider failures, model failures, failed Agent jobs, blocked Agent jobs, retries, enrichment gaps, Research failures, Verification failures, Insights failures, Personalization failures and other internal execution conditions are system concerns.

The customer may see that a Contact could not be prepared and may inspect details, but the application must not present internal failures as a mandatory customer inbox.

## Research and downstream knowledge

Research is a reusable Company knowledge function, not a one-campaign artifact. It may run repeatedly over the life of a Company and keep adding sourced facts, structured knowledge and newer dossier versions.

Insights reads the **current eligible Research/Company knowledge available when Insights starts**. Personalization reads the **current eligible Research and Insights knowledge available when Personalization starts**.

Versioning and lineage answer **what a run used**. They must not become a historical-predecessor prerequisite that blocks an otherwise valid run.

## Admin boundary

Admin is the diagnostic and recovery surface for:

- failed or blocked Agent jobs;
- retries and reruns;
- provider/model errors;
- queue and lease state;
- detailed resolution state;
- global controls and Campaign overrides;
- operational recovery.

Normal customers should not need to understand those internals to operate the product.

## Sending boundary

VMR does not automatically send outreach.

Gmail draft creation, where enabled, is an explicit customer action. Sending remains manual. No sequence generation, default readiness state or optional edit grants automatic send authority.

## Product test

A healthy customer journey is:

```text
Create Campaign
→ Capture / add Contacts
→ VMR processes autonomously
→ Ready for Sending
→ inspect/edit if desired
→ customer performs sending-related actions manually
```

Any design that inserts routine operator approvals, stage-by-stage intervention, generic task counts or manual recovery work into that path must be justified as a genuine customer-owned input. Otherwise it belongs in Admin diagnostics, not the customer workflow.
