"""The contact CRM (APP-002).

The operator workspace is built around permanent people, not campaigns. Every
service in this package takes contacts and captures as its subject and **never**
accepts a campaign identifier: a campaign is a downstream execution object that
consumes a saved audience much later in the workflow.

Two kinds of evidence share the workspace:

* a **Contact** — the permanent ``contacts`` row, including while identity or
  company fields are unresolved;
* a **pending capture** — an immutable ``linkedin_profile_snapshots`` row whose
  exact identifiers conflict and therefore cannot safely select one Contact.

They are unified by a read model (:mod:`app.services.crm.records`). A missing
``Contact.company_domain`` is NULL, never a placeholder; DAT-010's evidence and
operator/automatic resolution later complete the same person record.

Module map:

* :mod:`app.services.crm.states` — the four workflow dimensions, derived from
  authoritative sources rather than stored on the contact.
* :mod:`app.services.crm.records` — the unified list read model, its filters,
  and the predicates a saved audience will later reuse.
* :mod:`app.services.crm.detail` — the contact and capture detail read models.
* :mod:`app.services.crm.annotations` — label and note writes.
"""
