# VMR Outbound Agent

VMR Outbound Agent is a private, single-operator outbound operating system built around permanent Contacts, reusable Companies and observable Campaign execution.

## Current MVP

The current MVP produces a trustworthy human-approved email draft:

```text
Capture authorized prospect
→ resolve Contact and Company
→ gather sourced Company research
→ discover and verify an exact email
→ generate evidence-backed Insights
→ generate Campaign-specific Personalization
→ approve or discard one exact immutable draft
```

It does **not** send email. SalesHandy/provider submission, delivery events, replies, sequences and analytics are post-MVP work.

See [`docs/CURRENT_MVP.md`](docs/CURRENT_MVP.md) for the authoritative status, limitations and acceptance plan.

## Delivery status

- **Campaign pipeline:** PR #232, merged into `main`.
- **Customer-facing v2 application:** PR #233.
- **Operating acceptance:** one real Contact followed by a controlled 10–20 Contact batch.

Green CI proves the implementation gates. It does not replace live website, MillionVerifier and Claude CLI acceptance.

## Product surfaces

| Route | Purpose |
| --- | --- |
| `/` and `/app` | Customer-facing application |
| `/app/review` | Review evidence and approve/discard an exact immutable draft version |
| `/admin` | Operator/admin Workbench for jobs, controls, retries and authoritative write paths |

The v2 customer interface and Workbench share services and models but use separate routers, templates and stylesheets.

## Agent pipeline

1. **Capture Agent** — existing contact-first capture and promotion path.
2. **Identity Agent** — authoritative LinkedIn identity convergence.
3. **Company Agent** — permanent Company and domain linking.
4. **Research Agent** — deterministic registered workers that persist sourced evidence.
5. **Email Agent** — ordered candidate generation.
6. **Verification Agent** — durable exact-address verification.
7. **Insights Agent** — Claude CLI interpretation of persisted evidence.
8. **Personalization Agent** — Claude CLI generation of an immutable draft.
9. **Sending Agent** — registered but disabled; no production adapter exists.

The common worker uses the durable PostgreSQL Agent Job queue and supports parallel, Agent-scoped pools.

## Research and AI boundary

Research gathers evidence and writes the raw submission, a versioned dossier and sourced fact records. It does not use a language model and does not rewrite canonical Company fields.

Claude is used only by Insights and Personalization through one bounded thinking seam. Both run with `allowed_tools=()` and cannot verify email, change controls, approve their own draft or send.

## Email policy

The Email Agent tries at most three candidates and stops after the first verified result:

1. `firstname.lastname`
2. `firstname`
3. `finitiallastname`

It enqueues one child Verification Agent Job at a time and resumes from the committed Verification outcome.

Live MillionVerifier use requires the feature switch, valid credentials and effective Verification Agent configuration containing `{"live": true}`. Simulated evidence cannot complete a live Campaign stage.

## Core objects

- **Contact** — permanent canonical person.
- **Company** — permanent reusable organization.
- **Campaign** — Campaign-specific operating context and Agent controls.
- **Campaign Contact** — Campaign membership, pipeline state and draft boundary.
- **Collection** — reusable Contact grouping; the extension may call it a Label.
- **Agent Job** — resumable, inspectable unit of work with attempts, errors, leases and audit history.
- **DraftVersion** — immutable Campaign-specific draft output.
- **DraftApproval** — human decision against one exact draft version.

Campaign membership never owns or duplicates the permanent Contact or Company.

## Current operating choices

- Capture remains Campaign-independent.
- Campaign enrolment is explicit and reversible through the Workbench, including bulk enrolment.
- Knowledge Base editing remains on `/admin`; the customer interface reads it.
- Capture-domain decisions and suppression creation retain one authoritative admin write path.
- Unsupported capabilities are shown as unavailable instead of being represented with fabricated metrics or actions.

## Not built

- sending-provider integration and outcome synchronization;
- sending, replies, sequences and analytics backends;
- deterministic fit/confidence scoring;
- Saved Audience criteria;
- extension Campaign auto-add;
- multi-email cadence generation;
- draft editing or auto-send.

## Windows quick start

```bat
cd "C:\Users\sahil\Personal Data\VMR Data - Laptop\Outbound Agent\vmr-outbound"
git checkout feat/v2-customer-ui
git pull
run_vmr_app_v2.bat
```

Start the worker in another CMD window:

```bat
cd "C:\Users\sahil\Personal Data\VMR Data - Laptop\Outbound Agent\vmr-outbound"
run_vmr_worker.bat 8
```

The first live acceptance should use one Contact before increasing batch size.

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

- [`docs/CURRENT_MVP.md`](docs/CURRENT_MVP.md) — current product and acceptance truth
- [`docs/GOAL.md`](docs/GOAL.md) — authorized MVP outcome
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — current data and Agent architecture
- [`docs/PHASE_2_EXECUTION_MODEL.md`](docs/PHASE_2_EXECUTION_MODEL.md) — durable queue and pipeline contract
- [`docs/PROJECT_TRACKING.md`](docs/PROJECT_TRACKING.md) — management tracking rules
