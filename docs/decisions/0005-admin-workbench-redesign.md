# 0005 — Admin Workbench: one operator product over the existing backbone

Date: 2026-08-05. Status: accepted.

## Context

The Admin UI grew one feature and one screen at a time — import workbench,
Agent monitor, Agent Studio, CRM pages, verification console, Company
Intelligence — each with its own entry point and navigation. The result
reflected implementation history rather than how an operator understands the
application, and diagnosing one Contact required knowing which historical
surface held which fragment.

## Decision

Build a single Admin Workbench at `/admin` as the primary operator surface,
organised around Campaign -> Contacts -> Agent/Stage progress -> worker ->
Agent Job -> attempt -> evidence and corrective action. Specifically:

1. **New presentation layer, no new authority.** A read-only reader
   (`app/services/admin_workbench`) projects committed state, reusing the
   existing authoritative projections (`PhaseTwoWorkbenchReader`, drafts,
   policy services, the durable Research report). Mutations reuse
   `WorkbenchCommands` unchanged. No second retry/control system, no new
   tables, no migration.
2. **Route strategy: mount before, shadow one path.** The new router is
   included before the legacy web router. It takes over `/admin` (the old
   import overview moves to `/admin/legacy/overview`) and claims previously
   unused `/admin/...` paths. Every other legacy route keeps resolving in
   `app/web/routes.py` unchanged and is catalogued under
   `/admin/diagnostics`; the specialised workflows (imports, identity review,
   verification, KB, CI, local tools) stay linked from the new rail.
3. **Gating split: read vs act.** Pages render under `FEATURES__WORKBENCH`
   alone (still hard-locked to `APP_ENV=local`); corrective actions
   additionally require `FEATURES__AGENT_WORKBENCH` and refuse visibly
   without it. Reading the truth should not require the command switch.
4. **Truth rules carried over from the legacy macro sets.** No template
   derives a state; unimplemented areas (Sending) and lineage-free history
   render explicit unavailable states; failure categories map only from
   committed fields.
5. **Third stylesheet, not a reskin.** `admin.css` serves only
   `templates/admin/`; `app.css` (legacy admin) and `v2.css` (customer)
   are untouched, so the retained surfaces keep rendering exactly as before.

## Consequences

* The operator's primary path (Campaign -> Contact -> stage timeline -> Job)
  is one product with one visual system; the legacy surfaces survive as
  Advanced Diagnostics until each workflow is migrated deliberately.
* Two shells exist during the transition (new `admin.css` shell, legacy
  `app.css` shell). Accepted: it keeps the redesign additive and reversible.
* Tests pinning the old `/admin` behaviour were updated to assert the new
  shell plus the preserved legacy address, keeping the no-broken-bookmarks
  guarantee executable.
