# Handoff — Redesign slice 2: inline sending desk, manual email actions, Today

**Branch:** `redesign/02-sending-desk` (pushed) · stacked on `redesign/01-shell-navigation` · merge after PR 1
**Spec:** VMR_OUTBOUND_UX_IA_PASS2.md sections D.3–D.4, F, G, Phase 3–4. Locked with Sahil: approval dropped from the UI; Mark actioned is the human step; desktop-first.

## What changed

**Schema (Alembic `a7d3e5f19c22`, additive, reversible)**
- `sequence_email_actions` — append-only ledger of explicit acts on one email: `ACTIONED` / `SKIPPED` / `UNDONE` (an undo names the row it reverses). Records membership, Campaign, sequence key, message id, **exact message version**, position, actor, time, note. Nothing here sends.
- `today_dismissals` — (user, campaign, local day) “hide this card for me today”.
- `APP_TIMEZONE` setting (default `Asia/Kolkata`) for whole-day due arithmetic.

**Projection** — `app/services/email_progress.py`: per ready person, the seven email states (Ready / Upcoming / Due today / Overdue / Actioned / Skipped), Day 0, next email, due label, progress “n of 7 actioned”, last action. **Email 1 marked Actioned establishes Day 0**; Emails 2–7 are due on the fixed ladder 3/7/12/18/25/35 days from it, never from the previous action; acting late does not slide the cadence. `mark_actioned` / `skip_follow_up` (positions 2–7 only) / `undo` with customer-language refusals; a person must be Ready for Sending.

**Overview → Ready for Sending** — filters Due now (default) · All ready · First email · Follow-ups · Actioned; columns Person, Company, Next email, Due, Progress, Last action. Selecting a person opens the **inline sending desk** (`?person=&email=&section=`; still Campaign Overview): roster of ready people on the left, workbook on the right — header (person, company, i of N, due phrase, Previous/Next person, Open person), seven-step rail with state markers, document card (To, Subject, body, cadence day, state) with **Copy** (existing sequence.js), **Create Gmail draft** (one email, one draft; “Nothing is sent or scheduled”; stale-draft notice when the message was edited), **Mark actioned** / **Undo**, and beneath: Edit (new version, history kept), Why this email? (angle, based on, company context, research, validation — no ids/providers), History (versions + acts), Skip this follow-up (confirmed). Keyboard (`desk.js`): J/K or ↑/↓ people, ←/→ emails, Esc closes; nothing consequential on a keystroke. `POST /app/campaigns/{id}/desk/{membership}/{position}/{actioned|skip|undo|edit|gmail-draft}`.

**Gmail** — `gmail_drafts.create_draft(message_version_id=…)`: one current version, no review requirement, same reservation/idempotency/lineage as the batch path (kept for the person page’s existing callers only through routes; UI now uses one-email).

**Today** — rebuilt as the return surface: Due follow-ups grouped by Campaign (count, overdue, “Next: Email n for most people”, Open Campaign → desk on the first due person, Dismiss for today), Ready for first email, Campaigns in motion, Needs your setup (no people yet; no offering chosen). `POST /app/today/dismiss` needs a signed-in account.

**Person page** — seven emails still readable; confirm/discard buttons and the seven-draft Gmail panel removed; **Open in Campaign** deep-links to the desk. (Full Person/Company redesign is the next slice.)

## Validation
- ruff / ruff format / mypy clean. `tests/test_migrations.py` 17/17 (upgrade → check → downgrade → re-upgrade; check-constraint names match models).
- New `tests/test_sending_desk.py` (16): Day 0, due ladder vs previous action, skip/undo/refusals, exact-version record surviving an edit, desk over HTTP (columns/filters, workbook opens in place, mark actioned advances, skip needs confirmation, edit writes a version), Today due cards, dismissal per user, dismiss needs sign-in.
- Regression on the affected files (customer UI, operating model, route authorization 100 reachable, Gmail integration, sequence web/defects, beta1 operator UI, execution readiness): green. Full suite is CI’s.
- Screenshots on a demo database: Ready table with filters; desk open on Email 1; desk after Actioned/Skip (rail states, Day 0, “In 12 days”); Today with “Ready for first email”.

## To run locally
`alembic upgrade head` (two new tables); optional `APP_TIMEZONE`.

## Proposed tracker payload
| Field | Value |
| --- | --- |
| Item | UX Pass 2 — slice 2: inline sending desk + Today |
| Branch | `redesign/02-sending-desk` @ head (stacked on slice 1) |
| State | Built; migration proven; targeted tests green; awaiting PR + CI + review after slice 1 |
| Risk | Additive schema; no send path; Gmail one-draft reuses existing lineage; admin-only classifications unchanged |
| UAT | Ready person → open in place → Copy / Gmail draft / Mark actioned → Day 0 → Today shows due follow-ups → Dismiss vs Skip distinct |
