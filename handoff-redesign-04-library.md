# Handoff — Redesign slice 4: Library, read by everyone, edited by administrators

**Branch:** `redesign/04-library` · contains main-reconciled Slice 3 via merges `dec86686`, `c1c4408a`, `f9656cd1`, `47b30044` · merge after PR 3
**Spec:** VMR_OUTBOUND_UX_IA_PASS2.md section D.7, Phase 5. Locked with Sahil (16 Aug): the Library is only editable by an Admin.

## What changed

**Library (`/app/library`)** — sections renamed for the customer: Overview · **Business profile** (company) · Offerings · **Proof** (proof points) · **Message rules** (restricted claims) · Personas. Readiness links on the overview point at the renamed sections. Every section stays readable by every signed-in user (KB-001: reading what we may say is ordinary work).

**Editing in place, admin-only** — the forms that used to live on the legacy `/knowledge-base/*` pages now render inside the Library sections `{% if is_admin %}` (`_library_forms.html`: profile, offering + state + link/unlink, proof point, message rule, persona; archive/restore as `action=` on the update routes). All writes are under `/app/admin/library/…`, so the existing `/app/admin` prefix rule in `app/core/auth/policy.py` refuses non-admins with `admin_required` — no new policy code. Non-admins see "Only an administrator can change it." in the section head.

Services untouched: the routes call the same `seller_knowledge` write functions as before.

## Validation
- ruff / ruff format / mypy clean.
- New `tests/test_library.py` (4): user reads every section and sees no form; admin sees forms; user POST to an admin library route → 403 `admin_required`; admin edits an offering and the change renders.
- `tests/test_route_authorization.py`: the KB asymmetry test is restated for the Library (in slice 5, where the legacy pages leave).

- On the reconciled head `f9656cd1`: library, route authorization, customer UI, seller knowledge, extension account linking, sending desk — 493 collected, all green except one main-only test whose ~40 KB parametrize id overflows Windows' `PYTEST_CURRENT_TEST` env var (identical error on pristine main).

## Notes for review
- No schema change. Knowledge-base *generation* (`/knowledge-base/generate`, the Claude-CLI subprocess) is not carried into the Library UI; it is retired with the legacy pages in slice 5 and listed there as a deferred Admin data tool.

## Proposed tracker payload
| Field | Value |
| --- | --- |
| Item | UX Pass 2 — slice 4: Library edited in place by administrators |
| Branch | `redesign/04-library` @ head (stacked on slice 3) |
| State | Built; targeted tests green; awaiting PR + CI + review after slice 3 |
| Risk | Writes moved under the admin prefix; same services; no schema |
| UAT | Sign in as a user → Library readable, no forms; as admin → edit an offering, add a message rule, archive/restore |
