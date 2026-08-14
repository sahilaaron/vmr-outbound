# VMR Outbound Agent

VMR Outbound Agent is a private, contact-first outbound operating system built around permanent Contacts, reusable Companies, governed Campaign execution and human-controlled outreach.

## Current-state authority

For the current merged product, live Hosted Beta runtime, active UAT and work-in-flight distinction, read:

[`docs/CURRENT_PRODUCT_STATE.md`](docs/CURRENT_PRODUCT_STATE.md)

That document deliberately separates:

- what is merged to `main`;
- what is actually deployed and verified on the VPS;
- what is currently under development;
- what is known UAT debt rather than current product behavior.

Historical handoffs and review reports remain evidence of their time and are not rewritten to pretend they were always current.

## Delivery principle

The project optimizes for the **shortest safe path to real UAT**.

For a narrow fix, the normal path is:

```text
reproduce
→ smallest correct fix
→ directly affected test/file
→ touched-file static checks
→ push
→ GitHub CI
→ merge
→ deploy
→ real UAT
```

Do not duplicate broad CI locally, restart a whole-feature review for a tiny successor patch, or delay UAT for optional validation without a named risk that the extra step uniquely reduces. Substantial security, spend, migration, data-loss or sending-boundary changes still receive proportionate deeper review.

[`docs/PROPORTIONAL_VALIDATION.md`](docs/PROPORTIONAL_VALIDATION.md) is the authoritative delivery and validation policy.

## Current product path

The current product is designed around this operator flow:

```text
Capture authorized prospect
→ resolve permanent Contact and Company
→ gather sourced Company research
→ discover and verify an exact email
→ generate evidence-backed Insights
→ generate Campaign-specific Personalization
→ review/edit one immutable version lineage
→ human approval
→ create Gmail drafts
```

The product does **not** automatically send email.

Gmail draft creation is implemented as a separate mailbox grant and remains distinct from hosted identity login, extension authorization and sending authority. The fixed seven-message sequence cadence is days `0, 3, 7, 12, 18, 25, 35`; automatic scheduling/sending and reply polling are not part of the current sending path.

## Current merged / live distinction

As of 14 August 2026:

