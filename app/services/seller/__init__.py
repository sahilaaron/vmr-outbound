"""Seller-side knowledge base services (KB-001).

The seller half of the system's context: what we sell, what we may say, and to
whom. Operator-entered throughout — entering a record is the authorization for
it, so there is no review state and nothing here is generated, enriched, or
scored by a model.

Module map:

* :mod:`app.services.seller.common` — shared validation and the one error class.
* :mod:`app.services.seller.profile` — the single seller organisation profile.
* :mod:`app.services.seller.records` — offerings, proof points, restricted
  claims, personas, and their offering associations.
* :mod:`app.services.seller.campaign_offerings` — which offerings a campaign
  concerns. Association only; it never writes campaign copy or a call to action.
* :mod:`app.services.seller.readiness` — deterministic, explainable readiness.
  No model, no score, no gate.
* :mod:`app.services.seller.context` — the read-only retrieval boundary a
  future drafting step will ask for seller context.

Nothing in this package commits; the caller owns the transaction boundary.
"""

from app.services.seller.common import SellerKnowledgeError

__all__ = ["SellerKnowledgeError"]
