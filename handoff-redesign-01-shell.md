# Handoff — Redesign slice 1: four-destination shell and the Campaign workspace

**Branch:** `redesign/01-shell-navigation` (pushed) · base `origin/main` d5d2919d · 4 commits
**Spec:** VMR_OUTBOUND_UX_IA_PASS2.md (Cowork + ChatGPT), sections B–E, Phase 1–2, decisions locked with Sahil on 16 Aug 2026 (approval step dropped from the UI; Admin hidden now / consolidated last; one PR per slice; desktop-first).
**Stacked on it:** `redesign/02-sending-desk` (inline sending desk + Today) — separate PR after this one.

## What changed

**Shell** — customer navigation is exactly **Today · Campaigns · People · Library** plus a role-gated **Admin** entry in the same header; one **Add people** button; account menu with **Connections** (Gmail) and Sign out. No badges, no Capture pill, no Emails/Review, no Agent/Knowledge/Suppression items. (`app/web/v2/shell.py`, `templates/base.html`)

**Campaign workspace** — `/app/campaigns/{id}` becomes four tabs sharing one header (name, lifecycle pill Draft/Active/Paused/Archived, three outcome counts, Add people, overflow with Pause/Resume/Start and Archive→Setup):
- **Overview** — outcome proportion bar + one “what is happening” sentence, **Ready for Sending** table (only people whose current package satisfies the projection), Could-not-prepare card with plain-language top reasons, Setup summary, recent activity. Admin-held readiness (an Agent switched off) is said as “Preparation is being held by an administrator setting” with an admin-only diagnostics link — no Agent names.
- **People** — everyone in the Campaign, All / Processing / Ready for Sending / Could not prepare filter, search, plain detail per row (`campaign_workspace.py` maps stages to sentences such as “No email address could be found”).
- **Setup** — name/note, offering (single select), CTA and direction (they reach the writer), best-available-website policy, seven-email switch (when the deployment switch is on), website research (admin toggle = Research live opt-in), access (owner/assignees, admin assign/unassign), lifecycle Start/Pause/Resume, Archive with confirmation; ends with the computed answer.
- **Activity** — lifecycle/setup/access audit lines, people added by source, “Emails written for N people”, files imported.
- **New Campaign** — short form (name, offering, direction, note); creates **and starts** the Campaign; `allow_provisional_domains` defaults on.
- **Add people** — `/app/add-people` (choose Campaign) and `/app/campaigns/{id}/add-people`: three source cards (Chrome extension / Google Sheets / Import a file) with feature-switch state; file import flow unchanged underneath.

**Moved / retired**
- `/app/review` → 308 into Campaigns (or the person inside their Campaign when `?sequence=`); `/app/contacts*` → `/app/people*`; `/app/knowledge*` → `/app/library*`; `/app/capture` → `/app/add-people`; `/app/sending|replies|sequences|analytics` → `/app`; `/app/agents` → `/app/admin/agents`; `/app/suppressions` → `/app/admin/suppressions`; `/app/campaigns/{id}/edit` → `/setup`.
- Agent tiles, live opt-in, re-run and rerun-per-person moved to **`/app/admin/campaigns/{id}/diagnostics`** (admin-only). Global Agent controls now `POST /app/admin/agents/{id}/control`. Legacy single-draft approve/discard routes removed. `POST /app/campaigns/{id}/setup/research` is administrator-only in `app/core/auth/policy.py`.
- Person page (`/app/people/{id}?campaign=`) is unchanged for now (still the interim seven-email view); slice 2 replaces the working surface.
- Routes split into `app/web/v2/pages/{today,campaigns,imports,emails,people,library,admin,account,legacy}.py`; `app/web/v2/context.py` gone.

## Validation

- ruff check / ruff format --check / mypy: clean.
- Targeted pytest locally (Windows; PostgreSQL 18): route authorization 182/182; customer UI, operating model, Gmail, sequence web/defects, campaign auth/live-opt-in/rerun/readiness/enrolment/sequence-control, imports, hosted auth, user accounts, extension auth, admin workbench, agent studio, migrations — all green after the test repoints in commit 34d534e2 (see below). Not run locally: the full ~2,900-test suite (≈4 h on this machine) — CI is the authority.
- Screenshots reviewed locally against a real dev database: campaign list, overview (paused campaign with 128 could-not-prepare), people, setup, add-people, new campaign, admin landing.

**Test changes** (all deliberate): `test_v2_customer_ui.py` rewritten for the new IA; `test_review_copy_controls.py` deleted (its subject page is gone); review-page tests removed from `test_email_sequence_web.py`/`test_email_sequence_defects.py`/`test_gmail_draft_integration.py`; campaign-route tests repointed (`/edit`→`/setup`, `/execution`→`/lifecycle`, rerun/live→admin); `EXPECTED_USER_REACHABLE` 88→94.

## Known follow-ups (deferred backlog, not blockers)

- Legacy one-email Campaigns (not opted into sequences) still project as “Could not prepare” once a single draft exists — pre-existing projection rule; visible on Sahil’s older campaigns.
- Google Fonts `@import` in `v2.css` is blocked by the deployed CSP (`style-src 'self'`) so staging renders system fonts; make it deliberate in the polish slice.
- Person/Company detail, Library editing, and Admin consolidation are later slices per the plan.

## Proposed tracker payload

| Field | Value |
| --- | --- |
| Item | UX Pass 2 — slice 1: shell + Campaign workspace |
| Branch | `redesign/01-shell-navigation` @ head |
| State | Built, tests green locally on the affected set; awaiting PR + CI + review |
| Risk | UI/IA only; no schema change; no send/schedule path; admin-only verbs re-classified in policy |
| UAT | Create Campaign → Add people (any source) → Overview shows outcomes → People/Setup/Activity → legacy bookmarks redirect |
