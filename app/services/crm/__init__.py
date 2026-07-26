"""The contact CRM (APP-002).

The operator workspace is built around permanent people, not campaigns. Every
service in this package takes contacts and captures as its subject and **never**
accepts a campaign identifier: a campaign is a downstream execution object that
consumes a saved audience much later in the workflow.

Two kinds of record share the workspace, because a person the operator saved is
not allowed to disappear just because the system has not finished resolving
them:

* a **canonical contact** — a permanent ``contacts`` row;
* a **pending capture** — an immutable ``linkedin_profile_snapshots`` row whose
  outcome is ``unmatched_staged`` or ``ambiguous_review``, so no contact row
  exists yet.

They are unified by a read model (:mod:`app.services.crm.records`) rather than
by inventing provisional contact rows. ``Contact.company_domain`` stays
``NOT NULL`` on purpose: a canonical contact has a resolved company, and the
route from a captured company name to a domain is DAT-010's logo.dev candidate
flow plus an operator confirmation, not a guess made here.

Module map:

* :mod:`app.services.crm.states` — the four workflow dimensions, derived from
  authoritative sources rather than stored on the contact.
* :mod:`app.services.crm.records` — the unified list read model, its filters,
  and the predicates a saved audience will later reuse.
* :mod:`app.services.crm.detail` — the contact and capture detail read models.
* :mod:`app.services.crm.annotations` — label and note writes.
"""