- merged `main`: `c1bd054e45e09a22d3d8cf1e7aec629226f352e4` (PR #275);
- last independently verified live Hosted Beta SHA: `d9750b008919bf2bfe42a848b0b454eeedd66f1f`;
- account-linked VM Prospector authentication is merged to `main` but must not be called live until `/version` proves a deployment containing it.

See `docs/CURRENT_PRODUCT_STATE.md` for the exact live configuration/UAT facts.

## Product surfaces

| Route | Purpose |
| --- | --- |
| `/` and `/app` | Main customer/operator application |
| `/app/review` | Review evidence and act on exact immutable draft versions |
| `/app/admin/users` | Admin-created user directory; no public signup |
| `/admin` | Admin/Workbench surfaces for global controls, diagnostics and authoritative admin actions |
| `/admin/agents/studio` | Global Agent Studio and Agent-specific admin inspection |
| `/gmail/*` | Separate Gmail mailbox authorization and draft-creation path |

The v2 application and Admin/Workbench share services and models but use separate routers and templates.

## Agent pipeline

1. **Capture Agent** — contact-first intake and promotion.
2. **Identity Agent** — authoritative LinkedIn identity convergence.
3. **Company Agent** — permanent Company/domain linking.
4. **Research Agent** — registered deterministic workers persisting sourced evidence.
5. **Email Agent** — ordered candidate generation.
6. **Verification Agent** — durable exact-address verification.
7. **Insights Agent** — bounded interpretation of persisted evidence.
8. **Personalization Agent** — bounded generation of immutable Campaign-specific copy.
9. **Sending Agent** — registered contract only; no production sending adapter.

The worker uses the durable PostgreSQL Agent Job queue and supports Agent-scoped concurrency.

## Research and AI boundary

Research gathers evidence and writes the raw submission, versioned dossier and sourced fact records. It does not silently rewrite canonical Company fields.

The live Hosted Beta currently has Company Research operationally disabled, so real Campaign Contacts can pause at Research even though the implementation exists. That is a current runtime/control-state fact, not evidence that Research code is absent. The active UAT operator-controls work is moving ordinary operational switches into an Admin-controlled product layer.

Model-backed fallback paths are bounded. On the current VPS, Claude CLI is not installed, so model company-domain fallback attempts return `API_UNAVAILABLE` until the runtime capability exists.

## Email and verification policy

The Email Agent tries at most three candidates and stops after the first verified result:

1. `firstname.lastname`
2. `firstname`
3. `finitiallastname`

It enqueues one child Verification Agent Job at a time and resumes from committed Verification outcomes.

Provider spend is controlled by the relevant execution/control/provider configuration; `DRY_RUN` is not a general verification-spend brake.

## VM Prospector extension

VM Prospector is the operator-driven LinkedIn/Sales Navigator acquisition edge.

Current merged extension behavior after PR #275:

- ordinary hosted users do not paste a backend URL or reusable capture secret;
- the extension links to the operator's VMR account through first-party authorization-code + PKCE;
- access is short-lived and refresh authority rotates;
- hosted extension authority is restricted to exactly four routes:
  - `POST /api/intake/contact-captures`
  - `GET /api/contact-labels`
  - `GET /api/contacts/lookup`
  - `GET /api/campaigns`
- the legacy `vmrx1` shared credential remains local/development-only.

See `extensions/salesnav-capture/README.md` and `docs/CURRENT_PRODUCT_STATE.md`.

## Core objects

- **Contact** — permanent canonical person.
- **Company** — permanent reusable organization.
- **Campaign** — Campaign-specific operating context and controls.
- **Campaign Contact** — Campaign membership, pipeline state and draft boundary.
- **Collection** — reusable Contact grouping; the extension may call it a Label.
- **Agent Job** — resumable, inspectable work item with attempts, errors, leases and audit history.
- **DraftVersion** — immutable Campaign-specific draft output.
- **DraftApproval** — human decision against one exact draft version.

Campaign membership never owns or duplicates the permanent Contact or Company.

## Current operating choices

- Capture remains Contact-first; Campaign filing is optional at acquisition time.
- Human edits create immutable version lineage.
- A review row represents a real human action; default approval is not human approval.
- Approval is not sending authority.
- Gmail authorization is separate from hosted identity and extension authorization.
- Secrets remain deployment/server concerns and are never rendered back to operators.
- Unsupported capabilities must be shown as unavailable instead of fabricated.

## Currently not implemented

- automatic sending;
- sending-provider scheduler/orchestration;
- reply detection/polling and threaded follow-up automation;
- delivery/reply/bounce/opt-out synchronization as an automated sending loop;
- unrestricted autonomous Agent authority.

Some older documents refer to sequences, Gmail drafts or extension account-linking as future work. Use `docs/CURRENT_PRODUCT_STATE.md` to resolve those historical statements.

## Local development

Use the repository's current development/runbook documentation rather than old branch-specific quick-start commands. Hosted Beta deployment and rollback procedures live in `docs/STAGING_RUNBOOK.md`.

## Technology

- Python 3.11+
- FastAPI and Jinja
- SQLAlchemy 2
- PostgreSQL
- Alembic
- Pydantic Settings
- Pytest, Ruff and mypy
- Manifest V3 Chrome extension using plain JavaScript

## Governing documents

- [`docs/CURRENT_PRODUCT_STATE.md`](docs/CURRENT_PRODUCT_STATE.md) — current merged/live/UAT truth
- [`docs/PROPORTIONAL_VALIDATION.md`](docs/PROPORTIONAL_VALIDATION.md) — UAT-first delivery, review and validation authority
- [`docs/CURRENT_MVP.md`](docs/CURRENT_MVP.md) — current product scope and acceptance state
- [`docs/HOSTED_AUTH.md`](docs/HOSTED_AUTH.md) — hosted identity and extension authorization boundaries
- [`docs/STAGING_RUNBOOK.md`](docs/STAGING_RUNBOOK.md) — Hosted Beta deployment/runtime operations
- [`docs/CAPTURE_PROMOTION.md`](docs/CAPTURE_PROMOTION.md) — capture→domain→Contact promotion contract
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — data and Agent architecture
- [`docs/PRODUCTION_HARDENING.md`](docs/PRODUCTION_HARDENING.md) — HTTP/runtime safety contracts
- [`docs/PROJECT_TRACKING.md`](docs/PROJECT_TRACKING.md) — management tracking rules
