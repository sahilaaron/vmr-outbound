"""The permanent company workspace (APP-003).

Company intelligence belongs to the Company, not to a campaign. Nothing in this
package accepts a campaign identifier, and nothing here makes anyone
outreach-eligible.

Modules:

* :mod:`provenance` — which observation currently wins a canonical company field
  and why;
* :mod:`dossiers` — raw research submissions and the immutable interpretations
  of them, with one current selection;
* :mod:`conflicts` — identity disagreements, derived rather than queued;
* :mod:`records` — the company list;
* :mod:`detail` — the company detail workspace read model.

No crawler, fetcher or research engine lives here. APP-004 owns producing
research; this package owns receiving it, keeping it apart from canonical
fields, and showing an operator what is claimed, what won, and what is unknown.
"""
