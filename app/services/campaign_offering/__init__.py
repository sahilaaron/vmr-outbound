"""Campaign-scoped offering research read from one URL.

A Campaign may lead with an offering VMR read from a page the operator pointed
at, instead of with the Library item it names. It is an override for one
Campaign and nothing more: the Library is never written to, no other Campaign is
affected, and the named Library offering stays available underneath as
supporting credibility.

The modules, in the order the work happens:

* :mod:`~app.services.campaign_offering.urls` — what counts as an address we will
  ask the model to read. Not a fetcher; the application still has none.
* :mod:`~app.services.campaign_offering.jobs` — the durable queue, which is also
  the version ledger, because one run *is* one version.
* :mod:`~app.services.campaign_offering.prompts` — the single question asked, and
  the trust rules that shape it.
* :mod:`~app.services.campaign_offering.runner` — one run, end to end.
* :mod:`~app.services.campaign_offering.contracts` — the structured answer, and
  the validator that refuses anything else.
* :mod:`~app.services.campaign_offering.consistency` — when preparation waits, so
  one Campaign never pitches two different things.
* :mod:`~app.services.campaign_offering.read` — the customer-facing view of all
  of the above.

Which offering a Campaign is actually leading with is *not* decided here. That is
:mod:`app.services.seller.effective`, deliberately next to the seller-context
boundary the per-contact Agents already ask.

Nothing is imported eagerly. ``consistency`` is imported by
``app.services.agents.controls``, and ``runner`` imports the orchestrator, so an
import here would close that into a cycle.
"""

from __future__ import annotations

__all__: list[str] = []
